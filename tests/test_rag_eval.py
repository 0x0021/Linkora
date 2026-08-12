"""RAG 评测闭环（Phase A：检索质量）。

两条互补指标：

1. **Chunk Coverage（离线必跑，零依赖）**
   验证「语义分块 + 正则预清洗」是否把每个标准答案**完整保留在单个
   chunk 内**。这是分块质量最该守的底线——若答案被硬截断到两个 chunk，
   再好的 embedding 也救不回来。CI 永远可跑。

2. **Recall@k（embedding 可选）**
   探测 EmbeddingClient 可用性（离线本地模型或已配 API key）。可用时把
   全部 chunk 入库内存 faiss 索引，对每个 query 检索 top-k，看 golden
   所在 chunk 是否命中。无可用 embedding 时 graceful skip，不阻塞 CI。

忠实度（faithfulness）需 LLM 生成 + 评判、噪声大，列为 Phase B 后续。

运行：
    pytest tests/test_rag_eval.py -s          # CI 模式（含覆盖率打印）
    python -m tests.test_rag_eval             # 独立报告（有 embedding 则附 Recall）
"""
from __future__ import annotations

import logging
import re

import pytest

from src.tools.utils import clean_document_for_rag, split_text
from tests.rag_eval_dataset import RAG_EVAL_CORPUS, RAG_EVAL_QUERIES

logger = logging.getLogger(__name__)

TOP_K = 3
# 防回归阈值：合成集语义清晰，离线分块必须 100% 保住答案；
# 语义检索 Recall@k 至少 0.8（低于即提示分块/检索退化）。
CHUNK_COVERAGE_MIN = 1.0
RECALL_MIN = 0.8


def _norm_ws(s: str) -> str:
    """去空白归一化，使「压缩空白」类预清洗不影响子串判定。"""
    return re.sub(r"\s+", "", s or "")


def _build_chunks() -> dict[str, list[str]]:
    """对全部语料跑「正则预清洗 + 语义分块」，返回 {doc_id: [chunk, ...]}。"""
    out: dict[str, list[str]] = {}
    for doc in RAG_EVAL_CORPUS:
        cleaned = clean_document_for_rag(doc["content"], enable_llm=False)
        chunks = split_text(cleaned, max_len=500, overlap=50)
        out[doc["doc_id"]] = chunks
    return out


def _chunk_coverage(doc_chunks: dict[str, list[str]]):
    """返回 (hits, total, details)。"""
    hits = 0
    details = []
    for q in RAG_EVAL_QUERIES:
        chunks = doc_chunks.get(q["doc_id"], [])
        golden_n = _norm_ws(q["golden_text"])
        found = any(golden_n in _norm_ws(c) for c in chunks)
        if found:
            hits += 1
        details.append({
            "query": q["query"],
            "doc_id": q["doc_id"],
            "coverage": found,
            "n_chunks": len(chunks),
        })
    return hits, len(RAG_EVAL_QUERIES), details


def _build_eval_embedder():
    """探测可用 embedding：仅接受离线本地模型或已配 API key，避免触发联网下载。"""
    try:
        from src.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.info("[RAG Eval] 加载配置失败，跳过 embedding 评测: %s", exc)
        return None
    ec = getattr(cfg, "embedding", None)
    if not ec or not getattr(ec, "enabled", False):
        return None
    provider = getattr(ec, "provider", "")
    # 非离线 local 模型首次会联网下载，eval 场景直接跳过以免卡住 CI
    if provider == "local" and not getattr(ec, "offline", False):
        return None
    try:
        from src.memory.embedding import EmbeddingClient
        client = EmbeddingClient(ec)
        probe = client.embed("探针测试")
        return client if probe else None
    except Exception as exc:
        logger.info("[RAG Eval] embedding 初始化失败，跳过: %s", exc)
        return None


