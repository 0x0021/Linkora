#!/usr/bin/env python3
"""一次性迁移：把旧结构图片路径改写为新结构 <platform>/<account_id>/<chat_id>/<file>。

背景
----
图片曾按 ``data/tmp_images/<chat_name>/<file>`` 存储（目录名是 sanitized 后的聊天对象名，
可能含中文/重名，且跨平台、跨账号无法区分）。现统一改为分平台 + 账号 + 会话隔离：

    data/tmp_images/<platform>/<account_id>/<chat_id>/<file>

本脚本：
  1) 扫 ``data/conversations/*.db``（每个库对应一个平台 + 账号）；
  2) 从 ``conversations`` 表建立 ``sanitized(chat_name) -> chat_id`` 映射；
  3) 对每条 ``image_path`` 非空的消息：
     - 已是新结构（4 段且首段是已知平台）→ 跳过（幂等）；
     - 单路径 ``<old_dir>/<file>`` → 反查 chat_id，计算新 rel，物理移动文件；
     - JSON 映射（飞书卡片多图 ``{key: "<old_dir>/<file>"}``）→ 逐值改写后写回 JSON；
  4) 默认 dry-run（只打印会做什么），``--apply`` 才真正移动文件 + UPDATE DB。

account_id 默认用 ``src.memory.account_identity.resolve_account_id(platform)``
（飞书=appId，钉钉=corpId，企微=配置 sha）。它给出稳定的「每账号」目录键，满足隔离需求。
若想与运行时 poller 完全一致（飞书运行时用 open_id 而非 appId），可用
``--account-id feishu:ou_xxxx`` 显式指定与 poller 同款的 ``<platform>:<user_id>``。

用法
----
    python scripts/migrate_image_paths.py                 # dry-run 预览
    python scripts/migrate_image_paths.py --apply         # 真正迁移
    python scripts/migrate_image_paths.py --platform feishu --account-id feishu:ou_xxx --apply
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import re
import shutil
import sqlite3
import sys
from pathlib import Path

# 允许以脚本方式直接运行（把 repo 根加入 sys.path，便于 import src.*）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.image_path import is_new_image_path, image_rel_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_image_paths")

# 旧目录名曾用 ``re.sub(r"[^\w\u4e00-\u9fff]", "_", chat_name)[:40]`` 生成，
# 这里原样复刻，才能正确定位磁盘上的旧目录。
_OLD_SANITIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]")


def old_sanitize(name: str) -> str:
    return _OLD_SANITIZE_RE.sub("_", name or "未知")[:40]


def build_name_map(db_path: Path) -> dict[str, str]:
    """从 conversations 表建 ``sanitized(chat_name) -> chat_id`` 映射（含告警）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    try:
        rows = conn.execute("SELECT chat_id, chat_name FROM conversations").fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        chat_id = r["chat_id"] or ""
        chat_name = r["chat_name"] or ""
        key = old_sanitize(chat_name)
        if not key or not chat_id:
            continue
        if key in out and out[key] != chat_id:
            logger.warning("[%s] chat_name 冲突: %r 映射到 %r 与已存在的 %r，取后者",
                           db_path.name, chat_name, chat_id, out[key])
        out.setdefault(key, chat_id)
    conn.close()
    return out


