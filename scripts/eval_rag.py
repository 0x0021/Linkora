#!/usr/bin/env python3
"""RAG 评估闭环（Feature D）——离线评测知识库召回质量。

读取 golden_qa.json（问题 + 期望命中关键词），对每条问题做向量检索，
统计：
  - hit@1：top-1 相似度 >= 阈值的比例（衡量「能不能找到」）
  - keyword_coverage：期望关键词出现在 top-k 内容中的比例（衡量「找得准不准」）
  - avg_top1_sim：平均 top-1 相似度

用法：
  python scripts/eval_rag.py [--golden scripts/golden_qa.json] [--db-path PATH] [--top-k 3] [--threshold 0.5] [--report out.json]

说明：需要可用 embedding（config.yaml 的 embedding 段启用）。embedding 不可用时
脚本打印提示并退出（不报错），方便在无网/离线环境跳过评测。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.config import load_config
from src.memory.sqlite_store import SQLiteStore
from src.memory.embedding import EmbeddingClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=os.path.join(ROOT, "scripts", "golden_qa.json"))
    ap.add_argument("--db-path", default=None, help="数据库路径（默认取 config.db.path，即 linkora.db）")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    if not os.path.exists(args.golden):
        print(f"[eval] golden 文件不存在: {args.golden}")
        print("[eval] 请先创建 scripts/golden_qa.json，格式见 README/脚本注释。")
        return

    with open(args.golden, "r", encoding="utf-8") as f:
        golden = json.load(f)
    if not isinstance(golden, list):
        print("[eval] golden 必须是 list[{question, expect_keyword}]")
        return

    config = load_config()
    if not config.embedding.enabled:
        print("[eval] embedding 未启用，跳过评测（无网/离线环境）")
        return

    embedding = EmbeddingClient(config.embedding)
    db_path = args.db_path if (args.db_path and os.path.exists(args.db_path)) else config.db.path
    store = SQLiteStore(db_path)

    hit1 = 0
    cov = 0
    sims = []
    rows = []
    t0 = time.perf_counter()
    for item in golden:
        q = (item.get("question") or "").strip()
        kw = (item.get("expect_keyword") or "").strip()
        if not q:
            continue
        try:
            emb = embedding.embed(q)
        except Exception as e:
            print(f"[eval] 向量化失败（{q[:20]}）: {e}")
            emb = None
        if not emb:
            rows.append({"question": q, "error": "embed_failed"})
            continue
        results = store._kb_repo.search_kb(emb, top_k=args.top_k, query_text=q)
        top1 = results[0]["similarity"] if results else 0.0
        sims.append(top1)
        if top1 >= args.threshold:
            hit1 += 1
        kw_hit = bool(kw) and any(kw in (r.get("content") or "") for r in results)
        if kw_hit:
            cov += 1
        rows.append({
            "question": q,
            "expect_keyword": kw,
            "top1_sim": round(top1, 3),
            "hit@1": top1 >= args.threshold,
            "keyword_hit": kw_hit,
            "top_k_titles": [r.get("title") for r in results[: args.top_k]],
        })

    n = len(rows)
    avg_sim = (sum(sims) / len(sims)) if sims else 0.0
    summary = {
        "total": n,
        "hit@1": round(hit1 / n, 3) if n else 0.0,
        "keyword_coverage": round(cov / n, 3) if n else 0.0,
        "avg_top1_sim": round(avg_sim, 3),
        "threshold": args.threshold,
        "top_k": args.top_k,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    print("\n========== RAG 评估报告 ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for r in rows:
        flag = "✅" if r.get("hit@1") else ("⚠️ " if r.get("keyword_hit") else "❌")
        print(f"  {flag} {r['question'][:30]:<32} top1={r.get('top1_sim', 0):.2f} kw={r.get('keyword_hit', False)}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "details": rows}, f, ensure_ascii=False, indent=2)
        print(f"\n[eval] 报告已写入 {args.report}")


if __name__ == "__main__":
    main()
