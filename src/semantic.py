"""Phase 2 语义路由：复用本地 embedding 对消息向量化，与工具/技能的预计算向量比对。

设计要点：
- 子串匹配作为「高精度快路径」保留（见 agent._keyword_match_tool_names /
  SkillRouter.match_by_intent）；本模块负责「无精确命中时」的语义相似度兜底，
  覆盖同义改写 / 错别字 / 口语化表达（解决分析文档 2.6）。
- 仅消息侧实时编码；工具/技能向量预计算并按 (name, 语义文本签名) 缓存，
  文本变化自动重算（技能热加载无需手动失效），之后零成本。
- 降级：embedding 不可用（未启用 / kb_search 未注册 / 编码失败）时，
  所有函数安全返回空/None，上层行为回退到 Phase 1 的纯子串匹配。

状态管理：使用 _SemanticState 类封装客户端和缓存状态，避免 global 关键字。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 语义命中阈值（余弦相似度，0~1）。高于此值视为语义相关。
SEMANTIC_TOOL_THRESHOLD = 0.42
SEMANTIC_SKILL_THRESHOLD = 0.40


class _SemanticState:
    """语义路由状态（替代 global _client）。"""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._cache_lock = threading.Lock()
        self._tool_cache: dict[str, tuple[str, list[float]]] = {}
        self._skill_cache: dict[str, tuple[str, list[float]]] = {}

    @property
    def client(self) -> Any | None:
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    @property
    def tool_cache(self) -> dict[str, tuple[str, list[float]]]:
        return self._tool_cache

    @property
    def skill_cache(self) -> dict[str, tuple[str, list[float]]]:
        return self._skill_cache

    @property
    def cache_lock(self) -> threading.Lock:
        return self._cache_lock


_semantic_state = _SemanticState()


# 向后兼容：模块级属性别名（供测试直接访问内部状态）
# 使用 weakref 或直接引用保持与旧测试的兼容性
class _CacheProxy:
    """缓存字典代理，支持 dict 操作和 len/contains。"""
    
    def __init__(self, cache_attr: str):
        self._cache_attr = cache_attr
    
    def __getitem__(self, key: str):
        return getattr(_semantic_state, self._cache_attr)[key]
    
    def __setitem__(self, key: str, value):
        getattr(_semantic_state, self._cache_attr)[key] = value
    
    def __delitem__(self, key: str):
        del getattr(_semantic_state, self._cache_attr)[key]
    
    def __contains__(self, key: str) -> bool:
        return key in getattr(_semantic_state, self._cache_attr)
    
    def __len__(self) -> int:
        return len(getattr(_semantic_state, self._cache_attr))
    
    def clear(self) -> None:
        getattr(_semantic_state, self._cache_attr).clear()


class _ClientProxy:
    """客户端代理，支持读写并同步到 _semantic_state。"""
    
    def __init__(self):
        self._value = None
    
    def __getattr__(self, name: str):
        client = self._value
        if client is None:
            raise AttributeError(f"client is None")
        return getattr(client, name)
    
    def __setattr__(self, name: str, value):
        if name == '_value':
            super().__setattr__(name, value)
            # 同步到 _semantic_state
            _semantic_state._client = value
        else:
            client = self._value
            if client is None:
                raise AttributeError(f"client is None")
            setattr(client, name, value)


# 创建模块级兼容属性（每个属性使用独立的代理实例）
_tool_cache_proxy = _CacheProxy('_tool_cache')
_skill_cache_proxy = _CacheProxy('_skill_cache')
_cache_lock_proxy = _CacheProxy('_cache_lock')
_client_proxy = _ClientProxy()


# 将兼容属性直接绑定到模块（通过修改 __dict__）
import sys
_current_module = sys.modules[__name__]
_current_module.__dict__['_client'] = _client_proxy
_current_module.__dict__['_tool_cache'] = _tool_cache_proxy
_current_module.__dict__['_skill_cache'] = _skill_cache_proxy
_current_module.__dict__['_cache_lock'] = _cache_lock_proxy


def set_embedding_client(client: Any) -> None:
    """绑定共享 EmbeddingClient（main.py 在注册 kb_search 后调用）。"""
    _semantic_state.client = client


def get_embedding_client() -> Any | None:
    """返回共享 EmbeddingClient，未绑定返回 None。"""
    return _semantic_state.client


def invalidate_tools() -> None:
    """清空工具向量缓存（embedding 配置变更时调用）。"""
    with _semantic_state.cache_lock:
        _semantic_state.tool_cache.clear()


def invalidate_skills() -> None:
    """清空技能向量缓存。"""
    with _semantic_state.cache_lock:
        _semantic_state.skill_cache.clear()


def invalidate_all() -> None:
    invalidate_tools()
    invalidate_skills()


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """余弦相似度，任一为空或零向量返回 0.0。"""
    if not a or not b:
        return 0.0
    try:
        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))
    except Exception as _exc:
        logger.debug(f"cosine: swallowed exception: {_exc}")
        return 0.0


def _embed(text: str) -> list[float] | None:
    """用共享客户端编码文本，失败返回 None。"""
    client = _semantic_state.client
    if client is None or not getattr(client, "enabled", False):
        return None
    try:
        return client.embed(text)
    except Exception as e:
        logger.warning("[语义路由] 文本向量化失败: %s", e)
        return None


def _cached_vector(cache: dict, name: str, signature: str, text: str) -> list[float] | None:
    """返回文本向量，命中缓存则直接返回，否则编码并写入。"""
    with _semantic_state.cache_lock:
        entry = cache.get(name)
        if entry is not None and entry[0] == signature:
            return entry[1]
    vec = _embed(text)
    if vec:
        with _semantic_state.cache_lock:
            cache[name] = (signature, vec)
    return vec


def match_tools(message_vec: list[float] | None,
                tool_texts: list[tuple[str, str]],
                threshold: float = SEMANTIC_TOOL_THRESHOLD) -> dict[str, float]:
    """返回与消息向量语义相似度 >= threshold 的工具名及相似度。

    tool_texts: [(tool_name, semantic_text), ...]，语义文本由调用方构造
    （通常 = name + description + effective_intent_keywords）。

    返回 {tool_name: similarity}，未启用 embedding 或 message_vec 为空时返回 {}。
    """
    if message_vec is None or _semantic_state.client is None or not getattr(_semantic_state.client, "enabled", False):
        return {}
    out: dict[str, float] = {}
    for name, text in tool_texts:
        sig = str(hash(text))
        vec = _cached_vector(_semantic_state.tool_cache, name, sig, text)
        if not vec:
            continue
        sim = cosine(message_vec, vec)
        if sim >= threshold:
            out[name] = round(sim, 4)
    return out


def score_skill(message_vec: list[float] | None,
                name: str,
                semantic_text: str) -> float | None:
    """返回技能语义相似度（0~1），embedding 不可用时返回 None（调用方回退关键词）。"""
    if message_vec is None or _semantic_state.client is None or not getattr(_semantic_state.client, "enabled", False):
        return None
    sig = str(hash(semantic_text))
    vec = _cached_vector(_semantic_state.skill_cache, name, sig, semantic_text)
    if not vec:
        return None
    return round(cosine(message_vec, vec), 4)


def warmup_tools(tool_texts: list[tuple[str, str]]) -> None:
    """预计算工具向量（启动时调用，避免首条消息延迟）。失败静默忽略。"""
    if _semantic_state.client is None or not getattr(_semantic_state.client, "enabled", False):
        return
    for name, text in tool_texts:
        sig = str(hash(text))
        _cached_vector(_semantic_state.tool_cache, name, sig, text)