def migrate_single(old_rel: str, platform: str, account_id: str,
                   name_map: dict[str, str], image_temp_dir: Path,
                   apply: bool) -> tuple[str | None, str]:
    """迁移单个旧路径。返回 (new_rel 或 None, 人类可读说明)。"""
    old_rel = (old_rel or "").strip()
    if not old_rel:
        return None, "空路径，跳过"
    # 兼容「已写过绝对路径」的情况（早期 image_rel_path 返回绝对路径的遗留）：
    # 剥离 image_temp_dir 前缀，还原成相对路径后再判定。
    abs_prefix = str(image_temp_dir.resolve())
    was_absolute = old_rel.startswith(abs_prefix) or old_rel.startswith("/")
    norm = old_rel
    if norm.startswith(abs_prefix):
        norm = norm[len(abs_prefix):].lstrip("/")
    elif norm.startswith("/"):
        # 绝对但不含已知前缀：从前往后找首个已知 platform 段，截到那里
        segs = norm.split("/")
        for i, seg in enumerate(segs):
            if seg in ("feishu", "dingtalk", "wecom"):
                norm = "/".join(segs[i:])
                break
    if norm != old_rel:
        logger.info("  绝对路径已规范化: %s -> %s", old_rel, norm)
        old_rel = norm
    if is_new_image_path(old_rel):
        # 已新结构：若原始值是绝对路径（被规范化成相对），回写 DB 修正为相对路径；
        # 否则（本来就是相对新结构）跳过，保持幂等。
        if was_absolute:
            return old_rel, "绝对路径规范化新结构，改 DB 为相对路径"
        return None, "已是新结构，跳过"
    # 旧结构：<old_dir>/<file>
    parts = old_rel.replace("\\", "/").split("/")
    if len(parts) != 2:
        return None, f"无法识别为旧 2 段路径: {old_rel!r}，跳过"
    old_dir, filename = parts
    chat_id = name_map.get(old_dir)
    if not chat_id:
        # 兜底：用旧目录名直接当 chat_id 段（保证文件仍可定位，但不与实时 chat_id 对齐）
        chat_id = old_dir
        logger.warning("  找不到 chat_name=%r 对应的 chat_id，回退用目录名 %r 作为 chat_id 段",
                       old_dir, old_dir)
    new_rel = image_rel_path(image_temp_dir, platform, account_id, chat_id, filename)
    old_abs = image_temp_dir / old_dir / filename
    new_abs = image_temp_dir / new_rel
    if not old_abs.exists():
        # 源文件已丢失（可能之前就下失败 / 已手动清理）。仍把 DB 路径改写为新结构，
        # 至少保持 schema 一致；前端会按「图加载失败」优雅降级。
        logger.warning("  源文件不存在，仅改 DB 路径: %s", old_abs)
        return new_rel, f"DB 改写(源缺失): {old_rel} -> {new_rel}"
    if new_abs.exists():
        # 目标已存在（可能之前手动迁过 / 幂等重跑）→ 直接复用，删掉旧文件避免残留
        logger.info("  目标已存在，复用并清理源: %s", new_rel)
        if apply and old_abs.exists():
            try:
                old_abs.unlink()
            except OSError:
                pass
        return new_rel, f"目标已存在复用: {new_rel}"
    if apply:
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_abs), str(new_abs))
    return new_rel, f"{'会移动' if not apply else '已移动'}: {old_rel} -> {new_rel}"


