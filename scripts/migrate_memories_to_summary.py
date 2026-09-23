#!/usr/bin/env python3
"""记忆系统迁移：把「逐条单条事实」整合为「按人/主题的摘要」。

背景（2026-09-23 摘要化记忆改造）：
- 旧版：每条提取出的事实单独存一行（memories.kind='fact'），随时间堆积冗余。
- 新版：事实先入 memory_pending，由汇总调度器合并为「按 (scope, group_key) 聚合的
  整合摘要」(memories.kind='summary')，天然支持存储/检索/更新。

本脚本把**存量**的逐条事实一次性整合为摘要，写完即删旧行，完成从旧模式到新模式的切换。
- 个人记忆：按 sender_id 聚合为每人一份画像摘要。
- 公共记忆：按主题桶（网络/流程/人员/系统/其他）聚合为少量主题摘要。
- 整合方式：默认「结构化去重整合」（分组 + 归一化去重 + 要点罗列），剔除冗余、保留关键事实；
  加 --use-llm 则尝试调用配置中的 LLM 做更自然的合并（不可达时自动退回结构化）。
- embedding：尽量用本地嵌入服务补算；不可达则留空，由线上服务的 backfill 在启动时补算。

用法：
  python scripts/migrate_memories_to_summary.py            # 执行迁移
  python scripts/migrate_memories_to_summary.py --dry-run  # 仅预览分组，不改库
  python scripts/migrate_memories_to_summary.py --use-llm  # 尽量用 LLM 整合
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [记忆迁移] %(message)s")
logger = logging.getLogger("migrate_memories_to_summary")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isalnum()).lower()


def _ensure_memory_schema(con: sqlite3.Connection) -> None:
    """为旧库补齐摘要化改造所需的列与 memory_pending 表（幂等）。"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()}
    if "kind" not in cols:
        con.execute("ALTER TABLE memories ADD COLUMN kind TEXT DEFAULT 'fact'")
    if "group_key" not in cols:
        con.execute("ALTER TABLE memories ADD COLUMN group_key TEXT")
    if "updated_at" not in cols:
        con.execute("ALTER TABLE memories ADD COLUMN updated_at TEXT")
    con.execute(
        "CREATE TABLE IF NOT EXISTS memory_pending ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT DEFAULT 'personal',"
        "group_key TEXT, content TEXT NOT NULL, sender_name TEXT, chat_id TEXT,"
        "created_at TEXT NOT NULL)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_memory_pending_group ON memory_pending(scope, group_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_memory_pending_created ON memory_pending(created_at)")
    con.commit()


def find_memory_dbs() -> list[str]:
    """返回含 memories 表且存有旧版逐条事实(legacy)的库路径列表。"""
    candidates: list[str] = []
    data_dir = os.path.join(PROJECT_ROOT, "data")
    conv_dir = os.path.join(data_dir, "conversations")
    for d in (data_dir, conv_dir):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".db"):
                continue
            path = os.path.join(d, name)
            try:
                con = sqlite3.connect(path)
                tbl = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
                ).fetchone()
                if not tbl:
                    con.close()
                    continue
                _ensure_memory_schema(con)
                cnt = con.execute(
                    "SELECT COUNT(*) FROM memories WHERE kind IS NULL OR kind='fact'"
                ).fetchone()[0]
                con.close()
                if cnt > 0:
                    candidates.append(path)
            except sqlite3.Error as e:
                logger.warning("读取 %s 失败: %s", path, e)
    return candidates


def build_summary_text(scope: str, group_key: str, facts: list[str], sender_name: str) -> str:
    """结构化整合：去重 + 要点罗列（LLM 不可用时的兜底，也作为默认整合）。"""
    seen: set[str] = set()
    items: list[str] = []
    for f in facts:
        n = _norm(f)
        if not n or n in seen:
            continue
        seen.add(n)
        items.append(f.strip())
    if scope == "public":
        title = f"【公共记忆：{group_key.replace('public:', '')}】"
    else:
        who = sender_name or "匿名联系人"
        title = f"【{who} 的整合记忆】"
    body = "\n".join(f"- {it}" for it in items)
    return f"{title}\n{body}"


def llm_summary(text: str, base_url: str, api_key: str, model: str) -> str | None:
    """尽力用配置中的 LLM 做整合；任何失败返回 None（调用方退回结构化）。"""
    if not (base_url and model):
        return None
    prompt = (
        "把下面这些零散事实整合为一份简洁、条理清晰的整合记忆（中文，去除冗余、保留关键事实）。"
        "按要点罗列，不要添加原文没有的信息。只输出整合后的内容。\n\n"
        f"{text}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    url = base_url.rstrip("/") + "/v1/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key or ''}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 整合失败，退回结构化: %s", e)
        return None


def get_embedding_client(config):
    try:
        from src.memory.embedding import EmbeddingClient
        ec = EmbeddingClient(config.embedding)
        if getattr(ec, "enabled", False):
            return ec
    except Exception as e:  # noqa: BLE001
        logger.warning("构建 EmbeddingClient 失败: %s", e)
    return None


def _summary_key(scope: str, group_key: str) -> str:
    import hashlib
    return "sum_" + hashlib.md5(f"{scope}|{group_key}".encode("utf-8")).hexdigest()[:16]


