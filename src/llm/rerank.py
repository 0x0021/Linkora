"""BGE 本地离线重排（Phase 2 · P2-6）。

对 RAG 召回的候选文档做「**重排仅排序（reorder-only）**」：

- 不改变每条文档原有的 ``score`` 字段（置信度阈值判定、引文页脚展示
  沿用 bi-encoder 原始相似度，语义保持一致）；
- 仅调整 ``result["results"]`` 的**顺序**，让 cross-encoder 判定更相关的
  候选排在前面，提升注入 LLM 的知识质量；
- 默认关闭（``rerank_enabled=False``）；开启后 **lazy-load** ``CrossEncoder``，
  首次调用才加载，避免拖慢启动；
- 任意异常 / 模型缺失 / 离线加载失败 → **降级为原始顺序**（best-effort），
  绝不阻断主链路（RAG 注入、回复生成）。

设计依据：``docs/phase2_citation_confidence_design.md`` §6。
插入点：``style.retrieve_relevant_knowledge`` 拿到 ``result["results"]`` 之后、
``best_score`` 计算之前，加一层重排。

状态管理：使用 _RerankerState 类封装状态，避免 global 关键字。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class _RerankerState:
    """重排器状态（替代 global _reranker / _reranker_loaded）。"""

    def __init__(self) -> None:
        self._reranker = None
        self._reranker_loaded = False
        self._reranker_lock = threading.Lock()

    @property
    def reranker(self):
        return self._reranker

    @reranker.setter
    def reranker(self, value):
        self._reranker = value

    @property
    def reranker_loaded(self) -> bool:
        return self._reranker_loaded

    @reranker_loaded.setter
    def reranker_loaded(self, value: bool) -> None:
        self._reranker_loaded = value

    @property
    def reranker_lock(self) -> threading.Lock:
        return self._reranker_lock

    def clear(self) -> None:
        """清空状态（测试用）。"""
        with self._reranker_lock:
            self._reranker = None
            self._reranker_loaded = False


_reranker_state = _RerankerState()


def _clear_cache() -> None:
    """仅供测试使用：清空已加载的重排器与状态。"""
    _reranker_state.clear()


def _is_local_model_path(model: str) -> bool:
    return (
        model.startswith("./")
        or model.startswith("../")
        or model.startswith("~")
        or model.startswith("/")
    )


def get_reranker(model: str, offline: bool = False):
    """lazy 加载并返回一个 CrossEncoder 实例（缓存复用）。

    返回 CrossEncoder 实例；加载失败 / 依赖缺失时返回 ``None``。
    任何异常都被吞掉并返回 ``None``，调用方据此降级为原始顺序。
    """
    state = _reranker_state

    # 已成功加载 → 直接返回缓存
    if state.reranker is not None:
        return state.reranker

    # 已尝试加载但失败过 → 不再重试，直接降级（避免每次请求都触发异常路径）
    if state.reranker_loaded:
        return None

    with state.reranker_lock:
        # double-check：锁内再次确认（可能另一线程已加载）
        if state.reranker is not None:
            return state.reranker
        if state.reranker_loaded:
            return None

        state.reranker_loaded = True
        try:
            from sentence_transformers import CrossEncoder

            is_local = _is_local_model_path(model)
            if offline or is_local:
                # 纯离线 / 本地路径：禁止联网，仅用本地缓存
                import os
                os.environ["HF_HUB_OFFLINE"] = "1"
                logger.info("正在加载本地重排模型: %s（离线模式）", model)
                state.reranker = CrossEncoder(model, local_files_only=True)
            else:
                # 在线：允许按需下载
                logger.info("正在加载重排模型: %s", model)
                state.reranker = CrossEncoder(model, local_files_only=False)

            logger.info("重排模型加载完成: %s", model)
            return state.reranker
        except Exception as e:  # noqa: BLE001 - 降级兜底，不向上抛
            logger.error("[rerank] 加载重排模型失败，降级为原始顺序: %s", e)
            state.reranker = None
            return None


def rerank(
    query: str,
    results: list[dict],
    *,
    model: str = "BAAI/bge-reranker-base",
    offline: bool = False,
    top_k: int | None = None,
    timeout: float | None = None,
) -> list[dict]:
    """对召回结果做 reorder-only 重排。

    :param query: 用户查询
    :param results: kb_search 返回的 ``result["results"]`` 列表（每项含 ``content`` 等）
    :param model: CrossEncoder 模型名（默认 BAAI/bge-reranker-base）
    :param offline: 是否纯离线加载（local_files_only）
    :param top_k: 重排后保留前 N 条；``None`` 保留全部（仅排序）
    :param timeout: 重排超时（秒），超时即降级为原始顺序
    :returns: 重排（并可选截断）后的列表；任何异常返回原始 ``results``
    """
    if not results:
        return results

    # 副本：避免改动调用方持有的原始列表顺序之外的内容
    work = list(results)
    n = len(work)

    if top_k is not None:
        # 重排候选窗口：最多取 top_k 条参与重排（其余保持尾部原序），控制算力
        candidate = work[: max(1, min(top_k, n))]
    else:
        candidate = work

    reranker = get_reranker(model, offline=offline)
    if reranker is None:
        # 加载失败 → 原始顺序
        return _maybe_truncate(work, top_k)

    try:
        if timeout and timeout > 0:
            deadline = time.monotonic() + timeout
            pairs = [(query, (r.get("content") or "")) for r in candidate]
            # 预估超时：若单条预测已明显超期则直接降级（避免阻塞主链路）
            if time.monotonic() > deadline:
                return _maybe_truncate(work, top_k)
            scores = reranker.predict(pairs, show_progress_bar=False)  # type: ignore[attr-defined]
        else:
            pairs = [(query, (r.get("content") or "")) for r in candidate]
            scores = reranker.predict(pairs, show_progress_bar=False)  # type: ignore[attr-defined]

        # 关联重排分并降序排序（保留原始 score 字段不变）
        indexed = sorted(
            zip(candidate, [float(s) for s in scores], strict=True),
            key=lambda x: x[1],
            reverse=True,
        )
        reordered = [item for item, _ in indexed]

        if top_k is not None:
            # 候选窗口重排后，与未参与重排的尾部拼接
            return reordered[: max(1, min(top_k, n))] + work[len(candidate):]
        return reordered
    except Exception as e:  # noqa: BLE001 - 重排失败绝不阻断 RAG 主链路
        logger.warning("[rerank] 重排失败，降级为原始顺序: %s", e)
        return _maybe_truncate(work, top_k)


def _maybe_truncate(results: list[dict], top_k: int | None) -> list[dict]:
    if top_k is None:
        return results
    n = len(results)
    return results[: max(1, min(top_k, n))]