def migrate_db(db_path: Path, platform: str | None, account_id_override: str | None,
               image_temp_dir: Path, apply: bool) -> dict:
    """迁移单个 DB。返回统计 dict。"""
    # 1) 平台：参数 > DB 文件名前缀（feishu__xxx.db -> feishu）
    stem = db_path.stem
    if platform is None:
        platform = stem.split("__", 1)[0] if "__" in stem else stem.split("_", 1)[0]
    if not platform or platform == stem:
        # 无法从文件名推断平台（如只有 hash 后缀的遗留库）→ 跳过，需显式 --platform
        logger.warning("无法从文件名推断平台，跳过: %s（可用 --platform 指定）", db_path.name)
        return {"db": db_path.name, "skipped": 0, "total": 0, "migrated": 0,
                "json": 0, "missing_src": 0, "platform": "", "account_id": ""}
    # 2) account_id：参数 > resolve_account_id（稳定每账号键）
    if account_id_override:
        account_id = account_id_override
    else:
        try:
            from src.memory.account_identity import resolve_account_id
            account_id = resolve_account_id(platform)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] resolve_account_id 失败(%s)，回退 %s:unknown", db_path.name, e, platform)
            account_id = f"{platform}:unknown"
    # 3) chat_name -> chat_id
    name_map = build_name_map(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rowid, image_path FROM messages WHERE image_path IS NOT NULL AND image_path != ''"
    ).fetchall()

    stats = {"db": db_path.name, "platform": platform, "account_id": account_id,
             "total": 0, "skipped": 0, "migrated": 0, "json": 0, "missing_src": 0}
    updates: list[tuple[str, int]] = []

    for r in rows:
        rid = r[0]  # rowid（用索引更稳，避免 sqlite3.Row 对 rowid 别名的键名问题）
        stats["total"] += 1
        raw = r["image_path"]
        new_val: str | None = None
        note = ""
        if raw.lstrip().startswith("{"):
            # JSON 映射：逐值迁移
            try:
                mapping = json.loads(raw)
            except json.JSONDecodeError:
                stats["skipped"] += 1
                logger.warning("  [%s] rowid=%s 非法 JSON，跳过: %r", db_path.name, rid, raw[:60])
                continue
            new_mapping: dict[str, str] = {}
            changed = False
            for k, v in mapping.items():
                nr, n = migrate_single(v, platform, account_id, name_map, image_temp_dir, apply)
                if nr is not None and nr != v:
                    changed = True
                if "源缺失" in n:
                    stats["missing_src"] += 1
                new_mapping[k] = nr if nr is not None else v
            stats["json"] += 1
            if changed:
                new_val = json.dumps(new_mapping, ensure_ascii=False, sort_keys=True)
                note = "JSON 映射改写"
            else:
                stats["skipped"] += 1
                continue
        else:
            nr, note = migrate_single(raw, platform, account_id, name_map, image_temp_dir, apply)
            if nr is None:
                stats["skipped"] += 1
                continue
            if "源缺失" in note:
                stats["missing_src"] += 1
            new_val = nr

        if new_val is not None and new_val != raw:
            updates.append((new_val, rid))
            stats["migrated"] += 1
            logger.info("  [%s] rowid=%s %s", db_path.name, rid, note)
        else:
            stats["skipped"] += 1

    if apply and updates:
        conn.executemany("UPDATE messages SET image_path = ? WHERE rowid = ?", updates)
        conn.commit()
    conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移图片路径到 <platform>/<account_id>/<chat_id>/ 新结构")
    ap.add_argument("--data-dir", default=str(_REPO_ROOT), help="项目根目录（默认脚本上级）")
    ap.add_argument("--db-glob", default="data/conversations/*.db", help="DB 文件 glob（相对 data-dir）")
    ap.add_argument("--image-temp-dir", default="data/tmp_images", help="图片根目录（相对 data-dir）")
    ap.add_argument("--platform", default=None, help="强制平台（否则从 DB 文件名前缀推断）")
    ap.add_argument("--account-id", dest="account_id", default=None,
                    help="强制 account_id（格式 platform:user_id），覆盖 resolve_account_id")
    ap.add_argument("--apply", action="store_true", help="真正执行移动+写库；默认仅 dry-run 预览")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    image_temp_dir = (data_dir / args.image_temp_dir).resolve()
    db_files = sorted(glob.glob(str(data_dir / args.db_glob)))
    if not db_files:
        logger.error("未找到 DB: %s", data_dir / args.db_glob)
        return 2

    logger.info("模式: %s | 图片根: %s | DB 数: %d",
                "APPLY" if args.apply else "DRY-RUN", image_temp_dir, len(db_files))

    all_stats: list[dict] = []
    for db in db_files:
        db_path = Path(db)
        # 跳过备份文件
        if ".bak" in db_path.name or "_backup" in db_path.name:
            logger.info("跳过备份库: %s", db_path.name)
            continue
        logger.info("=== 处理 %s ===", db_path.name)
        st = migrate_db(db_path, args.platform, args.account_id, image_temp_dir, args.apply)
        all_stats.append(st)

    total_migrated = sum(s["migrated"] for s in all_stats)
    total_json = sum(s["json"] for s in all_stats)
    total_missing = sum(s["missing_src"] for s in all_stats)
    logger.info("汇总: 迁移 %d 条（含 %d 条 JSON 映射），源文件缺失 %d 条",
                total_migrated, total_json, total_missing)
    if not args.apply:
        logger.info("以上为 dry-run 预览。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
