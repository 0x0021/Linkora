#!/usr/bin/env python3
"""清理被污染的历史 AI 回复（history poisoning 止血脚本）。

根因：AI 自己产出的坏回复（含「由OWNER评估」「建议协助评估」「走正规」
「通过钉钉→工作台」等）被原样存入 messages 表，下一轮拼上下文时又被喂回
模型，模型照抄 → 自污染闭环。本脚本只删除 role=assistant 且命中坏特征签名的
回复，不动用户消息、不动系统消息、不动其它会话。

⚠️ 关键修复：运行时消息按账号隔离在**会话库** ``data/conversations/<platform>__<sha256>[:16].db``，
主库 ``data/linkora.db`` 只是历史残留/平台无关表的载体。原脚本只扫主库，等于对真正
喂给模型的会话库消息毫无作用。现改为**同时清理主库残留 + 遍历所有会话库**，并对每条
删除的消息连带清理磁盘孤儿图片（``data/tmp_images``）、维护 ``conversations.message_count``，
避免「坏回复清了但图片漏删 / 会话计数失真」的二次问题。

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
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "linkora.db")
CONV_ROOT = os.path.join(BASE, "data", "conversations")

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


def _con(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _purge_images(image_paths: list[str]) -> int:
    """连带清理磁盘孤儿图片。统一用主库路径作基准（data/tmp_images 相对主库 parent）。"""
    if not image_paths:
        return 0
    try:
        sys.path.insert(0, BASE)
        from src.memory.image_cleanup import purge_orphan_images
        return purge_orphan_images(DB, image_paths)
    except Exception as e:  # noqa: BLE001 — 清图失败不应拖垮主清理
        print(f"  [warn] 清理孤儿图片失败（已忽略）: {e}")
        return 0


def _purge_one(db_path: str, label: str, args: argparse.Namespace) -> tuple[int, int]:
    """清理单个库（主库或某会话库）。返回 (删除条数, 涉及会话数)。

    - 库文件不存在 / 无 messages 表 → 安全跳过（不创建空库、不报错）。
    - apply 时：删前收集 image_path 与 chat_id，删后维护 message_count、
      清 conversation_summaries、按需 wipe-chat，最后清孤儿图。
    """
    if not os.path.exists(db_path):
        print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {label}: 跳过（库不存在 {db_path}）")
        return 0, 0
    c = _con(db_path)
    cur = c.cursor()
    try:
        cur.execute(
            f"SELECT id, chat_id, image_path, substr(content, 1, 80) AS preview, length(content) AS clen "
            f"FROM messages WHERE role='assistant' AND ({WHERE}) ORDER BY chat_id, id"
        )
        bad = cur.fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            c.close()
            print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {label}: 跳过（无 messages 表）")
            return 0, 0
        c.close()
        raise
    chats = sorted({r["chat_id"] for r in bad})
    print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {label}: {db_path}")
    print(f"  命中坏 AI 回复: {len(bad)} 条，涉及会话: {len(chats)} 个")
    for r in bad:
        cid = (r["chat_id"] or "")[:24]
        print(f"  - id={r['id']} chat={cid}… len={r['clen']} :: {r['preview']!r}")

    if not args.apply:
        c.close()
        return len(bad), len(chats)

    # ---- apply ----
    image_paths = [r["image_path"] for r in bad if r["image_path"]]
    cur.execute(f"DELETE FROM messages WHERE role='assistant' AND ({WHERE})")
    deleted = cur.rowcount
    # 维护会话消息计数（避免 message_count 与真实消息数失真）
    for cid in chats:
        cur.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (cid,))
        cnt = cur.fetchone()[0]
        try:
            cur.execute("UPDATE conversations SET message_count=? WHERE chat_id=?", (cnt, cid))
        except sqlite3.OperationalError:
            pass  # 无 conversations 表则跳过计数维护
    # 清掉这些会话的摘要缓存（避免坏信息从 summary 二次注入）
    for cid in chats:
        try:
            cur.execute("DELETE FROM conversation_summaries WHERE chat_id=?", (cid,))
        except sqlite3.OperationalError:
            pass
    if args.wipe_chat:
        for cid in chats:
            cur.execute("SELECT image_path FROM messages WHERE chat_id=? AND image_path != ''", (cid,))
            image_paths.extend([r[0] for r in cur.fetchall()])
            cur.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
        print(f"  已额外清空 {len(chats)} 个会话的全部消息。")
    c.commit()
    c.close()
    removed = _purge_images(image_paths)
    print(f"  已删除 {deleted} 条坏 AI 回复；清理孤儿图片 {removed} 个。")
    return deleted, len(chats)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认 dry-run）")
    ap.add_argument("--wipe-chat", action="store_true",
                    help="命中会话的【所有】消息一并清空（仅 --apply 时生效）")
    args = ap.parse_args()

    total_del = 0
    total_chats = 0
    # 1) 主库残留（历史/平台无关表可能仍有 messages）
    d, ch = _purge_one(DB, "主库", args)
    total_del += d
    total_chats += ch
    # 2) 遍历所有按账号隔离的会话库（真正喂给模型的主力数据源）
    if os.path.isdir(CONV_ROOT):
        for fn in sorted(os.listdir(CONV_ROOT)):
            if fn.endswith(".db") and "__" in fn:
                path = os.path.join(CONV_ROOT, fn)
                d, ch = _purge_one(path, f"会话库 {fn}", args)
                total_del += d
                total_chats += ch
    else:
        print(f"（未找到会话库目录 {CONV_ROOT}，仅清理了主库）")

    if not args.apply:
        print("\n（dry-run，未做任何修改。加 --apply 执行删除；--wipe-chat 连会话其它消息一起清）")
    else:
        print(f"\n合计删除 {total_del} 条坏 AI 回复，涉及会话 {total_chats} 个。")


if __name__ == "__main__":
    main()
