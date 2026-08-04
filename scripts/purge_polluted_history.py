#!/usr/bin/env python3
"""清理被污染的历史 AI 回复（history poisoning 止血脚本）。

根因：AI 自己产出的坏回复（含「由OWNER评估」「建议协助评估」「走正规」
「通过钉钉→工作台」等）被原样存入 messages 表，下一轮拼上下文时又被喂回
模型，模型照抄 → 自污染闭环。本脚本只删除 role=assistant 且命中坏特征签名的
回复，不动用户消息、不动系统消息、不动其它会话。

用法：
  python scripts/purge_polluted_history.py            # dry-run（默认，只看不删）
  python scripts/purge_polluted_history.py --apply    # 真正删除
  python scripts/purge_polluted_history.py --apply --wipe-chat  # 顺带清空命中会话的全部消息

安全：dry-run 不写库；--apply 前请先确认备份（data/backups/ 下已有自动备份）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "linkora.db")

# 仅针对 AI 自身坏回复的特征签名（变量无关，可随项目调整）。
# 关键：只命中「把自己名字写进评估/审批口吻」或「残句」，绝不命中正常
# 的「通过钉钉工作台走 OA 审批」类正确指引（那是合规行为）。
BAD_SIGS = [
    "由OWNER",
    "经OWNER",
    "建议联系OWNER",
    "联系OWNER（IT",
    "建议协助评估",
    "评估后走正规",
    "走正规采购渠道",
]

WHERE = " OR ".join(f"content LIKE '%{s}%'" for s in BAD_SIGS)


def con() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认 dry-run）")
    ap.add_argument("--wipe-chat", action="store_true",
                    help="命中会话的【所有】消息一并清空（仅 --apply 时生效）")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"DB 不存在: {DB}")
        return

    c = con()
    cur = c.cursor()

    # 1) 定位坏回复
    cur.execute(
        f"SELECT id, chat_id, role, substr(content, 1, 80) AS preview, length(content) AS clen "
        f"FROM messages WHERE role='assistant' AND ({WHERE}) ORDER BY chat_id, id"
    )
    bad = cur.fetchall()
    chats = sorted({r["chat_id"] for r in bad})
    print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] 库: {DB}")
    print(f"命中坏 AI 回复: {len(bad)} 条，涉及会话: {len(chats)} 个")
    for r in bad:
        print(f"  - id={r['id']} chat={r['chat_id'][:24]}… len={r['clen']} :: {r['preview']!r}")

    if not args.apply:
        print("\n（dry-run，未做任何修改。加 --apply 执行删除；--wipe-chat 连会话其它消息一起清）")
        c.close()
        return

    # 2) 删除坏回复
    cur.execute(f"DELETE FROM messages WHERE role='assistant' AND ({WHERE})")
    deleted = cur.rowcount
    # 3) 清掉这些会话的摘要缓存（避免坏信息从 summary 二次注入）
    for cid in chats:
        cur.execute("DELETE FROM conversation_summaries WHERE chat_id=?", (cid,))
    if args.wipe_chat:
        for cid in chats:
            cur.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
        print(f"已额外清空 {len(chats)} 个会话的全部消息。")
    c.commit()
    c.close()
    print(f"\n已删除 {deleted} 条坏 AI 回复，并清理 {len(chats)} 个会话的摘要缓存。")


if __name__ == "__main__":
    main()