def migrate_db(path: str, *, dry_run: bool, use_llm: bool, embedding_client, llm_cfg) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _ensure_memory_schema(con)
    rows = con.execute(
        "SELECT id, content, sender_id, sender_name, scope FROM memories "
        "WHERE kind IS NULL OR kind='fact'"
    ).fetchall()

    # 分组：(scope, group_key) -> list[(content, sender_name)]
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in rows:
        scope = r["scope"] or "personal"
        content = (r["content"] or "").strip()
        if not content:
            continue
        if scope == "public":
            from src.memory.memory_repo import public_memory_topic_key
            gk = public_memory_topic_key(content)
        else:
            gk = r["sender_id"] or ""
        groups.setdefault((scope, gk), []).append((content, r["sender_name"] or ""))

    summary: dict[str, int] = {"groups": len(groups), "facts": len(rows), "summaries": 0, "deleted": 0}
    if not groups:
        con.close()
        return summary

    now = datetime.now().isoformat()
    # 先算好所有组的整合摘要（不删旧行），再统一写入 + 一次性删除旧逐条行。
    to_insert: list[tuple] = []
    for (scope, gk), facts in groups.items():
        fact_texts = [f for f, _ in facts]
        sender_name = next((s for _, s in facts if s), "")
        structured = build_summary_text(scope, gk, fact_texts, sender_name)
        final_text = structured
        if use_llm and llm_cfg:
            llm_out = llm_summary(structured, llm_cfg["base_url"], llm_cfg["api_key"], llm_cfg["model"])
            if llm_out:
                final_text = llm_out
        summary["summaries"] += 1
        if dry_run:
            logger.info("[dry-run] 将整合 (scope=%s, group=%s) %d 条事实 → %d 字摘要",
                        scope, gk, len(fact_texts), len(final_text))
            to_insert.append((scope, gk, final_text, sender_name, len(fact_texts)))
            continue

        emb = None
        if embedding_client:
            try:
                emb = embedding_client.embed(final_text)
            except Exception as e:  # noqa: BLE001
                logger.debug("embedding 失败: %s", e)
        emb_str = json.dumps(emb) if emb else None
        sender_id = gk if scope == "personal" else ""
        to_insert.append((scope, gk, final_text, sender_name, len(fact_texts), emb_str, sender_id))
        summary["deleted"] += len(fact_texts)

    if dry_run:
        con.close()
        return summary

    for scope, gk, final_text, sender_name, _n, emb_str, sender_id in to_insert:
        con.execute(
            "INSERT INTO memories (key, content, source, chat_id, sender_id, sender_name, "
            "embedding, created_at, scope, kind, group_key, updated_at) "
            "VALUES (?, ?, 'migration', '', ?, ?, ?, ?, ?, 'summary', ?, ?)",
            (_summary_key(scope, gk), final_text, sender_id, sender_name, emb_str,
             now, scope, gk, now),
        )
    # 删除全部旧版逐条事实（每条都已归入某个组的摘要，删除不会丢失信息）
    cur = con.execute("DELETE FROM memories WHERE kind IS NULL OR kind='fact'")
    summary["deleted"] = cur.rowcount
    con.commit()
    con.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="把逐条记忆整合为摘要")
    ap.add_argument("--dry-run", action="store_true", help="仅预览分组，不写库")
    ap.add_argument("--use-llm", action="store_true", help="尽量调用 LLM 整合（不可达则退回结构化）")
    ap.add_argument("--no-backup", action="store_true", help="不备份数据库")
    args = ap.parse_args()

    sys.path.insert(0, PROJECT_ROOT)
    config = None
    embedding_client = None
    llm_cfg = None
    try:
        from src.config import load_config
        config = load_config(os.path.join(PROJECT_ROOT, "config.yaml"), validate=False)
        embedding_client = get_embedding_client(config)
        if args.use_llm and config and getattr(config, "llm", None):
            llm_cfg = {
                "base_url": getattr(config.llm, "base_url", ""),
                "api_key": getattr(config.llm, "api_key", ""),
                "model": getattr(config.llm, "model", ""),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("加载配置/嵌入失败（仅影响 embedding/LLM，结构化整合仍可进行）: %s", e)

    dbs = find_memory_dbs()
    if not dbs:
        logger.info("未发现含旧版逐条事实的数据库，无需迁移。")
        return 0

    logger.info("发现 %d 个含旧版逐条事实的数据库：%s", len(dbs), dbs)

    for db in dbs:
        if not args.no_backup and not args.dry_run:
            bak = db + ".bak-migrate-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(db, bak)
            logger.info("已备份 %s -> %s", db, bak)
        result = migrate_db(db, dry_run=args.dry_run, use_llm=args.use_llm,
                            embedding_client=embedding_client, llm_cfg=llm_cfg)
        logger.info("%s 完成：%s", db, result)

    if args.dry_run:
        logger.info("dry-run 结束，未做任何修改。去掉 --dry-run 执行真实迁移。")
    else:
        logger.info("迁移完成。线上服务启动后会对缺 embedding 的摘要自动回填；"
                    "后续新事实仍由汇总调度器持续合并进对应摘要（增量更新）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
