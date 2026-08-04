from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from openai import (
    OpenAI,
    APIStatusError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
)

# 本地模型(第二层备用)通常部署在 127.0.0.1，需要绕过系统代理避免 502
try:
    import httpx
    _HAS_HTTPX = True
except Exception:
    _HAS_HTTPX = False

from src.config import LlmConfig
from src.llm.exceptions import LLMRateLimitExhaustedError

logger = logging.getLogger(__name__)

# 模块级限流信号:主模型触发 429/超时等频次限制时写入时间戳,
# 供后台 LLM 任务(摘要/记忆提取)感知并退避,保护免费额度。
# 【P0-3 伴生修复】加锁防后台线程与 poller 线程并发写。

class _LlmState:
    """LLM 客户端状态（替代 global LAST_RATE_LIMIT_TS）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_rate_limit_ts: float = 0.0

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    @property
    def last_rate_limit_ts(self) -> float:
        return self._last_rate_limit_ts

    @last_rate_limit_ts.setter
    def last_rate_limit_ts(self, value: float) -> None:
        self._last_rate_limit_ts = value

    def mark_rate_limited(self) -> None:
        """记录一次主模型频次限制(429/超时),供后台任务退避判断。"""
        with self._lock:
            self._last_rate_limit_ts = time.time()

    def seconds_since_rate_limit(self) -> float:
        """距离上次主模型频次限制已过去的秒数。"""
        # 读锁不必要（float 读不是原子的的读——读偏值不会崩但可能读到中间态），
        # 以场景看偏差不超过一次赋值，影响可忽略。不加读锁减少开销。
        return time.time() - self._last_rate_limit_ts


_llm_state = _LlmState()


def mark_rate_limited() -> None:
    _llm_state.mark_rate_limited()


def seconds_since_rate_limit() -> float:
    return _llm_state.seconds_since_rate_limit()


# 全局尝试次数预算 = (主池+备池+第二层备池 各自 模型数×max_retries) + 该安全余量。
# 余量吸收池轮换/边界抖动，避免恰好卡在整数倍时误熔断（原 magic +10）。
_MAX_EXTRA_GLOBAL_ATTEMPTS = 10


# 降级到备用池时，若消息已含工具结果，注入的系统提示（F7/F8 抽出，原两层池各复制一份）
_TOOL_RESULT_HINT = {
    "role": "system",
    "content": (
        "[系统备注] 前面的消息中已包含工具调用返回的搜索/查询结果。"
        "请务必基于这些结果进行总结和回答，不要说'找不到信息'。"
        "如果结果不足以完整回答，请基于已有信息给出部分答案并说明缺失项。"
    ),
}


@dataclass
class _RetryState:
    """跨三层模型池共享的重试累加状态（F7/F8 抽出，便于单测与熔断判定）。"""
    global_max_attempts: int = 0
    total_attempts: int = 0
    rate_limited_observed: bool = False
    last_err: Exception | None = None
    last_fallback_err: Exception | None = None

    def note_attempt(self) -> None:
        self.total_attempts += 1

    def check_budget(self, phase: str) -> None:
        """全局尝试次数超 cap 时熔断：限频观测到抛 LLMRateLimitExhaustedError，否则 RuntimeError。"""
        if self.total_attempts > self.global_max_attempts:
            cause = self.last_fallback_err or self.last_err
            if self.rate_limited_observed:
                raise LLMRateLimitExhaustedError(
                    f"LLM 全局最大尝试次数 ({self.global_max_attempts}) 已达，"
                    f"且观测到限频(429)：{phase}。最后错误: {cause}"
                ) from cause
            raise RuntimeError(
                f"LLM 全局最大尝试次数 ({self.global_max_attempts}) 已达，最后错误: {cause}"
            ) from cause


def _classify_failure(e: Exception) -> tuple[bool, bool, bool]:
    """错误分类原语（F7/F8 抽出）：返回 (is_rate_limited, is_auth_error, is_retryable)。

    合并 chat() 三层池原先各复制一遍的错误判定：限频嗅探 / 鉴权嗅探 / 可重试判定。
    """
    text = str(e).lower()
    is_rate_limited = "rate_limit" in text or "429" in text
    is_auth_error = "401" in text or "403" in text
    is_retryable = _is_retryable_error(e)
    return is_rate_limited, is_auth_error, is_retryable


def _is_retryable_error(e: Exception) -> bool:
    """判定是否为「瞬时故障」值得重试(P0-2)。

    可重试:
      - RateLimitError(含 429 限流);
      - APITimeoutError(超时);
      - APIConnectionError(连接/网关/代理错误);
      - APIStatusError 且 status_code >= 500(服务端 5xx 瞬时错误)。
    不可重试(直接进 DLQ,不应浪费重试):
      - APIStatusError 且 400 <= status_code < 500(如 401/403 鉴权、400 格式错误);
      - 纯文本异常且不含 429/rate_limit/timeout/瞬时网络 关键词(如 authentication failed)。
    """
    if isinstance(e, (RateLimitError, APITimeoutError, APIConnectionError)):
        return True
    if isinstance(e, APIStatusError):
        status = getattr(e, "status_code", None)
        # 5xx 视为服务端瞬时故障;4xx 视为客户端/鉴权错误,不重试
        if status is None:
            return True
        return status >= 500
    # 非 openai 异常(如 httpx 层、网络抖动、应用层错误):
    # 复用旧契约文本嗅探(429/rate_limit/timeout 可重试),并补充瞬时网络关键词,
    # 其余(鉴权/格式/未知)判为不可重试,直接进 DLQ / 走备用模型。
    text = str(e).lower()
    retryable_text = (
        "rate_limit" in text
        or "429" in text
        or "timeout" in text
        or "timed out" in text
    )
    transient_hints = ("connection", "connect", "reset", "network",
                       "unavailable", "temporary", "econn")
    return retryable_text or any(h in text for h in transient_hints)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict]
    finish_reason: str
    usage: dict
    discarded_tool_names: list[str] = field(default_factory=list)
    discarded_tool_results: list[dict] = field(default_factory=list)
    is_chunk: bool = False


@dataclass
class LLMStreamChunk:
    content: str | None
    tool_calls: list[dict]
    finish_reason: str | None
    is_done: bool = False


class LLMClient:
    def __init__(self, config: LlmConfig):
        self.config = config
        api_key = os.environ.get("LLM_API_KEY") or config.api_key
        if not api_key:
            logger.warning("LLM API 密钥未设置(config.llm.api_key 或 LLM_API_KEY 环境变量)")
        base_url = self._normalize_base_url(config.base_url)
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key or "dummy",
            timeout=config.timeout,
            max_retries=0,
        )

        # 备用模型配置(从环境变量或配置文件读取)
        self.fallback_model = os.environ.get("LLM_FALLBACK_MODEL") or getattr(config, 'fallback_model', None)
        self.fallback_api_key = os.environ.get("LLM_FALLBACK_API_KEY") or getattr(config, 'fallback_api_key', None)
        self.fallback_base_url = os.environ.get("LLM_FALLBACK_BASE_URL") or getattr(config, 'fallback_base_url', None)
        # 备用模型池（跨服务商兜底，与主/同池都失败后的最终降级层）
        self.fallback_model_pool: list[str] = list(getattr(config, 'fallback_model_pool', []) or [])
        
        # 第二层备用模型配置（fallback 全部失败后切换）
        self.secondary_fallback_model = os.environ.get("LLM_SECONDARY_FALLBACK_MODEL") or getattr(config, 'secondary_fallback_model', None)
        self.secondary_fallback_api_key = os.environ.get("LLM_SECONDARY_FALLBACK_API_KEY") or getattr(config, 'secondary_fallback_api_key', None)
        self.secondary_fallback_base_url = os.environ.get("LLM_SECONDARY_FALLBACK_BASE_URL") or getattr(config, 'secondary_fallback_base_url', None)
        self.secondary_fallback_model_pool: list[str] = list(getattr(config, 'secondary_fallback_model_pool', []) or [])

        # -- 同服务商模型池(免费额度轮换)--
        # 与主模型共用 base_url / api_key 的备选模型列表(不含主模型本身)。
        # 构建轮换顺序:主模型在前,其余按配置顺序去重追加。
        pool = list(getattr(config, 'model_pool', []) or [])
        seen: set[str] = {config.model}
        self.model_pool: list[str] = [config.model]
        for m in pool:
            if m and m not in seen:
                seen.add(m)
                self.model_pool.append(m)
        # 池内除主模型外的部分(主模型失败后才依次尝试)
        self._pool_alternates = self.model_pool[1:]

        if self.fallback_model or self.fallback_model_pool:
            fb_clients = {}
            # 备用池：fallback_model_pool 内每个模型共用 fallback_base_url/fallback_api_key
            for m in self.fallback_model_pool:
                if m:
                    fb_clients[m] = OpenAI(
                        base_url=self._normalize_base_url(self.fallback_base_url),
                        api_key=self.fallback_api_key or api_key or "dummy",
                        timeout=config.timeout,
                        max_retries=0,
                    )
            # 单备用模型（旧）：fallback_model 单独建一个 client
            if self.fallback_model:
                fb_clients[self.fallback_model] = OpenAI(
                    base_url=self._normalize_base_url(self.fallback_base_url),
                    api_key=self.fallback_api_key or api_key or "dummy",
                    timeout=config.timeout,
                    max_retries=0,
                )
            self.fallback_clients = fb_clients
            self.fallback_order = list(fb_clients.keys())
            if self.fallback_order:
                logger.info("LLM 备用模型池已配置(%d个): %s @ %s",
                            len(self.fallback_order), self.fallback_order, self.fallback_base_url)
            else:
                self.fallback_clients = None
                logger.info("LLM 客户端已初始化,base_url: %s,模型: %s(无备用模型)", base_url, config.model)
        else:
            self.fallback_clients = None
            self.fallback_order: list[str] = []
            logger.info("LLM 客户端已初始化,base_url: %s,模型: %s(无备用模型)", base_url, config.model)
        
        # 第二层备用模型（fallback 全部失败后切换）
        # 本地模型通常部署在 127.0.0.1，需要绕过系统代理避免 502
        sf_http_client = None
        if _HAS_HTTPX and self.secondary_fallback_base_url:
            try:
                sf_http_client = httpx.Client(trust_env=False)
            except Exception as _exc:
                logger.warning(f"__init__: swallowed exception: {_exc}")
                pass
        if self.secondary_fallback_model or self.secondary_fallback_model_pool:
            sf_clients = {}
            for m in self.secondary_fallback_model_pool:
                if m:
                    sf_clients[m] = OpenAI(
                        base_url=self._normalize_base_url(self.secondary_fallback_base_url),
                        api_key=self.secondary_fallback_api_key or "dummy",
                        timeout=config.timeout,
                        max_retries=0,
                        http_client=sf_http_client,
                    )
            if self.secondary_fallback_model:
                sf_clients[self.secondary_fallback_model] = OpenAI(
                    base_url=self._normalize_base_url(self.secondary_fallback_base_url),
                    api_key=self.secondary_fallback_api_key or "dummy",
                    timeout=config.timeout,
                    max_retries=0,
                    http_client=sf_http_client,
                )
            self.secondary_fallback_clients = sf_clients
            self.secondary_fallback_order = list(sf_clients.keys())
            if self.secondary_fallback_order:
                logger.info("LLM 第二层备用模型池已配置(%d个): %s @ %s",
                            len(self.secondary_fallback_order), self.secondary_fallback_order, self.secondary_fallback_base_url)
            else:
                self.secondary_fallback_clients = None
        else:
            self.secondary_fallback_clients = None
            self.secondary_fallback_order: list[str] = []



    def _rebuild_model_pool(self) -> None:
        """根据最新的 self.config.model_pool 重建 self.model_pool 和 _pool_alternates。

        供热重载（reload_config）调用：config.yaml 中 llm.model_pool 变更后，无需重启即可
        刷新客户端持有的模型池列表，使新配置的免费模型在下一次 LLM 调用时立即被纳入轮换。
        """
        pool = list(getattr(self.config, 'model_pool', []) or [])
        seen: set[str] = {self.config.model}
        self.model_pool = [self.config.model]
        for m in pool:
            if m and m not in seen:
                seen.add(m)
                self.model_pool.append(m)
        self._pool_alternates = self.model_pool[1:]
        logger.info("LLM 模型池已重建：主模型=%s，池内备选 %d 个（%s）",
                    self.config.model, len(self._pool_alternates), self._pool_alternates)

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        if not url:
            return url
        url = url.rstrip("/")
        suffixes_to_strip = [
            "/chat/completions",
            "/v1/chat/completions",
            "/v1/chat",
            "/chat",
        ]
        for suffix in suffixes_to_strip:
            if url.endswith(suffix):
                logger.warning("LLM base_url 包含路径后缀 '%s',已自动去除", suffix)
                url = url[: -len(suffix)]
                break
        url = url.rstrip("/")
        if not url.endswith("/v1") and "/v1/" not in url and not url.endswith("/v1/"):
            pass
        return url



    def chat(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float | None = None, stream: bool = False) -> LLMResponse | Iterator[LLMStreamChunk]:
        """调用 LLM：主模型优先，瞬时故障指数退避重试；
        主模型耗尽后，在同一服务商模型池（model_pool）内逐个轮换；
        池内全部失败，再降级到跨服务商备用模型池（fallback_model_pool，
        无池则回退单 fallback_model），池内逐个轮换。
        401/403 鉴权错误会跳过同池轮换直接终止该层。

        Args:
            stream: 是否启用流式输出。流式仅在主模型上尝试，失败则降级为非流式。
        """
        from typing import Iterator

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        raw_retries = getattr(self.config, "max_retries", 3)
        max_retries = 3 if not isinstance(raw_retries, int) else max(1, int(raw_retries))
        raw_backoff = getattr(self.config, "base_backoff", 2.0)
        base_backoff = 2.0 if not isinstance(raw_backoff, (int, float)) else float(raw_backoff)

        attempts = [(self.client, m) for m in self.model_pool]

        global_max_attempts = (
            len(attempts) * max_retries
            + len(self.fallback_order) * max_retries
            + len(self.secondary_fallback_order) * max_retries
            + _MAX_EXTRA_GLOBAL_ATTEMPTS
        )
        # 跨三层池共享的重试累加状态（F7/F8 抽出，便于单测与熔断判定）
        state = _RetryState(global_max_attempts=global_max_attempts)

        # 流式仅在主模型尝试，失败降级为非流式
        if stream:
            for client, model in attempts:
                logger.info("LLM 流式请求: 模型=%s，消息数=%d，工具数=%d",
                            model, len(messages), len(tools or []))
                model_kwargs = dict(kwargs)
                model_kwargs["model"] = model
                try:
                    return self._do_chat(client, model_kwargs, stream=True)
                except Exception as e:
                    state.last_err = e
                    logger.warning("LLM(%s) 流式调用失败，降级为非流式: %s", model, e)
                    stream = False
                    break

        for client, model in attempts:
            if not stream:
                logger.info("LLM 请求: 模型=%s，消息数=%d，工具数=%d",
                            model, len(messages), len(tools or []))
            model_kwargs = dict(kwargs)
            model_kwargs["model"] = model
            result = self._retry_primary_model(
                client, model, model_kwargs, state, max_retries, base_backoff
            )
            if result is not None:
                return result
            continue

        # 主模型池全部失败后的降级路径
        if not self.fallback_clients:
            if state.total_attempts >= global_max_attempts:
                if state.rate_limited_observed:
                    raise LLMRateLimitExhaustedError(
                        f"LLM 全局最大尝试次数 ({global_max_attempts}) 已达，"
                        f"主模型池全部因限流失败。最后错误: {state.last_err}"
                    ) from state.last_err
                raise RuntimeError(
                    f"LLM 全局最大尝试次数 ({global_max_attempts}) 已达，最后错误: {state.last_err}"
                ) from state.last_err
            if state.rate_limited_observed:
                raise LLMRateLimitExhaustedError(
                    f"主模型池全部因限流(429)失败，且无备用模型池。最后错误: {state.last_err}"
                ) from state.last_err
            raise state.last_err  # type: ignore[misc]

        fb = self._try_fallback_pool(
            self.fallback_order, self.fallback_clients, kwargs, messages, state, label="跨服务商备用"
        )
        if fb is not None:
            return fb

        sf = self._try_fallback_pool(
            self.secondary_fallback_order, self.secondary_fallback_clients,
            kwargs, messages, state, label="第二层备用"
        )
        if sf is not None:
            return sf

        if state.rate_limited_observed:
            raise LLMRateLimitExhaustedError(
                f"主模型池与跨服务商备用池全部因限频(429/rate_limit)耗尽。"
                f"Last primary: {state.last_err}, Last fallback: {state.last_fallback_err}"
            ) from state.last_fallback_err
        raise RuntimeError(
            f"All primary pool and fallback LLM failed. "
            f"Last primary: {state.last_err}, Last fallback: {state.last_fallback_err}"
        ) from state.last_fallback_err

    def _retry_primary_model(self, client, model, model_kwargs, state, max_retries, base_backoff):
        """主模型池单模型重试原语（F7/F8 抽出）：对单模型做最多 max_retries 次指数退避重试。

        返回成功响应；若需尝试池内下一模型（重试耗尽或不可重试）返回 None。
        内部处理：限频标记 + 主模型池轮换 + 全局预算熔断（check_budget 抛错）。
        """
        for attempt in range(max_retries):
            state.note_attempt()
            state.check_budget("主模型池全部因限流失败")
            try:
                return self._do_chat(client, model_kwargs, stream=False)
            except Exception as e:
                state.last_err = e
                is_rate_limited, _is_auth, retryable = _classify_failure(e)
                if is_rate_limited:
                    mark_rate_limited()
                    state.rate_limited_observed = True
                    # 跨请求记忆：限频模型移到池尾，下次 chat() 不再优先尝试
                    try:
                        idx = self.model_pool.index(model)
                        self.model_pool.append(self.model_pool.pop(idx))
                        self._pool_alternates = self.model_pool[1:]
                    except ValueError as _exc:
                        logger.debug(f"_retry_primary_model: swallowed exception: {_exc}")
                        pass
                if not retryable:
                    logger.warning("LLM(%s) 失败（不可重试，跳至下一降级层）: %s", model, e)
                    return None
                if attempt < max_retries - 1:
                    wait = base_backoff * (2 ** attempt)
                    logger.warning(
                        "LLM(%s) 瞬时故障 (第%d/%d次重试)，等待 %.1fs 后重试: %s",
                        model, attempt + 1, max_retries, wait, e,
                    )
                    time.sleep(wait)
                    continue
                logger.warning("LLM(%s) 重试 %d 次后仍失败，尝试下一模型", model, max_retries)
        return None

    def _try_fallback_pool(self, pool_order, pool_clients, kwargs, messages, state, label):
        """跨服务商/第二层备用模型池尝试原语（F7/F8 抽出）：池内每模型单次尝试，失败跳下一模型。

        返回首个成功响应；池内全失败返回 None（并写入 state.last_fallback_err）。
        不在此层做内部重试——重试仅发生于主模型池（_retry_primary_model）。
        """
        if not pool_clients:
            return None
        last_pool_err: Exception | None = None
        for fb_model in pool_order:
            fb_client = pool_clients[fb_model]
            logger.info("模型池全部失败，切换到%s模型: %s（还剩 %d 个备用）",
                        label, fb_model, len(pool_order) - pool_order.index(fb_model) - 1)
            fb_kwargs = kwargs.copy()
            fb_kwargs["model"] = fb_model

            if any(m.get("role") == "tool" for m in messages):
                fb_kwargs["messages"] = [_TOOL_RESULT_HINT] + list(messages)

            state.note_attempt()
            state.check_budget(f"{label}模型池均因限流失败")
            try:
                return self._do_chat(fb_client, fb_kwargs, stream=False)
            except Exception as fb_err:
                last_pool_err = fb_err
                is_rate_limited, is_auth, _retryable = _classify_failure(fb_err)
                if is_rate_limited:
                    state.rate_limited_observed = True
                if not _retryable or is_auth:
                    logger.warning("%s模型 %s 失败（不可重试/鉴权错误，尝试下一备用）: %s",
                                   label, fb_model, fb_err)
                    continue
                logger.warning("%s模型 %s 失败，尝试下一备用: %s", label, fb_model, fb_err)
            continue
        state.last_fallback_err = last_pool_err
        return None


    def _do_chat(self, client: OpenAI, kwargs: dict, stream: bool = False) -> LLMResponse | Iterator[LLMStreamChunk]:
        """执行实际的 LLM 调用。"""
        if stream:
            return self._do_chat_stream(client, kwargs)
        response = client.chat.completions.create(**kwargs)
        result = self._parse_response(response, kwargs)
        # 响应回来后才能拿到真实消费 token 数（请求阶段尚无 usage）
        if result.usage:
            u = result.usage
            logger.info(
                "LLM 响应: 模型=%s，token 提示=%d 补全=%d 总计=%d",
                kwargs.get("model", "?"),
                u.get("prompt_tokens", 0),
                u.get("completion_tokens", 0),
                u.get("total_tokens", 0),
            )
        return result

    def _do_chat_stream(self, client: OpenAI, kwargs: dict) -> Iterator[LLMStreamChunk]:
        """执行流式 LLM 调用，返回迭代器。"""
        response = client.chat.completions.create(**kwargs, stream=True)
        tools = kwargs.get("tools") or []
        valid_names = {t.get("function", {}).get("name") for t in tools if isinstance(t, dict)}
        model = kwargs.get("model", "?")

        accumulated_content = ""
        tool_calls = []
        finish_reason = None

        for chunk in response:
            choice = chunk.choices[0]
            delta = choice.delta

            # 推理/thinking 字段（reasoning 模型专用）刻意不向用户暴露：
            # 仅记录、不 yield，确保思考链绝不进入 content/回复（需求 #3 通道层兜底）。
            _rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if _rc:
                logger.debug("[LLM] 丢弃 reasoning_content（不向用户暴露）: %d 字符", len(_rc))

            if delta.content:
                accumulated_content += delta.content
                yield LLMStreamChunk(
                    content=delta.content,
                    tool_calls=[],
                    finish_reason=None,
                    is_done=False,
                )

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    index = tc_delta.index
                    if index >= len(tool_calls):
                        tool_calls.append({
                            "id": "",
                            "name": "",
                            "arguments": "",
                        })

                    if tc_delta.id:
                        tool_calls[index]["id"] = tc_delta.id
                    if tc_delta.function.name:
                        tool_calls[index]["name"] = tc_delta.function.name
                    if tc_delta.function.arguments:
                        tool_calls[index]["arguments"] += tc_delta.function.arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        final_tool_calls = []
        discarded_names = []
        for tc in tool_calls:
            name = tc["name"]
            if not name:
                continue

            if name not in valid_names:
                normalized = name.replace("-", "_")
                if normalized in valid_names:
                    logger.info(
                        "LLM(%s) tool_call.name=%r 标准化为 %r",
                        model, name, normalized,
                    )
                    name = normalized
                else:
                    logger.warning(
                        "LLM(%s) 返回的 tool_call.name=%r 不在 schema 中，丢弃",
                        model, name,
                    )
                    discarded_names.append(name)
                    continue

            import json
            try:
                args = json.loads(tc["arguments"])
            except (json.JSONDecodeError, TypeError) as _exc:
                logger.warning(f"_do_chat_stream: swallowed exception: {_exc}")
                args = {}
            final_tool_calls.append({
                "id": tc["id"],
                "name": name,
                "args": args,
            })

        yield LLMStreamChunk(
            content=None,
            tool_calls=final_tool_calls,
            finish_reason=finish_reason,
            is_done=True,
        )

    def _parse_response(self, response, kwargs: dict) -> LLMResponse:
        """解析非流式 LLM 响应。"""
        choice = response.choices[0]
        msg = choice.message

        # 推理/thinking 字段（reasoning 模型）不并入 content（需求 #3 通道层兜底）：
        # 某些厂商把思考链放在 message.reasoning_content / .reasoning，必须显式排除，
        # 只取 message.content 作为回复正文，确保思考过程绝不进入用户可见内容。
        _rc = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        if _rc:
            logger.debug("[LLM] 响应含 reasoning_content，已排除在回复内容之外: %d 字符", len(_rc))

        tools = kwargs.get("tools") or []
        valid_names = {t.get("function", {}).get("name") for t in tools if isinstance(t, dict)}
        model = kwargs.get("model", "?")

        tool_calls = []
        discarded_names = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                if name not in valid_names:
                    normalized = name.replace("-", "_")
                    if normalized in valid_names:
                        logger.info(
                            "LLM(%s) tool_call.name=%r 标准化为 %r",
                            model, name, normalized,
                        )
                        name = normalized
                    else:
                        logger.warning(
                            "LLM(%s) 返回的 tool_call.name=%r 不在 schema 中，丢弃（可能工具已被收敛移除）",
                            model, tc.function.name,
                        )
                        discarded_names.append(name)
                        continue
                import json
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError) as _exc:
                    logger.warning(f"_parse_response: swallowed exception: {_exc}")
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": name,
                    "args": args,
                })

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        discarded_tool_results = []
        if discarded_names:
            discarded_set = set(discarded_names)
            for tc in msg.tool_calls:
                if tc.function.name in discarded_set:
                    discarded_tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": f"Tool {tc.function.name} not available",
                    })

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            discarded_tool_names=discarded_names,
            discarded_tool_results=discarded_tool_results,
        )
