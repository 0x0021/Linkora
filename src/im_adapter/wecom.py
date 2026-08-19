"""企业微信（WeCom）CLI 适配器 —— 基于真实 ``wecom-cli`` v0.1.9 语法实现。

继承路径：``base_adapter.BaseIMAdapter``（统一能力接口 + CLI 执行引擎）。

本实现已对照真实 ``wecom-cli`` 探查结果落地（非凭空猜测）：

- CLI 二进制默认 ``/opt/homebrew/bin/wecom-cli``，领域式语法：
  ``wecom-cli msg send_message --json '{...}'`` /
  ``wecom-cli contact get_userlist`` 等。
- **所有参数走 ``--json '<JSON>'`` 传递**（子命令另有 ``--schema`` 导出参数结构）。
- **输出是 JSON-RPC 信封**，退出码恒为 0（无论成败）：：

      {"id":"mcp_rpc_xxx","jsonrpc":"2.0",
       "result":{"content":[{"text":"<真实JSON字符串>","type":"text"}],"isError":false}}

  真实业务数据在 ``result.content[0].text``（内部又是一层 JSON 字符串）。
- **错误形态分两层**：
  1. 调用层失败（校验/网络）→ stdout 前缀 ``Error: 请求失败：{信封}``，
     信封内 ``result.isError=true``，``text`` 为 ``"Error executing tool X: ..."``。
  2. API 层失败（如无效 media_id）→ 信封 ``isError=false``，但内层
     ``{"errcode": 850017, "errmsg": "..."}`` 的 ``errcode != 0``。
  两层都需在 ``run()`` 中识别并抛出对应异常。
- 因此需**完全覆写 ``run()``**：解析信封 → 判定 ``isError`` / 内层 ``errcode`` →
  抽取真实错误文本交给 ``_classify_error`` 分类（权限 / 可重试 / 基础）。

已实现：认证 / 组织 / 联系人 / 会话消息拉取 / 发送 / 媒体下载 的 IM 核心闭环。
未实现（企微 CLI 无对应能力）：
- ``doc_search`` / ``doc_read`` / ``mark_read``：空实现（经工具层门控对飞书/企微隐藏，不会暴露）。
- ``calendar_event_list`` / ``todo_task_create``：显式抛出 ``IMAdapterUnsupportedTypeError``，
  落实「门控工具应显式报错」决策——get_calendar_events / create_todo 在工具层已对企微
  门控隐藏（platforms=["dingtalk"]），此处仅防御性兜底，避免静默返回空结果被误判为成功。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

# 重试退避上限（秒）：防止 retries 配置被调大时 2**attempt 指数退避暴涨，
# 长时间阻塞调用线程（原逻辑 time.sleep(min(2 ** attempt, MAX_BACKOFF_SECONDS)) 无封顶）。
MAX_BACKOFF_SECONDS = 30

from .base_adapter import BaseIMAdapter
from .errors import IMAdapterError, IMAdapterUnsupportedTypeError
from .markdown_fix import normalize_markdown_for_platform

logger = logging.getLogger(__name__)

# 企业微信标准 errcode（用于错误分类；未命中时再走关键字兜底）
# 参考：https://open.work.weixin.qq.com/devtool/query
_PERMISSION_CODES = frozenset({
    48002,   # api 接口无权限
    60003,   # 不允许通过通讯录同步助手获取非企业成员的userid
    60011,   # 不允许获取该类型的成员/部门/标签
    60013,   # 不存在的成员/部门/标签
    60020,   # 不允许访问的IP或来源
    61004,   # 部门/成员无权限
    48003,   # 接口无权限（应用无此权限）
})
_RETRYABLE_CODES = frozenset({
    40014,   # 不合法的 access_token（可刷新重试）
    41001,   # 缺少 access_token
    42001,   # access_token 已过期
    45009,   # 接口调用超限（限频）
    45011,   # 接口调用频率超过限制
})
_PERMISSION_HINTS = (
    "permission", "forbidden", "no permission", "access denied",
    "不允许", "无权限", "api forbidden",
)
_RETRYABLE_HINTS = (
    "rate", "limit", "quota", "frequency", "timeout", "timed out",
    "network", "connection", "try again", "too many", "throttl",
    "freq", "超限", "频率",
)


class WecomCliAdapter(BaseIMAdapter):
    """企业微信（WeCom）CLI 适配器（基于 ``wecom-cli`` 实现 IM 核心能力）。

    用法：:

        adapter = WecomCliAdapter()                 # 默认调 /opt/homebrew/bin/wecom-cli
        adapter.chat_message_send(user="owner", text="你好")

    注意：
    - 企微 CLI **没有** ``--dry-run`` 标志，故 ``_build_command`` 直接拼命令；
      发送是否真实完全由调用方是否 ``force_no_dry_run=True`` 决定（与 dws 一致，
      bot 的发送路径始终强制真实调用）。
    - 企微以「当前扫码登录用户」驱动，单租户，无钉钉式 CorpId 概念；
      ``get_current_org`` 等以伪组织占位返回。
    """

    # 企微 markdown 子集同样不支持 GFM 表格：发送前转换（同钉钉）
    supports_markdown_tables = False

    @staticmethod
    def _resolve_cli_path(cli_path: str) -> str:
        """解析 wecom-cli 路径，兼容 Apple Silicon 与 Intel Mac Homebrew。

        优先级：显式传入 → PATH 查找 → /opt/homebrew/bin（Apple Silicon）→
        /usr/local/bin（Intel Mac）→ /usr/bin（系统默认）。
        若全未命中，返回原始路径，由上层在首次调用时因 FileNotFoundError 报明确错误。
        """
        if cli_path:
            return cli_path
        found = shutil.which("wecom-cli")
        if found:
            return found
        # 多架构 fallback：依次检测各典型 Homebrew 安装路径
        fallback_paths = [
            "/opt/homebrew/bin/wecom-cli",   # Apple Silicon (arm64)
            "/usr/local/bin/wecom-cli",       # Intel Mac (x86_64)
            "/usr/bin/wecom-cli",             # 系统级安装
        ]
        for p in fallback_paths:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                logger.info("wecom-cli 自动发现于 fallback 路径: %s", p)
                return p
        # 全部未命中：返回第一个 fallback 路径，由首次 subprocess.run 报错
        logger.warning(
            "wecom-cli 未在 PATH 及常见路径中找到，已回退至 %s（若不可执行请安装 wecom-cli）",
            fallback_paths[0],
        )
        return fallback_paths[0]

    def __init__(self, cli_path: str = "",
                 timeout: int = 30, retries: int = 2,
                 dry_run: bool = True, profile: str = ""):
        cli_path = self._resolve_cli_path(cli_path)
        super().__init__(cli_path=cli_path, timeout=timeout, retries=retries,
                         dry_run=dry_run, profile=profile)
        # 授权过期（850003）友好日志与降噪状态
        self._auth_expired_active = False
        self._auth_expired_log_ts = 0.0
        self._aibot_id_cache: str | None = None

    def warn_read_signal_unsupported(self, poller_config: Any) -> None:
        """启动期一次性告警：企微 CLI 不支持已读回执，依赖对方「已读」信号的回复门控 /
        标记已读在企微静默失效。

        - ``suppress_when_owner_read``（已读闸门：会话被判定已读则抑制 AI 回复）
        - ``mark_read_after_process``（处理后标记会话已读，消除未读红点）
        二者都依赖平台已读回执能力；企微 CLI 无对应实现（基类 ``mark_read`` 为空操作，
        ``chat_message_list_unread_conversations`` 仅返回近期活跃会话近似替代），开关开启时
        用户会误以为「老板已读就不抢答」的保护在生效，实际不生效。此处显式告警使其可见，
        不在轮询热路径里每轮刷日志（启动期调用一次即可）。
        """
        suppress = getattr(poller_config, "suppress_when_owner_read", False)
        mark_read = getattr(poller_config, "mark_read_after_process", False)
        if suppress or mark_read:
            logger.warning(
                "[wecom] 当前平台不支持已读回执，"
                "suppress_when_owner_read / mark_read_after_process 将不生效"
            )

    # ------------------------------------------------------------------
    # 引擎覆写：JSON-RPC 信封解析
    # ------------------------------------------------------------------

    def _build_command(self, args: list[str], *,
                       force_no_dry_run: bool = False) -> list[str]:
        """企微无 ``--dry-run`` / ``--profile`` 概念，直接拼 ``[cli, *args]``。"""
        return [self.cli_path, *list(args)]

    def run(self, args: list[str], timeout: int | None = None,
            retries: int | None = None, operation: str = "",
            force_no_dry_run: bool = False) -> dict:
        """执行命令，解析 ``wecom-cli`` 的 JSON-RPC 信封。

        退出码恒为 0，成败全看信封里的 ``result.isError`` 与内层 ``errcode``。
        错误文本经 ``_classify_error`` 映射为 ``IMAdapter*`` 异常并按重试策略退避。
        """
        timeout = timeout if timeout is not None else self.timeout
        retries = retries if retries is not None else self.retries
        cmd = self._build_command(args, force_no_dry_run=force_no_dry_run)
        last_error: Any = None
        for attempt in range(retries + 1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, encoding="utf-8",
                    env=self._no_browser_env,
                )
                # 【P0-2026-08-08】不再忽略 CLI 退出码：此前只取 stdout 并直接解析，
                # CLI 崩溃（段错误 / 二进制缺失 / 子进程异常终止）被当成「成功无数据」，
                # 表现为企微消息静默丢失。此处对齐 base.py 通用引擎的 returncode 处理。
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
                    if result.returncode < 0:
                        # 负数退出码 = 子进程被信号杀死（如关机阶段 Ctrl+C 把企微 CLI
                        # 一并终止），属正常关机而非真实故障，降级为 debug 且不重试。
                        sig = -result.returncode
                        logger.debug("%s 子进程被信号 %d 终止（可能处于关机阶段）: %s",
                                     self.cli_path, sig, stderr)
                        raise self._shutdown_error_class()(
                            f"{self.cli_path} terminated by signal {sig}: {stderr}")
                    error_class = self._classify_error(stderr)
                    raise error_class(f"{self.cli_path} exit {result.returncode}: {stderr}")
                output = (result.stdout or "").strip()
                return self._parse_output(output)
            except subprocess.TimeoutExpired:
                last_error = self._retryable_error_class()(
                    f"{self.cli_path} 超时 {timeout}s")
                if attempt < retries:
                    time.sleep(min(2 ** attempt, MAX_BACKOFF_SECONDS))
                    continue
                raise
            except NotImplementedError:
                raise
            except self._permission_error_class():
                raise
            except self._non_retryable_error_class():
                raise
            except self._retryable_error_class() as e:
                last_error = e
                if attempt < retries:
                    time.sleep(min(2 ** attempt, MAX_BACKOFF_SECONDS))
                    continue
                raise
            except self._base_error_class() as e:
                last_error = e
                if attempt < retries:
                    time.sleep(min(2 ** attempt, MAX_BACKOFF_SECONDS))
                    continue
                raise
        raise last_error  # type: ignore

    def _parse_output(self, output: str) -> dict:
        """从 ``wecom-cli`` 的 stdout 抽取真实业务 dict。

        处理三种情形：
        - 空输出 → ``{}``
        - ``Error: 请求失败：{信封}`` → 解析信封并抛错
        - 纯信封 JSON → 取 ``result.content[0].text`` 内层 JSON 字符串 →
          若 ``isError`` 或内层 ``errcode != 0`` 则抛错，否则返回内层 dict
        """
        raw = (output or "").strip()
        if not raw:
            return {}
        if raw.startswith("Error:"):
            idx = raw.find("{")
            if idx >= 0:
                raw = raw[idx:]
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as e:
            raise self._base_error_class()(
                f"wecom: 无法解析 JSON 输出: {e}\n{raw[:500]}") from e
        if not isinstance(envelope, dict) or "result" not in envelope:
            raise self._base_error_class()(
                f"wecom: 响应缺少 result 信封: {raw[:500]}")
        result = envelope.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
        if bool(result.get("isError")):
            err_text = text or raw
            err_cls = self._classify_error(err_text)
            raise err_cls(err_text)
        if not text:
            return {}
        try:
            inner = json.loads(text)
        except json.JSONDecodeError as _exc:
            logger.debug(f"_parse_output: swallowed exception: {_exc}")
            return {"raw": text}
        if isinstance(inner, dict) and inner.get("errcode", 0) != 0:
            errmsg = inner.get("errmsg", "")
            detail = errmsg or json.dumps(inner, ensure_ascii=False)
            err_cls = self._classify_error(detail)
            raise err_cls(detail)
        return inner

    def _classify_error(self, error_msg: str) -> type[IMAdapterError]:
        """把企微错误文本映射为 ``IMAdapter*`` 异常类。

        错误文本可能是：
        - ``{"errcode":N,"errmsg":"..."}``（API 层错误）
        - ``Error executing tool X: 2 validation errors...``（调用层校验错误）
        """
        msg = error_msg or ""
        code = None
        try:
            obj = json.loads(msg)
            if isinstance(obj, dict):
                code = obj.get("errcode")
                sub = obj.get("errmsg")
                if isinstance(sub, str) and sub:
                    msg = sub
        except Exception as e:
            logger.debug("[resilience] 解析异常，使用兜底: %s", e)
            pass
        low = msg.lower()
        if code in _PERMISSION_CODES or any(k in low for k in _PERMISSION_HINTS):
            return self._permission_error_class()
        if code in _RETRYABLE_CODES or any(k in low for k in _RETRYABLE_HINTS):
            return self._retryable_error_class()
        return self._base_error_class()

    # ------------------------------------------------------------------
    # 授权过期（850003）友好日志与降噪
    # ------------------------------------------------------------------
    AUTH_EXPIRED_ERRCODES = frozenset({850003})
    AUTH_LOG_INTERVAL_SECONDS = 600  # 授权过期提醒最小间隔：10 分钟内只报一次，避免轮询刷屏

    _AUTH_URL_TPL = (
        "https://work.weixin.qq.com/ai/aiHelper/authorizationPage"
        "?str_aibotid={aibot_id}&type=6&from=chat&forceInnerBrowser=1"
    )

    def _extract_aibot_id(self, text: str) -> str | None:
        """从企微错误文本或本地 ``auth show`` 解析机器人 ID（aibot_id）。"""
        if text:
            m = re.search(r"str_aibotid=([A-Za-z0-9_-]+)", text)
            if m:
                return m.group(1)
            m = re.search(r"机器人\s*id[：:]\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
            if m:
                return m.group(1)
        # 兜底：调 wecom-cli auth show（结果缓存一次，避免反复 subprocess）
        if self._aibot_id_cache is None:
            try:
                out = subprocess.run(
                    [self.cli_path, "auth", "show"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", env=self._no_browser_env,
                )
                obj = json.loads((out.stdout or "").strip())
                self._aibot_id_cache = obj.get("id") or ""
            except Exception as e:  # noqa: BLE001
                logger.debug("提取 aibot_id 失败: %s", e)
                self._aibot_id_cache = ""
        return self._aibot_id_cache or None

    def _is_auth_expired_error(self, exc: Exception) -> bool:
        """判定异常是否为企微「消息」能力授权过期（850003）。"""
        text = str(exc)
        m = re.search(r"errcode[\"'\s:]+([0-9]+)", text)
        if m and int(m.group(1)) in self.AUTH_EXPIRED_ERRCODES:
            return True
        low = text.lower()
        if "authorization expired" in low:
            return True
        if ("授权" in text or "authorization" in low) and ("消息" in text or "message" in low):
            return True
        return False

    def _auth_expired_message(self, exc: Exception) -> str:
        aibot_id = self._extract_aibot_id(str(exc))
        url = self._AUTH_URL_TPL.format(aibot_id=aibot_id) if aibot_id else ""
        lines = [
            "企业微信机器人「消息」能力授权已过期（errcode 850003），机器人暂时无法读取/接收消息。",
            "原因：企业微信「智能机器人」的「消息」权限到期，需创建者重新授权。",
            "解决办法（二选一）：",
            "  1) 在企业微信内置浏览器打开下方授权链接完成授权；",
            "  2) 前往「工作台 → 智能机器人」找到对应机器人，重授权「消息」权限。",
        ]
        if url:
            lines.append(f"授权链接：{url}")
        if aibot_id:
            lines.append(f"机器人 ID：{aibot_id}")
        lines.append("授权后无需重启本服务，下一轮轮询将自动恢复。")
        return "\n".join(lines)

    def _maybe_log_auth_expired(self, exc: Exception) -> bool:
        """若异常为授权过期，按降噪间隔打印一条人话提醒，返回 True；否则 False。"""
        if not self._is_auth_expired_error(exc):
            return False
        now = time.time()
        if self._auth_expired_active and (now - self._auth_expired_log_ts) < self.AUTH_LOG_INTERVAL_SECONDS:
            return True  # 降噪：未到再次提醒间隔，仅抑制，不重复打印
        logger.warning("⚠️ %s", self._auth_expired_message(exc))
        self._auth_expired_active = True
        self._auth_expired_log_ts = now
        return True

    def _maybe_log_auth_recovered(self) -> None:
        """若之前处于授权过期状态，打印一条恢复提醒并重置。"""
        if self._auth_expired_active:
            logger.info("✅ 企业微信机器人「消息」权限已恢复，消息拉取恢复正常。")
            self._auth_expired_active = False
            self._auth_expired_log_ts = 0.0

    # ------------------------------------------------------------------
    # 认证 / 组织
    # ------------------------------------------------------------------

    def auth_status(self) -> dict:
        """检查认证状态（企微无 whoami，以 ``contact get_userlist`` 作为连通性探针）。"""
        try:
            resp = self.run(["contact", "get_userlist"], force_no_dry_run=True)
        except Exception as e:  # noqa: BLE001 - 探测失败即未登录
            return {"authenticated": False, "error": str(e)}
        ok = isinstance(resp, dict) and resp.get("errcode") == 0
        return {
            "authenticated": ok,
            "brand": "wecom",
            "userlist_count": len(resp.get("userlist") or []) if isinstance(resp, dict) else 0,
        }

    def is_authenticated(self) -> bool | str:
        """零网络优先不可用；以 ``get_userlist`` 探测判定登录态。"""
        try:
            resp = self.run(["contact", "get_userlist"], force_no_dry_run=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[resilience] is_authenticated 探测异常: %s", e, exc_info=True)
            return False
        if not isinstance(resp, dict):
            return False
        return resp.get("errcode") == 0

    def auth_login(self, device_flow: bool = False, no_browser: bool = True) -> dict:
        """触发扫码登录（``wecom-cli init``）。通常为交互式，需用户手动扫码。

        内置重试机制：企微 init 超时或网络错误时自动重试最多 2 次（间隔 3 秒）。
        """
        args = ["init"]
        if no_browser:
            args.append("--no-open")
        last_error = None
        for attempt in range(3):
            try:
                return self.run(args, timeout=300, force_no_dry_run=True)
            # 基类 run() 超时抛的是 _retryable_error_class()（与本文件其余捕获点一致）。
            # 此前误写为 IMAdapterTimeoutError —— 该类根本不存在，求值 except 子句即抛
            # NameError 并穿透整个 try（连下面的 except Exception 也接不住），
            # 导致企微登录一超时就崩、3 次重试完全失效。
            except self._retryable_error_class() as e:
                logger.warning("wecom auth_login 超时 (attempt %d/3): %s", attempt + 1, e)
                last_error = e
                if attempt < 2:
                    time.sleep(3)
            except Exception as e:
                logger.error("wecom auth_login 异常 (attempt %d/3): %s", attempt + 1, e)
                last_error = e
                if attempt < 2:
                    time.sleep(3)
        raise last_error or RuntimeError("wecom auth_login 失败：所有重试均已用完")

    def profile_list(self) -> dict:
        """企微无多 profile 概念，返回单一伪 profile。"""
        try:
            auth = self.is_authenticated()
        except Exception as e:  # noqa: BLE001
            logger.warning("[resilience] profile_list 探测认证态异常: %s", e, exc_info=True)
            auth = False
        return {"authenticated": bool(auth), "profiles": [{"name": "wecom", "brand": "wecom"}]}

    def get_current_org(self) -> dict:
        """企微单租户，以伪组织占位返回。"""
        return {"corp_id": "wecom", "corp_name": "企业微信"}

    def list_orgs(self) -> list[dict]:
        """企微单租户，返回当前组织作为唯一项。"""
        return [{"corp_id": "wecom", "corp_name": "企业微信"}]

    # ------------------------------------------------------------------
    # 联系人
    # ------------------------------------------------------------------

    def contact_user_get_self(self) -> dict:
        """获取当前登录用户自身信息。

        优先读环境变量 ``WECOM_USER_ID``；其次尝试 ``contact get_userlist``
        （单用户场景即为自己）；最后 fallback 系统 ``$USER``。
        """
        user_id = os.environ.get("WECOM_USER_ID", "")
        if user_id:
            return {"user_id": user_id, "name": user_id, "title": ""}
        try:
            resp = self.run(["contact", "get_userlist"], force_no_dry_run=True)
            users = resp.get("userlist") or [] if isinstance(resp, dict) else []
            if len(users) == 1:
                u = users[0]
                return {
                    "user_id": u.get("userid", ""),
                    "name": u.get("name", ""),
                    "title": u.get("position") or u.get("title") or "",
                }
        except Exception as e:
            logger.debug("[resilience] 解析异常，使用兜底: %s", e)
            pass
        user_id = os.environ.get("USER", "")
        return {"user_id": user_id, "name": user_id, "title": ""} if user_id else {}

    def contact_user_search(self, keyword: str) -> list[dict]:
        """按关键字搜索联系人（企微仅提供全量 ``get_userlist``，客户端过滤）。"""
        if not keyword:
            return []
        try:
            resp = self.run(["contact", "get_userlist"], force_no_dry_run=True)
        except Exception as e:  # noqa: BLE001
            if self._maybe_log_auth_expired(e):
                return []
            logger.warning("[resilience] 拉取列表失败，返回空: %s", e, exc_info=True)
            return []
        users = resp.get("userlist") or [] if isinstance(resp, dict) else []
        kw = keyword.lower()
        return [
            u for u in users
            if kw in (str(u.get("name", "")) + str(u.get("userid", "")) + str(u.get("alias", ""))).lower()
        ]

    # ------------------------------------------------------------------
    # 会话 / 消息拉取
    # ------------------------------------------------------------------

    def _infer_single_chat(self, chat: dict) -> bool:
        """判断会话是否为单聊。

        以企微原生 ``chat_type`` 字段为准：``1`` 表示单聊、``2`` 表示群聊
        （与 ``get_message`` / ``chat_message_list_all`` 调用口径一致）。
        ``chat_type`` 缺失时按 ``is_group`` 推断（与既有实现保持一致）。
        """
        ct = chat.get("chat_type")
        if ct is not None:
            return str(ct) == "1"
        return not bool(chat.get("is_group"))

    def _normalize_chat(self, chat: dict) -> dict:
        """将企微原生会话格式映射为 poller 兼容的通用会话格式。

        poller 依赖两个 dingtalk 特有字段：
        - ``openConversationId``: 会话唯一标识（企微用 ``chatid`` / ``id``）
        - ``singleChat``: 是否为单聊（以企微 ``chat_type`` 字段为准）

        与飞书 ``_normalize_chat`` 保持镜像，确保企微未读/最近会话能被
        poller 的未读通道正确识别（否则 ``openConversationId`` 缺失会被跳过）。
        """
        cid = chat.get("chatid") or chat.get("id") or chat.get("chat_id") or ""
        if not cid:
            return chat
        result = dict(chat)
        result.setdefault("openConversationId", cid)
        result.setdefault("title", chat.get("name") or chat.get("title") or cid)
        if "singleChat" not in result:
            result["singleChat"] = self._infer_single_chat(chat)
        return result

    def chat_message_list_unread_conversations(self, count: int = 20) -> list[dict]:
        """获取近期会话列表。

        **重要限制**：企微 CLI 不支持严格语义的「未读」查询（即只能返回有未读消息的会话），
        当前实现为取最近 7 天内有过消息的活跃会话作为近似替代。
        方法名保留 ``unread`` 仅用于保持跨平台接口契约一致，调用方不可假定结果均为真未读。
        """
        begin, end = self._time_window()
        try:
            resp = self.run(
                ["msg", "get_msg_chat_list", "--json",
                 json.dumps({"begin_time": begin, "end_time": end, "cursor": None})],
                force_no_dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            if self._maybe_log_auth_expired(e):
                return []
            logger.warning("[resilience] 拉取列表失败，返回空: %s", e, exc_info=True)
            return []
        chats = resp.get("chats") or [] if isinstance(resp, dict) else []
        # 归一化：补 openConversationId / singleChat，供 poller 未读通道识别会话
        self._maybe_log_auth_recovered()
        return [self._normalize_chat(c) for c in chats[:count]]

    def chat_message_list_direct(self, user_id: str = "",
                                 open_dingtalk_id: str = "",
                                 time_str: str = "",
                                 limit: int = 50) -> list[dict]:
        """拉取单聊消息（按时间正序）。``user_id`` / ``open_dingtalk_id`` 二选一。"""
        target = user_id or open_dingtalk_id
        if not target:
            raise ValueError("chat_message_list_direct 需提供 user_id 或 open_dingtalk_id")
        begin, end = self._time_window(time_str)
        try:
            resp = self.run(
                ["msg", "get_message", "--json",
                 json.dumps({"chat_type": 1, "chatid": target,
                             "begin_time": begin, "end_time": end, "cursor": None})],
                force_no_dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            if self._maybe_log_auth_expired(e):
                return []
            logger.warning("[resilience] 拉取列表失败，返回空: %s", e, exc_info=True)
            return []
        msgs = resp.get("messages") or [] if isinstance(resp, dict) else []
        return [self._normalize_message(m) for m in msgs[:limit]]

    def chat_message_list(self, group: str, time_str: str,
                          limit: int = 50) -> list[dict]:
        """拉取群聊消息（按时间正序）。``group`` 即 chat_id。"""
        if not group:
            raise ValueError("chat_message_list 需提供 group(chat_id)")
        begin, end = self._time_window(time_str)
        try:
            resp = self.run(
                ["msg", "get_message", "--json",
                 json.dumps({"chat_type": 2, "chatid": group,
                             "begin_time": begin, "end_time": end, "cursor": None})],
                force_no_dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            if self._maybe_log_auth_expired(e):
                return []
            logger.warning("[resilience] 拉取列表失败，返回空: %s", e, exc_info=True)
            return []
        msgs = resp.get("messages") or [] if isinstance(resp, dict) else []
        return [self._normalize_message(m) for m in msgs[:limit]]

    def _normalize_message(self, msg: dict) -> dict:
        """将企微消息字段映射为 poller ``_raw_to_message`` 期望的 dingtalk 字段名。

        ``_raw_to_message`` 依赖以下字段：
        - ``openMessageId`` / ``msgId`` → 企微 ``msgid``
        - ``senderName`` / ``sender`` → 企微 ``sender_name`` 或 ``sender``
        - ``senderOpenDingTalkId`` / ``senderId`` → 企微 ``sender``
        - ``content`` → 企微 ``content``（可能是 dict 或字符串）
        - ``msgType`` → 企微 ``msg_type``
        - ``createTime`` → 企微 ``msg_time``（Unix 毫秒时间戳）
        """
        result = dict(msg)
        # 消息 ID
        mid = msg.get("msgid") or msg.get("msg_id") or ""
        if mid and "openMessageId" not in result:
            result["openMessageId"] = mid
            result.setdefault("msgId", mid)
        # 发送者
        sid = msg.get("sender") or msg.get("sender_id") or msg.get("from") or ""
        sname = msg.get("sender_name") or msg.get("sendername") or sid or ""
        if sid:
            result["senderId"] = sid
            result["senderOpenDingTalkId"] = sid
        if sname:
            result["sender"] = sname
            result["senderName"] = sname
        # 消息正文
        content = msg.get("content")
        if content is not None:
            if isinstance(content, dict):
                result.setdefault("content", json.dumps(content, ensure_ascii=False))
            else:
                result.setdefault("content", str(content))
        # 消息类型
        mt = msg.get("msg_type") or msg.get("msgtype") or msg.get("msgType") or ""
        if mt:
            result.setdefault("msgType", mt)
        # 时间戳：企微 msg_time 通常为 Unix 毫秒时间戳
        ct = msg.get("msg_time")
        if ct is not None:
            ts = self._normalize_timestamp(ct)
            if ts:
                result.setdefault("createTime", ts)
        return result

    def chat_message_list_all(self, start: str, end: str,
                              limit: int = 50,
                              max_pages: int | None = None,
                              chat_ids: list[str] | None = None,
                              chat_meta: dict[str, dict] | None = None) -> dict:
        """按时间范围拉取所有消息（单聊 + 群聊），自动分页聚合。

        实现：列出全部会话（7 天窗）→ 逐会话 ``get_message`` 合并为
        ``conversationMessagesList`` 格式（poller 兼容）。
        """
        try:
            cl = self.run(
                ["msg", "get_msg_chat_list", "--json",
                 json.dumps({"begin_time": start, "end_time": end, "cursor": None})],
                force_no_dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            if self._maybe_log_auth_expired(e):
                return {"conversationMessagesList": []}
            logger.warning("[resilience] chat_message_list_all 拉取会话列表异常: %s", e, exc_info=True)
            cl = {}
        chats = cl.get("chats") or [] if isinstance(cl, dict) else []

        conv_list: list[dict] = []
        for chat in chats:
            chatid = chat.get("chatid") or chat.get("id") or ""
            chat_type = chat.get("chat_type") or (2 if chat.get("is_group") else 1)
            title = chat.get("name") or chat.get("title") or chatid
            if not chatid:
                continue
            try:
                mr = self.run(
                    ["msg", "get_message", "--json",
                     json.dumps({"chat_type": chat_type, "chatid": chatid,
                                 "begin_time": start, "end_time": end, "cursor": None})],
                    force_no_dry_run=True,
                )
            except Exception as e:  # noqa: BLE001
                if self._maybe_log_auth_expired(e):
                    continue
                logger.warning("[resilience] chat_message_list_all 拉取会话 %s 消息异常: %s", chatid, e, exc_info=True)
                continue
            msgs = mr.get("messages") or [] if isinstance(mr, dict) else []
            if msgs:
                normalized = [self._normalize_message(m) for m in msgs]
                conv_list.append({
                    "openConversationId": chatid,
                    "title": title,
                    "messages": normalized,
                    "singleChat": chat_type == 1,
                })
        self._maybe_log_auth_recovered()
        return {"conversationMessagesList": conv_list}

    # ------------------------------------------------------------------
    # 发送 / 媒体
    # ------------------------------------------------------------------

    def chat_message_send(self, *, group: str | None = None,
                          user: str | None = None,
                          open_dingtalk_id: str | None = None,
                          title: str = "", text: str = "",
                          uuid: str | None = None,
                          ai_tag: bool | None = None,
                          msg_type: str | None = None,
                          media_id: str | None = None,
                          file_path: str | None = None,
                          at_all: bool = False,
                          at_open_dingtalk_ids: str | None = None) -> dict:
        """发送文本消息（企微 ``send_message`` 当前仅支持文本，最大 2048 字节）。

        参数映射：
        - 目标：``group`` → ``chat_type=2``（群聊，chatid=群ID）；
          ``user`` / ``open_dingtalk_id`` → ``chat_type=1``（单聊，chatid=userid）。
        - ``text`` / ``title`` → ``text.content``。
        - 富媒体参数（``media_id`` / ``file_path`` / 非 text 的 ``msg_type``）：
          企微暂不支持，**忽略并告警**（保持接口契约一致）。
        - ``uuid``：企微 CLI 不支持幂等键（``send_message`` 无 ``idempotency_key`` 参数），
          接口以 ``"noop_uuid"`` 标记原值回传给调用方，调用方可自行幂等判定。

        Returns:
            dict（含 ``errcode`` / ``errmsg`` 等企微原始字段），提供 ``uuid`` 时额外
            包含 ``"noop_uuid": uuid`` 标记，保持与钉钉「返回原始 dict」一致的契约。
        """
        target = group or user or open_dingtalk_id
        if not target:
            raise ValueError("chat_message_send 需提供 group / user / open_dingtalk_id 之一")
        if media_id or file_path or (msg_type and msg_type != "text"):
            logger.warning(
                "wecom 不支持 %s 消息类型，已降级为纯文本发送（media_id=%s, file_path=%s）",
                msg_type or "富媒体", media_id, file_path,
            )
        content = text or title
        if not content:
            raise ValueError("chat_message_send 需提供 text 或 title")
        # 企微 markdown 子集不支持 GFM 表格：按平台能力转换（supports_markdown_tables=False）
        if not self.supports_markdown_tables:
            content = normalize_markdown_for_platform(content, supports_tables=False)

        payload = {
            "chat_type": 2 if group else 1,
            "chatid": target,
            "msgtype": "text",
            "text": {"content": content[:2048]},
        }
        result: dict = self.run(
            ["msg", "send_message", "--json",
             json.dumps(payload, ensure_ascii=False)],
            force_no_dry_run=True,
        )
        if uuid:
            result.setdefault("noop_uuid", uuid)
        return result

    def chat_message_reply(self, *, message_id: str, text: str = "",
                           title: str = "", uuid: str | None = None,
                           reply_in_thread: bool = False,
                           group: str | None = None,
                           user: str | None = None,
                           open_dingtalk_id: str | None = None,
                           msg_type: str | None = None,
                           media_id: str | None = None,
                           file_path: str | None = None) -> dict:
        """企微回复：原生无「回复/话题」概念，降级为向原会话重发（保持接口契约）。

        - ``reply_in_thread``：企微无话题概念，**忽略并告警**。
        - ``uuid``：企微 CLI 无幂等键，以 ``noop_uuid`` 回传原值。
        - 富媒体：企微 ``send_message`` 仅支持文本，**忽略并告警**降级纯文本。
        - 目标：企微 reply 需显式 ``group`` / ``user`` / ``open_dingtalk_id``
          （飞书由 message_id 推导，此处不推导）；三者皆空抛 ``ValueError``。
        - ``message_id`` 仅用于日志追踪，企微无原生引用字段。
        """
        target = group or user or open_dingtalk_id
        if not target:
            raise ValueError("wecom chat_message_reply 需提供 group / user / open_dingtalk_id 之一")
        if reply_in_thread:
            logger.warning("wecom 不支持 reply_in_thread，已忽略（消息直接发往会话）")
        if media_id or file_path or (msg_type and msg_type != "text"):
            logger.warning(
                "wecom 不支持 %s 回复消息类型，已降级为纯文本",
                msg_type or "富媒体",
            )
        content = text or title
        if not content:
            raise ValueError("chat_message_reply 需提供 text 或 title")
        # 企微 markdown 子集不支持 GFM 表格：按平台能力转换（supports_markdown_tables=False）
        if not self.supports_markdown_tables:
            content = normalize_markdown_for_platform(content, supports_tables=False)

        payload = {
            "chat_type": 2 if group else 1,
            "chatid": target,
            "msgtype": "text",
            "text": {"content": content[:2048]},
        }
        result: dict = self.run(
            ["msg", "send_message", "--json",
             json.dumps(payload, ensure_ascii=False)],
            force_no_dry_run=True,
        )
        if uuid:
            result.setdefault("noop_uuid", uuid)
        return result

    def chat_message_update(self, *, message_id: str, text: str = "",
                           title: str = "", group: str | None = None,
                           user: str | None = None) -> dict:
        """企微不支持更新已发送消息（原生无此能力）。

        返回空 dict，上层检测到不支持时应降级为非流式输出。
        """
        logger.warning("企微不支持 chat_message_update（流式输出降级为一次性发送）")
        return {}

    def media_upload(self, file_path: str, media_type: str = "image") -> str:
        """上传本地媒体文件。

        企微 ``send_message`` 仅支持文本，无法经 CLI 上传媒体；此处仅做存在性
        校验并返回路径作为「媒体引用」，保持接口契约（调用方如需发图片应换平台）。
        """
        if not file_path or not os.path.exists(file_path):
            raise ValueError(f"media_upload: 文件不存在 {file_path!r}")
        return file_path

    def download_media(self, *, media_id: str, message_id: str,
                       conversation_id: str, output_path: str) -> str:
        """下载聊天中的媒体文件到本地，返回写入的本地路径。

        企微 ``get_msg_media`` 返回文件内容（base64 编码）于 ``result.content[0].text``
        内层 JSON 的某字段；本方法抽取 base64 解码后落盘并校验非空。
        """
        if not media_id:
            raise ValueError("download_media 需提供 media_id")
        try:
            resp = self.run(
                ["msg", "get_msg_media", "--json",
                 json.dumps({"media_id": media_id})],
                force_no_dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            raise self._base_error_class()(f"wecom 下载媒体失败: {e}") from e

        b64: str | None = None
        if isinstance(resp, dict):
            for key in ("content", "media_data", "base64", "file_content", "data"):
                v = resp.get(key)
                if isinstance(v, str) and v:
                    b64 = v
                    break
        if not b64:
            raise self._base_error_class()(
                f"wecom 下载响应无 base64 内容: {json.dumps(resp, ensure_ascii=False)[:300]}")
        try:
            data = base64.b64decode(b64)
        except Exception as e:  # noqa: BLE001
            raise self._base_error_class()(f"wecom 媒体 base64 解码失败: {e}") from e
        out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
        os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise self._base_error_class()(f"wecom 下载文件为空: {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # 企微 CLI 不支持的能力（桩）
    # ------------------------------------------------------------------

    def doc_search(self, query: str, page_size: int = 10) -> list[dict]:
        """企微 CLI 不支持文档搜索。"""
        return []

    def doc_read(self, node_id: str, content_format: str = "markdown") -> dict:
        """企微 CLI 不支持文档读取。"""
        return {}

    def calendar_event_list(self, start: str = "", end: str = "") -> list[dict]:
        """企微 CLI 不支持日历事件查询（无对应子命令）。

        显式报错而非返回空列表，落实「门控工具应显式报错」决策——
        get_calendar_events 在工具层已对企微门控隐藏（platforms=["dingtalk"]），
        此处仅防御性兜底：被直接调用适配器时给出明确错误而非静默空结果。
        """
        raise IMAdapterUnsupportedTypeError("企微 CLI 不支持日历事件查询")

    def todo_task_create(self, title: str, executors: str,
                         due: str = "", priority: str = "") -> dict:
        """企微 CLI 不支持待办创建（无对应子命令）。

        显式报错而非返回空 dict，落实「门控工具应显式报错」决策——
        create_todo 在工具层已对企微门控隐藏（platforms=["dingtalk"]）。
        """
        raise IMAdapterUnsupportedTypeError("企微 CLI 不支持待办创建")
