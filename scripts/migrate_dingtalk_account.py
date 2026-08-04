#!/usr/bin/env python3
"""钉钉账号隔离修正迁移：把早期 bug 产生的字面量账号 ``dingtalk:corpId`` 迁移到真实
corpId（``dingtalk:ding9888...``）。

背景
----
`_resolve_dingtalk` 早期用正则匹配到 JSON 键名 ``corpId`` 而非值，导致
``resolve_account_id("dingtalk")`` 返回字面量 ``dingtalk:corpId``。该账号串同时决定了：

  * per-account 会话库文件名：``data/conversations/dingtalk__<sha256(account_id)[:16]>.db``
  * 图片物理目录：``data/tmp_images/dingtalk/<account_id_dir>/...``

修正 ``_resolve_dingtalk`` 后会返回真实 corpId，账号哈希随之改变，历史数据（会话库 +
图片路径 + 磁盘目录）落到新的账号。若修复后该账号已产生新数据则非空，本脚本把旧账号数据
**合并**进新账号（保留新账号既有数据），保证修复后历史可读。

幂等：dry-run 默认；``--apply`` 才落盘。重复运行安全（旧库已不存在 / 路径已是新账号则跳过）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys

# 让脚本可直接以仓库根目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.memory.account_identity import resolve_account_id  # noqa: E402
from src.image_path import account_id_dir, is_new_image_path  # noqa: E402
from src.config import DEFAULT_TMP_IMAGES_DIR  # noqa: E402

PLATFORM = "dingtalk"
OLD_ACCOUNT = "dingtalk:corpId"  # 早期 bug 产生的字面量账号


def _digest(acct: str) -> str:
    return hashlib.sha256(acct.encode("utf-8")).hexdigest()[:16]


def _conv_root() -> str:
    # 与 SQLiteStore._conv_db_path 一致：db_path 目录下的 conversations/
    from src.config import DEFAULT_STORAGE_PATH
    return os.path.join(os.path.dirname(os.path.abspath(DEFAULT_STORAGE_PATH)), "conversations")


def _rewrite_rel(rel: str, real_dir: str) -> str:
    """把单个 image_path（可能含 chat_id 维度的 JSON 多图映射）的账号段改为 real_dir。"""
    if not rel:
        return rel
    s = rel.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return json.dumps({k: _rewrite_rel(v, real_dir) for k, v in d.items()},
                                  ensure_ascii=False)
        except Exception:
            pass
    parts = rel.split("/")
    if len(parts) == 4 and parts[0] in ("dingtalk", "feishu", "wecom") and parts[1] != real_dir:
        parts[1] = real_dir
        return "/".join(parts)
    return rel


def _merge_old_into_new(old_db: str, new_db: str) -> dict:
    """把旧账号库(old_db)的全部数据合并进新账号库(new_db)，保留 new_db 既有数据。

    新库文件名即修复后真实 corpId 账号对应的库，必须保留其内部（Fix #3 之后写入的）
    少量数据。本函数以 ATTACH 方式把旧库行合并进来：

      * messages / external_friends 含 AUTOINCREMENT id —— 插入时排除 id 列让其重新分配，
        并以 msg_id / open_dingtalk_id 的 UNIQUE 约束去重；
      * conversations / conversation_summaries / dedup_messages / blocked_conversations
        以主键做 INSERT OR IGNORE 去重。
      * 最后修正 sqlite_sequence，避免后续插入因自增序列过小而 id 冲突。
    """
    stats: dict = {}
    con = sqlite3.connect(new_db)
    con.execute("ATTACH DATABASE ? AS old", (old_db,))
    try:
        cur = con.execute("INSERT OR IGNORE INTO conversations SELECT * FROM old.conversations")
        stats["conversations"] = cur.rowcount
        cur = con.execute(
            "INSERT OR IGNORE INTO messages "
            "(chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, "
            " timestamp, role, image_path, is_bot, is_archived, skip_reason, created_at) "
            "SELECT chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, "
            " timestamp, role, image_path, is_bot, is_archived, skip_reason, created_at "
            "FROM old.messages"
        )
        stats["messages"] = cur.rowcount
        cur = con.execute("INSERT OR IGNORE INTO conversation_summaries SELECT * FROM old.conversation_summaries")
        stats["conversation_summaries"] = cur.rowcount
        cur = con.execute("INSERT OR IGNORE INTO dedup_messages SELECT * FROM old.dedup_messages")
        stats["dedup_messages"] = cur.rowcount
        cur = con.execute(
            "INSERT OR IGNORE INTO external_friends "
            "(name, open_dingtalk_id, chat_id, notes, created_at, updated_at) "
            "SELECT name, open_dingtalk_id, chat_id, notes, created_at, updated_at "
            "FROM old.external_friends"
        )
        stats["external_friends"] = cur.rowcount
        cur = con.execute("INSERT OR IGNORE INTO blocked_conversations SELECT * FROM old.blocked_conversations")
        stats["blocked_conversations"] = cur.rowcount
        # 修正 AUTOINCREMENT 序列
        for tbl, pk in (("messages", "id"), ("external_friends", "id")):
            mx = con.execute(f"SELECT MAX({pk}) FROM {tbl}").fetchone()[0] or 0
            row = con.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (tbl,)).fetchone()
            if row and row[0] < mx:
                con.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (mx, tbl))
            elif not row:
                con.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", (tbl, mx))
        con.commit()
    finally:
        con.execute("DETACH DATABASE old")
        con.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="落盘执行（默认 dry-run）")
    ap.add_argument("--data-dir", default=".", help="仓库根目录")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    os.chdir(data_dir)

    real_acct = resolve_account_id(PLATFORM)            # 例如 dingtalk:ding9888...
    real_dir = account_id_dir(real_acct)                # 例如 ding9888ef577f7811cb
    old_dir = account_id_dir(OLD_ACCOUNT)               # corpId
    new_hash = _digest(real_acct)
    old_hash = _digest(OLD_ACCOUNT)
    conv_root = _conv_root()
    tmp_images = os.path.join(data_dir, DEFAULT_TMP_IMAGES_DIR.lstrip("./"))
    dingtalk_img = os.path.join(tmp_images, PLATFORM)

    old_db = os.path.join(conv_root, f"{PLATFORM}__{old_hash}.db")
    new_db = os.path.join(conv_root, f"{PLATFORM}__{new_hash}.db")

    print(f"[info] real_acct   = {real_acct}")
    print(f"[info] real_dir    = {real_dir}")
    print(f"[info] old_db      = {old_db}")
    print(f"[info] new_db      = {new_db}")
    print(f"[info] dingtalk_img= {dingtalk_img}")
    print(f"[mode] {'APPLY' if args.apply else 'DRY-RUN'}\n")

    changed = 0

    # ---- 1) per-account 会话库：旧(corpId) -> 新(真实 corpId) ----
    # 新库文件名即真实 corpId 账号对应的库，必须保留其内部既有数据（Fix #3 之后写入的少量
    # 消息）。因此采用「合并」而非「改名覆盖」：把旧库 ATTACH 进新库后合并，再删除旧库。
    if os.path.exists(old_db):
        try:
            oc = sqlite3.connect(old_db)
            oc.row_factory = sqlite3.Row
            old_counts = {t: oc.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                          for t in ("conversations", "messages", "conversation_summaries",
                                    "dedup_messages", "external_friends", "blocked_conversations")}
            oc.close()
        except Exception:
            old_counts = {}
        print(f"[1] 旧账号会话库存在，需合并 -> {os.path.basename(new_db)}")
        if old_counts:
            print(f"    旧库行数: {old_counts}")
        # checkpoint 把 WAL 合并进主库，避免合并后丢数据
        try:
            c = sqlite3.connect(old_db)
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
        except Exception as e:
            print(f"  [warn] checkpoint 失败: {e}")
        if args.apply:
            if os.path.exists(new_db):
                stats = _merge_old_into_new(old_db, new_db)
                print(f"  [ok] 已合并旧库进新库: {stats}")
            else:
                # 新库不存在（纯修复场景，无验证数据）：直接改名
                for suf in ("", "-wal", "-shm"):
                    src = old_db + suf
                    if os.path.exists(src):
                        shutil.move(src, new_db + suf)
                print(f"  [ok] 已改名旧库 -> 新库")
            # 删除旧库残留（含可能的 -wal/-shm）
            for suf in ("", "-wal", "-shm"):
                p = old_db + suf
                if os.path.exists(p):
                    os.remove(p)
            print(f"  [ok] 已删除旧库残留")
        changed += 1
    else:
        print(f"[1] 旧账号会话库不存在（{os.path.basename(old_db)}），跳过")

    # ---- 2) 改写新库内 image_path 的账号段（corpId / open_id 等 -> 真实 corpId） ----
    target_db = new_db if os.path.exists(new_db) else old_db
    if os.path.exists(target_db):
        con = sqlite3.connect(target_db)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        try:
            cur.execute("SELECT id, image_path FROM messages WHERE image_path IS NOT NULL AND image_path != ''")
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            rows = []
        upd = []
        for r in rows:
            old_val = r["image_path"]
            new_val = _rewrite_rel(old_val, real_dir)
            if new_val != old_val:
                upd.append((new_val, r["id"]))
        con.close()
        if upd:
            print(f"[2] {len(upd)} 条 image_path 需改写账号段 -> {real_dir}")
            if args.apply:
                con = sqlite3.connect(target_db)
                con.executemany("UPDATE messages SET image_path=? WHERE id=?", upd)
                con.commit()
                con.close()
                print(f"  [ok] 已改写 {len(upd)} 条")
            changed += 1
        else:
            print(f"[2] image_path 账号段已正确（{len(rows)} 条检查，无需改写）")
    else:
        print(f"[2] 目标会话库不存在，跳过 image_path 改写")

    # ---- 3) 磁盘图片目录：除真实账号目录外的子目录合并进真实账号目录 ----
    # 图片物理结构为 account_dir/chat_id/filename（多层嵌套），需用 copytree 整树合并。
    if os.path.isdir(dingtalk_img):
        subdirs = [d for d in os.listdir(dingtalk_img)
                   if os.path.isdir(os.path.join(dingtalk_img, d)) and d != real_dir]
        if subdirs:
            print(f"[3] 需合并的图片子目录: {subdirs}")
            if args.apply:
                real_path = os.path.join(dingtalk_img, real_dir)
                os.makedirs(real_path, exist_ok=True)
                for sd in subdirs:
                    src = os.path.join(dingtalk_img, sd)
                    # 整树合并（含 chat_id 子目录）；dirs_exist_ok 允许已存在的目标目录
                    shutil.copytree(src, real_path, dirs_exist_ok=True)
                    shutil.rmtree(src)
                    print(f"  [ok] 已合并并删除 {sd}/")
                changed += 1
        else:
            print(f"[3] 图片目录已只包含真实账号目录（{real_dir}），无需合并")
    else:
        print(f"[3] 图片目录不存在: {dingtalk_img}")

    print(f"\n[done] {'DRY-RUN，未改动任何文件' if not args.apply else 'APPLY 完成'}"
          f"{'' if changed else '（无需变更）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
