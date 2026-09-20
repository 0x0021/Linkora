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
    collect_active_image_paths,
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


def test_conv_db_path_contract(tmp_path):
    """契约(D4 根因)：会话分库路径位于 conversations/ 下，且**不等于**全局主库 db_path。

    孤儿扫描必须用会话分库路径（conv_db_path）构建 active 集，否则 active_paths 永远
    匹配不上 data/conversations/*.db，会把全部会话分库（含活跃账号）误判为孤儿并删图。
    """
    from src.memory.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=str(tmp_path / "linkora.db"))
    conv_path = store.conv_db_path("dingtalk")

    assert "conversations" in conv_path
    assert conv_path.endswith(".db")
    assert conv_path != store.db_path
    assert Path(conv_path).parent == tmp_path / "conversations"


def test_scan_orphan_method_excludes_active_conv_db(tmp_path, monkeypatch):
    """回归(D4)：_scan_orphan_conversation_dbs 必须用 store.conv_db_path 排除活跃库，
    否则活跃账号图片会被误删（2026-09-01 事故）。

    通过 monkeypatch data_path 把扫描限定在 tmp_path，避免触碰真实 data/。
    """
    import src.paths as paths_mod

    monkeypatch.setattr(paths_mod, "data_path", lambda name: tmp_path / name)
    # 身份可信度护栏会解析账号身份；CI 无 dws/lark-cli 时返回兜底键 → 整轮停手，
    # 本用例要验证的是「活跃库排除」，故显式给出可信身份。
    import src.memory.account_identity as ai_mod

    monkeypatch.setattr(ai_mod, "resolve_account_id", lambda platform, fallback_corp_id=None: f"{platform}:confident")

    conv = tmp_path / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    tmp_root = tmp_path / "tmp_images"

    active_path = conv / "dingtalk__active.db"
    _make_db(active_path, ["dingtalk/acct/chat/active_ocr.png", "dingtalk/acct/chat/active_ocr2.png"])
    orphan_db = conv / "dingtalk__orphan_real.db"
    _make_db(orphan_db, ["dingtalk/acct/chat/orphan_ocr.png"])

    active_img = _make_img(tmp_root, "dingtalk/acct/chat/active_ocr.png")
    active_img2 = _make_img(tmp_root, "dingtalk/acct/chat/active_ocr2.png")
    orphan_img = _make_img(tmp_root, "dingtalk/acct/chat/orphan_ocr.png")

    class FakeStore:
        # 旧代码的错误来源：用全局主库路径做 active 匹配
        db_path = str(tmp_path / "linkora.db")

        def conv_db_path(self, platform, fallback_corp_id=None):
            return str(active_path)

    from types import SimpleNamespace

    from src.platform.memory import MemoryMixin

    host = MemoryMixin()
    host.platforms = {"dingtalk": SimpleNamespace(store=FakeStore())}

    host._scan_orphan_conversation_dbs()

    assert active_img.exists() and active_img2.exists()  # 活跃账号图片保留（回归点）
    assert not orphan_img.exists()  # 孤儿图片回收
    assert orphan_db.exists()  # 孤儿库本体不删


# ── 跨库保护集 ────────────────────────────────────────────────────────────────

def test_collect_active_image_paths_union(tmp_path):
    a = tmp_path / "conversations" / "a.db"
    b = tmp_path / "conversations" / "b.db"
    _make_db(a, ["dingtalk/x/ocr_1.png", "dingtalk/x/ocr_2.png"])
    _make_db(b, ["feishu/y/card_1.png"])
    got = collect_active_image_paths({str(a), str(b), str(tmp_path / "missing.db")})
    assert got == {
        "dingtalk/x/ocr_1.png",
        "dingtalk/x/ocr_2.png",
        "feishu/y/card_1.png",
    }


def test_scan_protects_images_shared_with_active_db(tmp_path):
    """回归：同一张图同时被活跃库与孤儿库引用时，**不得**因孤儿库的引用而删除。

    这是 2026-09-01 事故「活跃集算错」之外的第二种形态：即便活跃集完全正确，只要
    跨库存在共享 image_path（账号迁移 / 复制库残留就会产生），按孤儿库引用删仍会
    打穿活跃账号的图。回收前必须先减去活跃库保护集。
    """
    conv = tmp_path / "conversations"
    tmp_root = tmp_path / "tmp_images"
    active_db = conv / "dingtalk__active.db"
    orphan_db = conv / "dingtalk__orphan.db"
    shared = "dingtalk/acct/chat/ocr_shared.png"
    only_orphan = "dingtalk/acct/chat/ocr_orphan_only.png"

    _make_db(active_db, [shared])
    _make_db(orphan_db, [shared, only_orphan])

    shared_img = _make_img(tmp_root, shared)
    only_img = _make_img(tmp_root, only_orphan)

    orphan_names, reclaimed = scan_and_reclaim_orphan_tmp_images(conv, {str(active_db)}, tmp_root)

    assert orphan_names == ["dingtalk__orphan.db"]
    assert shared_img.exists()  # 活跃库仍在引用 → 保护集命中，不删
    assert not only_img.exists()  # 仅孤儿库引用 → 回收
    assert reclaimed == 1


# ── 身份不确定时停手（fail-closed）────────────────────────────────────────────

def test_identity_is_confident_contract():
    from src.memory.account_identity import identity_is_confident

    assert identity_is_confident("dingtalk:ding9888ef577f7811cb")
    assert identity_is_confident("feishu:cli_a1b2c3")
    assert identity_is_confident("wecom:7f67711665a9a12f")
    # 兜底键：不含真实账号成分
    assert not identity_is_confident("dingtalk:unknown")
    assert not identity_is_confident("feishu:unknown")
    assert not identity_is_confident("wecom")  # 裸平台名兜底（未找到企微配置）
    assert not identity_is_confident("")
    assert not identity_is_confident(None)  # type: ignore[arg-type]


def test_scan_method_skips_reclaim_when_identity_unknown(tmp_path, monkeypatch):
    """身份解析退化成兜底键时必须整轮停手：此时推导的活跃库路径与磁盘真实分库名
    不匹配，继续回收会把活跃账号图片全部删掉（2026-09-01 事故的成因路径）。"""
    import src.memory.account_identity as ai_mod
    import src.paths as paths_mod

    monkeypatch.setattr(paths_mod, "data_path", lambda name: tmp_path / name)
    monkeypatch.setattr(
        ai_mod, "resolve_account_id",
        lambda platform, fallback_corp_id=None: f"{platform}:unknown",
    )

    conv = tmp_path / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    tmp_root = tmp_path / "tmp_images"

    # 身份不可信时 conv_db_path 会推导出一个磁盘上**不存在**的路径（公式用的是
    # "<platform>:unknown" 的哈希），于是活跃集与真实分库名对不上。
    class FakeStore:
        db_path = str(tmp_path / "linkora.db")

        def conv_db_path(self, platform, fallback_corp_id=None):
            return str(conv / "dingtalk__unknown00000000.db")  # 故意不存在

    active_img = _make_img(tmp_root, "dingtalk/acct/chat/active_ocr.png")
    # 磁盘上的真实分库（名字与不可信身份推导出的路径不一致）
    _make_db(conv / "dingtalk__realaccount11.db", ["dingtalk/acct/chat/active_ocr.png"])

    from types import SimpleNamespace

    from src.platform.memory import MemoryMixin

    host = MemoryMixin()
    host.platforms = {"dingtalk": SimpleNamespace(store=FakeStore())}

    host._scan_orphan_conversation_dbs()

    assert active_img.exists()  # 关键回归点：身份不可信 → 一张图都不许删
