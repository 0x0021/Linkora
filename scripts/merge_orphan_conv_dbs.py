#!/usr/bin/env python3
"""合并账号身份漂移产生的孤儿分库到当前正常库。

背景
----
``resolve_account_id`` 在重登录 / CLI 解析漂移后可能返回不同的账号键，导致
per-account 会话库 ``data/conversations/<platform>__<sha256(account_id)[:16]>.db``
被拆分到多个文件。本脚本把「非当前哈希的分库（孤儿）」中、正常库没有的独有数据，
按 ``msg_id`` / 主键去重合并进当前正常库。

安全设计
--------
* 默认 dry-run，仅报告将合并的行数；``--apply`` 才落盘。
* 落盘前自动备份所有涉及分库到 ``data/migration_backup_<时间戳>/``。
* ``--kill-bot``（默认开启）在合并前暂停 bot 写入进程（读 pid 文件 SIGTERM）。
* 消息表 ``msg_id`` 为 UNIQUE，其他表均有主键，``INSERT OR IGNORE`` 天然去重。
* 含自增 ``id`` 的表（messages / external_friends）合并时排除 ``id`` 列重新分配，
  避免两库 ``id`` 重叠导致整行被忽略而丢数据。
* 合并后修正 ``sqlite_sequence``，避免后续自增 ``id`` 冲突。
* ``--delete-orphans`` 合并成功后删除孤儿库文件（备份仍在，可恢复）。
* 若某平台账号解析失败（回退 ``unknown``），跳过该平台以免误判正常库。

用法
----
    python scripts/merge_orphan_conv_dbs.py            # 仅报告计划
    python scripts/merge_orphan_conv_dbs.py --apply    # 执行合并（自动备份+暂停 bot）
    python scripts/merge_orphan_conv_dbs.py --apply --delete-orphans
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.memory.account_identity import resolve_account_id  # noqa: E402

PLATFORMS = ["feishu", "dingtalk", "wecom"]
TABLES = [
    "messages",
    "conversations",
    "dedup_messages",
    "conversation_summaries",
    "external_friends",
    "blocked_conversations",
]
# 含自增 id 的表：合并时排除 id 列，避免两库 id 重叠丢数据
ID_TABLES = {"messages", "external_friends"}
# 非 ID 表的主键（用于 dry-run 统计与去重键展示）
PK_OF = {
    "conversations": "chat_id",
    "dedup_messages": "msg_id",
    "conversation_summaries": "chat_id",
    "blocked_conversations": "chat_id",
}


def conv_root() -> str:
    from src.config import DEFAULT_STORAGE_PATH

    return os.path.join(os.path.dirname(os.path.abspath(DEFAULT_STORAGE_PATH)), "conversations")


def digest(aid: str) -> str:
    return hashlib.sha256(aid.encode("utf-8")).hexdigest()[:16]


def backup(src: str, backup_dir: str) -> None:
    os.makedirs(backup_dir, exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        s = src + suf
        if os.path.exists(s):
            shutil.copy(s, os.path.join(backup_dir, os.path.basename(s) + suf))


def kill_bot(data_dir: str) -> None:
    for name in ("linkora.pid", "linkora.worker.pid", "linkora.web.pid"):
        p = os.path.join(data_dir, name)
        if not os.path.exists(p):
            continue
        try:
            pid = int(open(p).read().strip())
        except Exception:
            continue
        try:
            os.kill(pid, 15)  # SIGTERM
            print(f"  [bot] 已发送 SIGTERM 到 {name} (pid {pid})")
        except ProcessLookupError:
            print(f"  [bot] {name} 进程不存在（跳过）")
        except Exception as e:  # noqa: BLE001
            print(f"  [bot] kill {name} 失败: {e}")


def _cols_excl_id(conn: sqlite3.Connection, tbl: str) -> list[str]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
    return [c for c in cols if c != "id"]


def _uniq_col(conn: sqlite3.Connection, tbl: str) -> str:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
    if "msg_id" in cols:
        return "msg_id"
    if "open_dingtalk_id" in cols:
        return "open_dingtalk_id"
    return "chat_id"


def merge(old_db: str, new_db: str, apply: bool, backup_dir: str) -> None:
    """把孤儿库 old_db 的独有数据合并进正常库 new_db。

    apply=True 才落盘；否则仅统计将合并的行数。
    """
    # 先把孤儿库 WAL 合并进主文件，避免 ATTACH 读不到未 checkpoint 的数据
    try:
        oc = sqlite3.connect(old_db)
        oc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        oc.close()
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] checkpoint {os.path.basename(old_db)} 失败: {e}")

    con = sqlite3.connect(new_db)
    con.execute("ATTACH DATABASE ? AS old", (old_db,))
    try:
        for tbl in TABLES:
            has_new = con.execute("SELECT name FROM sqlite_master WHERE name=?", (tbl,)).fetchone()
            has_old = con.execute("SELECT name FROM old.sqlite_master WHERE name=?", (tbl,)).fetchone()
            if not (has_new and has_old):
                continue

            if tbl in ID_TABLES:
                cols = _cols_excl_id(con, tbl)
                cl = ",".join(cols)
                uk = _uniq_col(con, tbl)
                if not apply:
                    old_rows = con.execute(f"SELECT COUNT(*) FROM old.{tbl}").fetchone()[0]
                    old_only = con.execute(
                        f"SELECT COUNT(*) FROM old.{tbl} WHERE {uk} NOT IN (SELECT {uk} FROM {tbl})"
                    ).fetchone()[0]
                    print(f"    [dry] {tbl}: old={old_rows} 独有(按{uk})={old_only}")
                else:
                    cur = con.execute(f"INSERT OR IGNORE INTO {tbl} ({cl}) SELECT {cl} FROM old.{tbl}")
                    print(f"    [ok] {tbl}: 合并 {cur.rowcount} 行（按{uk}去重）")
            else:
                pk = PK_OF.get(tbl, "chat_id")
                if not apply:
                    old_rows = con.execute(f"SELECT COUNT(*) FROM old.{tbl}").fetchone()[0]
                    old_only = con.execute(
                        f"SELECT COUNT(*) FROM old.{tbl} WHERE {pk} NOT IN (SELECT {pk} FROM {tbl})"
                    ).fetchone()[0]
                    print(f"    [dry] {tbl}: old={old_rows} 独有(按{pk})={old_only}")
                else:
                    cur = con.execute(f"INSERT OR IGNORE INTO {tbl} SELECT * FROM old.{tbl}")
                    print(f"    [ok] {tbl}: 合并 {cur.rowcount} 行（按{pk}去重）")

        if apply:
            # 修正自增序列，避免后续插入 id 冲突
            for tbl in ID_TABLES:
                mx = con.execute(f"SELECT MAX(id) FROM {tbl}").fetchone()[0] or 0
                row = con.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (tbl,)).fetchone()
                if row and row[0] < mx:
                    con.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (mx, tbl))
                elif not row:
                    con.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", (tbl, mx))
            con.commit()
    finally:
        con.execute("DETACH DATABASE old")
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="落盘执行（默认 dry-run）")
    ap.add_argument("--delete-orphans", action="store_true", help="合并成功后删除孤儿库文件")
    ap.add_argument("--no-kill-bot", action="store_true", help="不暂停 bot 进程")
    ap.add_argument("--data-dir", default=".", help="仓库根目录")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    os.chdir(data_dir)
    root = conv_root()
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(data_dir, "data", "migration_backup_" + ts)

    print(f"[mode] {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"[backup] {backup_dir}")
    if args.apply and not args.no_kill_bot:
        kill_bot(data_dir)

    for p in PLATFORMS:
        aid = resolve_account_id(p)
        if aid.endswith(":unknown") or aid == "wecom":
            print(f"\n## 平台 {p}: 账号解析失败（{aid}），跳过以免误判正常库")
            continue
        nd = digest(aid)
        normal_db = os.path.join(root, f"{p}__{nd}.db")
        orphans = []
        for f in sorted(glob.glob(os.path.join(root, f"{p}__*.db"))):
            base = os.path.basename(f)
            if "bak" in base:
                continue
            d = base.split("__")[1].split(".")[0]
            if d != nd:
                orphans.append(f)
        print(f"\n## 平台 {p}: 正常库={os.path.basename(normal_db)} (account={aid})")
        print(f"   孤儿库={[os.path.basename(o) for o in orphans]}")
        if not os.path.exists(normal_db):
            print("  [warn] 正常库不存在，跳过（无法合并）")
            continue
        if args.apply:
            backup(normal_db, backup_dir)
            for o in orphans:
                backup(o, backup_dir)
        for o in orphans:
            print(f"  -- 合并 {os.path.basename(o)} -> {os.path.basename(normal_db)}")
            merge(o, normal_db, args.apply, backup_dir)
            if args.apply and args.delete_orphans:
                for suf in ("", "-wal", "-shm"):
                    pp = o + suf
                    if os.path.exists(pp):
                        os.remove(pp)
                print(f"     [del] 已删除孤儿库 {os.path.basename(o)}")

    if args.apply:
        print("\n=== 合并后正常库消息数 ===")
        for p in PLATFORMS:
            aid = resolve_account_id(p)
            if aid.endswith(":unknown") or aid == "wecom":
                continue
            nd = digest(aid)
            db = os.path.join(root, f"{p}__{nd}.db")
            if os.path.exists(db):
                n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                print(f"  {p}: {n} 条消息")

    print(f"\n[done] {'APPLY 完成，备份在 ' + backup_dir if args.apply else 'DRY-RUN 结束，未改动任何文件'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
