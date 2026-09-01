"""孤儿会话库检测 + tmp_images 回收测试（D4）。

覆盖 src.platform.orphan_cleanup：
- 活跃库（db_path 在 active 集）跳过，不回收其图片；
- 备份类文件名跳过；
- 孤儿库只读收集 messages.image_path，按真实 tmp_images 根回收其引用图片；
- 不删除孤儿库本体；单库异常不影响其余。
"""
import sqlite3
from pathlib import Path

from src.platform.orphan_cleanup import (
    _is_backup_name,
    collect_orphan_image_paths,
    scan_and_reclaim_orphan_tmp_images,
)


def _make_db(db_path: Path, image_paths: list[str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, image_path TEXT)")
    if image_paths:
        conn.executemany(
            "INSERT INTO messages(image_path) VALUES (?)",
            [(p,) for p in image_paths],
        )
    conn.commit()
    conn.close()


def _make_img(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"png-bytes")
    return p


def test_is_backup_name():
    assert _is_backup_name("feishu__bak_20260728_231825.db")
    assert _is_backup_name("feishu__full_bak_20260728_233837.db")
    assert _is_backup_name("feishu__9cc368acef4db1ed.db.bak_pre_cleanup_20260728_222943")
    assert not _is_backup_name("dingtalk__490224ac5f43564b.db")
    assert not _is_backup_name("wecom__7f67711665a9a12f.db")


def test_collect_orphan_image_paths_dedup(tmp_path):
    db = tmp_path / "conversations" / "orphan.db"
    _make_db(db, ["dingtalk/a/c/ocr_1.png", "dingtalk/a/c/ocr_1.png", "feishu/x/y/card_2.png", ""])
    rels = collect_orphan_image_paths(db)
    assert rels == ["dingtalk/a/c/ocr_1.png", "feishu/x/y/card_2.png"]


def test_collect_orphan_image_paths_missing_table(tmp_path):
    db = tmp_path / "conversations" / "broken.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other(x INTEGER)")
    conn.commit()
    conn.close()
    assert collect_orphan_image_paths(db) == []


def test_scan_reclaims_orphan_images_only(tmp_path):
    conv = tmp_path / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    tmp_root = tmp_path / "tmp_images"
    active_db = conv / "dingtalk__active.db"
    _make_db(active_db, ["dingtalk/acct/chat/ocr_active.png"])  # 活跃库，跳过
    orphan_db = conv / "dingtalk__orphan1.db"
    _make_db(orphan_db, ["dingtalk/acct/chat/ocr_1.png", "dingtalk/acct/chat/ocr_2.png"])

    orphan_img1 = _make_img(tmp_root, "dingtalk/acct/chat/ocr_1.png")
    orphan_img2 = _make_img(tmp_root, "dingtalk/acct/chat/ocr_2.png")
    # 活跃库引用的图片不应被回收
    active_img = _make_img(tmp_root, "dingtalk/acct/chat/ocr_active.png")

    orphan_names, reclaimed = scan_and_reclaim_orphan_tmp_images(
        conv, {str(active_db)}, tmp_root
    )

    assert orphan_names == ["dingtalk__orphan1.db"]
    assert reclaimed == 2
    assert not orphan_img1.exists()
    assert not orphan_img2.exists()
    assert active_img.exists()  # 活跃库图片保留
    assert orphan_db.exists()  # 孤儿库本体不删


def test_scan_skips_backup_named_dbs(tmp_path):
    conv = tmp_path / "conversations"
    tmp_root = tmp_path / "tmp_images"
    backup_db = conv / "feishu__bak_20260728_231825.db"
    _make_db(backup_db, ["feishu/x/y/should_not_delete.png"])
    bak_img = _make_img(tmp_root, "feishu/x/y/should_not_delete.png")

    orphan_names, reclaimed = scan_and_reclaim_orphan_tmp_images(conv, set(), tmp_root)

    assert orphan_names == []  # 备份类被跳过
    assert reclaimed == 0
    assert bak_img.exists()  # 备份引用图片未删


def test_scan_empty_active_reclaims_all(tmp_path):
    """模块函数契约：active 集为空时把全部库当孤儿回收（护栏由 memory.py 方法层负责）。"""
    conv = tmp_path / "conversations"
    tmp_root = tmp_path / "tmp_images"
    orphan_db = conv / "dingtalk__orphan2.db"
    _make_db(orphan_db, ["dingtalk/a/c/ocr_1.png"])
    img = _make_img(tmp_root, "dingtalk/a/c/ocr_1.png")

    orphan_names, reclaimed = scan_and_reclaim_orphan_tmp_images(conv, set(), tmp_root)

    assert orphan_names == ["dingtalk__orphan2.db"]
    assert reclaimed == 1
    assert not img.exists()  # 模块按契约回收（memory.py 在活跃集为空时已拦截，不会走到这）


def test_scan_missing_dir_returns_empty(tmp_path):
    assert scan_and_reclaim_orphan_tmp_images(tmp_path / "nope", set(), tmp_path) == ([], 0)