def _recall_at_k(doc_chunks: dict[str, list[str]], client, top_k: int = TOP_K):
    """把全部 chunk 入库内存 faiss，对每个 query 检索 top-k，返回 (hits, total)。"""
    from src.memory.vector_index import VectorIndex

    # 扁平化 chunk 列表并保留 (doc_id, local_idx)
    all_chunks: list[tuple[str, int, str]] = []
    for doc in RAG_EVAL_CORPUS:
        for i, c in enumerate(doc_chunks[doc["doc_id"]]):
            all_chunks.append((doc["doc_id"], i, c))

    texts = [t for _, _, t in all_chunks]
    embs = client.embed_batch(texts)
    real = [(gid, emb) for gid, emb in enumerate(embs) if emb]
    if not real:
        return 0, len(RAG_EVAL_QUERIES)

    dim = len(real[0][1])
    vi = VectorIndex(dim)
    for gid, emb in real:
        vi.add(gid, emb)

    hits = 0
    evaluated = 0
    for _qi, q in enumerate(RAG_EVAL_QUERIES):
        chunks = doc_chunks[q["doc_id"]]
        golden_n = _norm_ws(q["golden_text"])
        local = next((i for i, c in enumerate(chunks) if golden_n in _norm_ws(c)), None)
        if local is None:
            continue
        golden_gid = next(
            (g for g, (d, li, _) in enumerate(all_chunks)
             if d == q["doc_id"] and li == local),
            None,
        )
        if golden_gid is None or golden_gid not in {g for g, _ in real}:
            continue
        q_emb = client.embed(q["query"])
        if not q_emb:
            continue
        evaluated += 1
        top_ids = {r[0] for r in vi.search(q_emb, top_k=top_k)}
        if golden_gid in top_ids:
            hits += 1
    return hits, evaluated


def test_rag_chunk_coverage_offline():
    """离线必跑：语义分块须把每个标准答案完整保留在单个 chunk 内。"""
    doc_chunks = _build_chunks()
    hits, total, details = _chunk_coverage(doc_chunks)
    miss = [d for d in details if not d["coverage"]]
    rate = hits / total if total else 0
    print(f"\n[RAG Eval] Chunk Coverage: {hits}/{total} = {rate:.2%}")
    for d in details:
        flag = "OK " if d["coverage"] else "MISS"
        print(f"  [{flag}] {d['query']}  (doc={d['doc_id']}, chunks={d['n_chunks']})")
    assert rate >= CHUNK_COVERAGE_MIN, (
        f"分块切碎了 {len(miss)} 个标准答案（应 100% 完整保留）：{miss}"
    )


def test_rag_recall_at_k():
    """语义检索 Recall@k：有可用 embedding 时跑，否则 skip。"""
    client = _build_eval_embedder()
    if client is None:
        pytest.skip("无可用 embedding（未配置离线本地模型或 API key），跳过 Recall@k 评测")

    doc_chunks = _build_chunks()
    hits, total = _recall_at_k(doc_chunks, client, top_k=TOP_K)
    rate = hits / total if total else 0
    print(f"\n[RAG Eval] Recall@{TOP_K}: {hits}/{total} = {rate:.2%}")
    assert rate >= RECALL_MIN, f"Recall@{TOP_K} 仅 {rate:.2%}，低于 {RECALL_MIN:.2%} 基线"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dc = _build_chunks()
    hits, total, details = _chunk_coverage(dc)
    rate = hits / total if total else 0
    print(f"[RAG Eval] Chunk Coverage: {hits}/{total} = {rate:.2%}")
    for d in details:
        print(f"  [{'OK ' if d['coverage'] else 'MISS'}] {d['query']} (chunks={d['n_chunks']})")

    client = _build_eval_embedder()
    if client is not None:
        rh, rt = _recall_at_k(dc, client, top_k=TOP_K)
        rrate = rh / rt if rt else 0
        print(f"[RAG Eval] Recall@{TOP_K}: {rh}/{rt} = {rrate:.2%}")
    else:
        print("[RAG Eval] Recall@k 跳过：无可用 embedding（配置 offline 本地模型或 API key 后启用）")
