#!/usr/bin/env python3
"""一次性维护脚本（2026-10-28）：split_text 重叠膨胀 / 字符硬切 / 块内空行缺陷修复后，
用 dingtalk_docs 缓存原文对「钉钉来源」的 KB 文档全量重分块 + 重嵌入。

- 复用与 doc_sync_scheduler 完全一致的入库管线：清洗(LLM 可用时) → split_text → upsert → 逐块嵌入。
- 其余来源（manual / upload / web）原文未落库，无法无损重分块，
  待各自内容更新时经正常入库路径自然重投。

用法：.venv/bin/python scripts/rechunk_dingtalk_kb.py [--dry-run]
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.paths import get_config_path  # noqa: E402
from src.memory.store_factory import get_store  # noqa: E402
from src.tools.utils import clean_document_for_rag, split_text  # noqa: E402
from src.kb.feishu_importer import _create_embedding_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rechunk_kb")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    no_llm = "--no-llm" in sys.argv
    cfg = load_config(str(get_config_path()))
    store = get_store()
    repo = store._kb_repo
    docs_repo = store._docs_repo

    rag_cfg = getattr(cfg, "rag", None)
    chunk_size = getattr(rag_cfg, "chunk_size", 800) if rag_cfg else 800
    chunk_overlap = getattr(rag_cfg, "chunk_overlap", 50) if rag_cfg else 50
    hard_max = getattr(rag_cfg, "chunk_hard_max", None) if rag_cfg else None
    enable_llm = bool(getattr(rag_cfg, "llm_clean_enabled", False)) if rag_cfg else False
    max_chars = getattr(rag_cfg, "llm_clean_max_chars", 8000) if rag_cfg else 8000

    llm_client = None
    if enable_llm and not no_llm:
        try:
            from src.llm.client import LLMClient

            llm_client = LLMClient(cfg.llm)
            log.info("LLM 清洗已启用（llm_clean_enabled=true）")
        except Exception as exc:
            log.warning("LLMClient 构造失败，回退正则预清洗: %s", exc)

    embed_client = _create_embedding_client(cfg)
    if embed_client is None or not embed_client.is_enabled:
        log.error("embedding 未启用，重嵌入无法进行，中止")
        return 1

    docs, total = repo.list_kb_documents(limit=1000)
    targets = [d for d in docs if d.get("source") == "dingtalk"]
    log.info("KB 文档共 %d 篇，其中钉钉来源 %d 篇待重分块%s",
             total, len(targets), "（dry-run）" if dry_run else "")

    ok = failed = 0
    for d in targets:
        doc_id, title, source_id = d["id"], d.get("title", ""), d.get("source_id", "")
        cached = docs_repo.get_dingtalk_doc(source_id) if source_id else None
        raw = (cached or {}).get("content", "")
        if not raw.strip():
            log.warning("[doc %s] %s：缓存原文为空，跳过", doc_id, title)
            failed += 1
            continue

        cleaned = clean_document_for_rag(
            raw, llm_client=llm_client, enable_llm=llm_client is not None, max_chars=max_chars,
        )
        chunks = split_text(cleaned, max_len=chunk_size, overlap=chunk_overlap, hard_max=hard_max)
        if not chunks:
            log.warning("[doc %s] %s：重分块结果为空，跳过（不动原分块）", doc_id, title)
            failed += 1
            continue

        if dry_run:
            log.info("[doc %s] %s：将产出 %d 块（dry-run 不写库）", doc_id, title, len(chunks))
            ok += 1
            continue

        try:
            kb_doc_id = repo.upsert_kb_document(
                title=title, doc_type=d.get("doc_type", "dingtalk"), source="dingtalk",
                chunks=chunks, source_id=source_id, url=d.get("url", ""), content=raw,
            )
        except Exception as exc:
            log.error("[doc %s] %s：upsert 失败: %s", doc_id, title, exc)
            failed += 1
            continue

        all_chunks = repo.list_kb_chunks(kb_doc_id)
        embed_failed = 0
        for chunk in all_chunks:
            emb = embed_client.embed_with_retry(chunk["content"])
            if emb:
                repo.update_chunk_embedding(chunk["id"], emb)
            else:
                embed_failed += 1
        log.info("[doc %s] %s：重投 %d 块，嵌入失败 %d", doc_id, title, len(all_chunks), embed_failed)
        ok += 1

    log.info("完成：成功 %d，失败/跳过 %d%s", ok, failed, "（dry-run）" if dry_run else "")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
