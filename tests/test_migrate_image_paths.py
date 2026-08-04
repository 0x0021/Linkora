"""scripts.migrate_image_paths 迁移脚本集成测试。

用临时 DB + 临时图片目录，覆盖：
- 单图旧路径 -> 新结构，文件物理移动
- 飞书 JSON 多图映射整体改写
- chat_id 含 base64 特殊字符（+/=）正确转义
- 幂等：二次运行不重复移动、count 不变
- 源文件缺失：仅改写 DB 路径不报错
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
import migrate_image_paths as mig  # noqa: E402


def _make_db(db_path: Path, convs: list[tuple[str, str]], imgs: list[str]):
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (chat_id TEXT, chat_name TEXT)")
    conn.execute("CREATE TABLE messages (image_path TEXT)")
    conn.executemany("INSERT INTO conversations VALUES (?,?)", convs)
    conn.executemany("INSERT INTO messages (image_path) VALUES (?)", [(x,) for x in imgs])
    conn.commit()
    conn.close()


def _old_dir(chat_name: str) -> str:
    # 复刻旧 sanitize：re.sub(r"[^\w\u4e00-\u9fff]", "_", chat_name)[:40]
    import re
    return re.sub(r"[^\w\u4e00-\u9fff]", "_", chat_name)[:40]


def test_single_image_moved(tmp_path: Path):
    img_root = tmp_path / "tmp_images"
    img_root.mkdir()
    db = tmp_path / "feishu__test.db"
    chat_id = "oc_13c85f9a027902117d9063f2dc04f138"
    chat_name = "飞行社"
    old_dir = _old_dir(chat_name)
    fname = "card_img_v3_02124_x.png"
    (img_root / old_dir).mkdir(parents=True)
    (img_root / old_dir / fname).write_bytes(b"PNGDATA")

    _make_db(db, [(chat_id, chat_name)], [f"{old_dir}/{fname}"])

    st = mig.migrate_db(db, "feishu", "feishu:ou_abc", img_root, apply=True)
    assert st["migrated"] == 1

    # 新路径格式
    conn = sqlite3.connect(str(db))
    new_val = conn.execute("SELECT image_path FROM messages").fetchone()[0]
    conn.close()
    assert new_val == f"feishu/ou_abc/{chat_id}/{fname}"
    # 文件已移动到新位置，旧位置消失
    assert (img_root / new_val).exists()
    assert not (img_root / old_dir / fname).exists()


def test_json_map_migrated(tmp_path: Path):
    img_root = tmp_path / "tmp_images"
    img_root.mkdir()
    db = tmp_path / "feishu__test.db"
    chat_id = "oc_13c85f9a027902117d9063f2dc04f138"
    chat_name = "飞书智能助手"
    old_dir = _old_dir(chat_name)
    files = {
        "img_v3_a": "card_img_v3_a.png",
        "img_v3_b": "card_img_v3_b.png",
    }
    (img_root / old_dir).mkdir(parents=True)
    for f in files.values():
        (img_root / old_dir / f).write_bytes(b"PNGDATA")
    mapping = {k: f"{old_dir}/{v}" for k, v in files.items()}
    _make_db(db, [(chat_id, chat_name)], [json.dumps(mapping, ensure_ascii=False)])

    st = mig.migrate_db(db, "feishu", "feishu:ou_abc", img_root, apply=True)
    assert st["migrated"] == 1
    assert st["json"] == 1

    conn = sqlite3.connect(str(db))
    new_val = conn.execute("SELECT image_path FROM messages").fetchone()[0]
    conn.close()
    new_map = json.loads(new_val)
    for k, v in new_map.items():
        assert v == f"feishu/ou_abc/{chat_id}/{files[k]}"
        assert (img_root / v).exists()


def test_chat_id_special_chars(tmp_path: Path):
    img_root = tmp_path / "tmp_images"
    img_root.mkdir()
    db = tmp_path / "dingtalk__test.db"
    # 钉钉 chat_id 含 + / =
    chat_id = "cidB75S9QNvDmZabnBuIdwWoVzEPClGZiWHpZfyYdmL5zQ="
    chat_name = "陈海艳"
    old_dir = _old_dir(chat_name)
    fname = "ocr_x.png"
    (img_root / old_dir).mkdir(parents=True)
    (img_root / old_dir / fname).write_bytes(b"PNGDATA")
    _make_db(db, [(chat_id, chat_name)], [f"{old_dir}/{fname}"])

    st = mig.migrate_db(db, "dingtalk", "dingtalk:corp123", img_root, apply=True)
    assert st["migrated"] == 1
    conn = sqlite3.connect(str(db))
    new_val = conn.execute("SELECT image_path FROM messages").fetchone()[0]
    conn.close()
    # chat_id 段里的 = 被清掉
    assert new_val == f"dingtalk/corp123/cidB75S9QNvDmZabnBuIdwWoVzEPClGZiWHpZfyYdmL5zQ/{fname}"
    assert (img_root / new_val).exists()


def test_idempotent(tmp_path: Path):
    img_root = tmp_path / "tmp_images"
    img_root.mkdir()
    db = tmp_path / "feishu__test.db"
    chat_id = "oc_abc"
    chat_name = "飞行社"
    old_dir = _old_dir(chat_name)
    fname = "card_x.png"
    (img_root / old_dir).mkdir(parents=True)
    (img_root / old_dir / fname).write_bytes(b"PNGDATA")
    _make_db(db, [(chat_id, chat_name)], [f"{old_dir}/{fname}"])

    mig.migrate_db(db, "feishu", "feishu:ou_abc", img_root, apply=True)
    st2 = mig.migrate_db(db, "feishu", "feishu:ou_abc", img_root, apply=True)
    # 第二次应为 0 migrated（已是新结构，被跳过）
    assert st2["migrated"] == 0
    assert st2["skipped"] == 1


def test_missing_source_only_rewrites_db(tmp_path: Path):
    img_root = tmp_path / "tmp_images"
    img_root.mkdir()
    db = tmp_path / "feishu__test.db"
    chat_id = "oc_abc"
    chat_name = "飞行社"
    old_dir = _old_dir(chat_name)
    fname = "ghost.png"  # 文件不存在
    _make_db(db, [(chat_id, chat_name)], [f"{old_dir}/{fname}"])

    st = mig.migrate_db(db, "feishu", "feishu:ou_abc", img_root, apply=True)
    assert st["migrated"] == 1
    assert st["missing_src"] == 1
    conn = sqlite3.connect(str(db))
    new_val = conn.execute("SELECT image_path FROM messages").fetchone()[0]
    conn.close()
    # DB 仍改写为新结构路径（前端按加载失败优雅降级）
    assert new_val == f"feishu/ou_abc/{chat_id}/{fname}"
