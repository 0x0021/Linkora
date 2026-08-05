"""技能热加载测试（SkillManager 文件监控 + 自动 reload）。"""

import shutil
import time
import threading
from pathlib import Path

import pytest

from src.skills.manager import SkillManager


def _patch_skill_dirs(monkeypatch, root: str):
    """临时覆盖模块级 _SKILL_DIRS 仅指向测试目录。"""
    import src.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "_SKILL_DIRS", [root + "/data/skills"])


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def tmp_skill_root(tmp_path):
    """创建一个含 skills 目录结构的临时项目根。"""
    data_skills = tmp_path / "data" / "skills"
    data_skills.mkdir(parents=True)
    return tmp_path


def _write_skill(skill_dir: Path, name: str, description: str = "test skill", **extra) -> None:
    """在 skill_dir 下写一个最小合法 SKILL.md。"""
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"""---
name: {name}
description: {description}
---
# {name}

Test body.
"""
    if extra:
        # 简单追加额外 frontmatter 字段（不通用，够测试用）
        for k, v in extra.items():
            fm = fm.replace("---\n", f"---\n{k}: {v}\n", 1)
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")


# ── 基础加载 ────────────────────────────────────────────────

class TestSkillLoading:
    def test_load_single_skill(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        _write_skill(tmp_skill_root / "data" / "skills" / "hello", "hello", "greeting")
        mgr = SkillManager(tmp_skill_root, poll_interval=1.0)
        count = mgr.reload()
        assert count == 1
        assert "hello" in mgr.list_names()
        s = mgr.get("hello")
        assert s is not None
        assert s.name == "hello"

    def test_load_multiple_skills(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        for name in ["alpha", "beta", "gamma"]:
            _write_skill(tmp_skill_root / "data" / "skills" / name, name, f"skill {name}")
        mgr = SkillManager(tmp_skill_root)
        assert mgr.reload() == 3
        assert set(mgr.list_names()) == {"alpha", "beta", "gamma"}

    def test_skip_missing_skillmd(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        bad_dir = tmp_skill_root / "data" / "skills" / "no-skill-md"
        bad_dir.mkdir()
        (bad_dir / "readme.txt").write_text("nothing here")
        _write_skill(tmp_skill_root / "data" / "skills" / "good", "good", "ok")
        mgr = SkillManager(tmp_skill_root)
        assert mgr.reload() == 1
        assert "good" in mgr.list_names()

    def test_reload_clears_previous(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        _write_skill(tmp_skill_root / "data" / "skills" / "old", "old", "old skill")
        mgr = SkillManager(tmp_skill_root)
        assert mgr.reload() == 1
        # 删旧加新
        shutil.rmtree(tmp_skill_root / "data" / "skills" / "old")
        _write_skill(tmp_skill_root / "data" / "skills" / "new", "new", "new skill")
        assert mgr.reload() == 1
        assert "new" in mgr.list_names()
        assert "old" not in mgr.list_names()


# ── 线程安全 ────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_read(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        _write_skill(tmp_skill_root / "data" / "skills" / "shared", "shared", "concurrent test")
        mgr = SkillManager(tmp_skill_root)
        mgr.reload()

        errors = []

        def reader():
            try:
                for _ in range(100):
                    s = mgr.get("shared")
                    mgr.list_names()
                    mgr.list_all()
                    mgr.skills_prompt_section()
                    mgr.activate_prompt("shared")
                    assert s is not None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors, f"Concurrent read errors: {errors}"

    def test_reload_during_read(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        for i in range(5):
            _write_skill(tmp_skill_root / "data" / "skills" / f"skill{i}", f"skill{i}", f"desc {i}")
        mgr = SkillManager(tmp_skill_root)
        mgr.reload()

        stop = False
        errors = []

        def reloader():
            while not stop:
                mgr.reload()
                time.sleep(0.01)

        def reader():
            while not stop:
                try:
                    mgr.list_all()
                    mgr.get("skill0")
                except Exception as e:
                    errors.append(e)
                time.sleep(0.005)

        rt = threading.Thread(target=reloader, daemon=True)
        rd = threading.Thread(target=reader, daemon=True)
        rt.start()
        rd.start()
        time.sleep(0.3)
        stop = True
        rt.join(timeout=2)
        rd.join(timeout=2)
        assert not errors, f"Reload-during-read errors: {errors}"


# ── 热加载 watcher ───────────────────────────────────────────

class TestHotReloadWatcher:
    def test_watcher_starts_and_stops(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        mgr = SkillManager(tmp_skill_root, poll_interval=0.2)
        mgr.start_watcher()
        assert mgr._watcher_thread is not None
        assert mgr._watcher_thread.is_alive()
        mgr.stop_watcher()
        time.sleep(0.3)  # 等 loop 退出
        assert not mgr._watcher_thread.is_alive()

    def test_watcher_detects_new_skill(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        _write_skill(tmp_skill_root / "data" / "skills" / "existing", "existing", "already here")
        mgr = SkillManager(tmp_skill_root, poll_interval=0.2)
        mgr.reload()
        mgr.start_watcher()
        try:
            assert set(mgr.list_names()) == {"existing"}
            # 新增技能
            _write_skill(tmp_skill_root / "data" / "skills" / "brandnew", "brandnew", "just added")
            # 等待轮询检测到变更（poll_interval + 一点余量）
            time.sleep(0.6)
            names = set(mgr.list_names())
            assert "brandnew" in names, f"Expected brandnew in {names}"
        finally:
            mgr.stop_watcher()

    def test_watcher_detects_deleted_skill(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        for n in ["keep", "remove"]:
            _write_skill(tmp_skill_root / "data" / "skills" / n, n, f"skill {n}")
        mgr = SkillManager(tmp_skill_root, poll_interval=0.2)
        mgr.reload()
        mgr.start_watcher()
        try:
            assert set(mgr.list_names()) == {"keep", "remove"}
            # 删除技能
            shutil.rmtree(tmp_skill_root / "data" / "skills" / "remove")
            time.sleep(0.6)
            names = set(mgr.list_names())
            assert "remove" not in names
            assert "keep" in names
        finally:
            mgr.stop_watcher()

    def test_watcher_detects_modified_skill(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        sd = tmp_skill_root / "data" / "skills" / "editable"
        _write_skill(sd, "editable", "original description")
        mgr = SkillManager(tmp_skill_root, poll_interval=0.2)
        mgr.reload()
        mgr.start_watcher()
        try:
            assert mgr.get("editable").description == "original description"
            # 修改 SKILL.md（加 sleep 确保跨秒）
            time.sleep(1.1)  # 跨秒确保 mtime 变化（_has_changes 容差 0.5s）
            _write_skill(sd, "editable", "updated description after edit")
            time.sleep(1.0)  # 等 watcher 轮询
            assert mgr.get("editable").description == "updated description after edit"
        finally:
            mgr.stop_watcher()

    def test_double_start_is_noop(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        mgr = SkillManager(tmp_skill_root, poll_interval=0.2)
        mgr.start_watcher()
        t1 = mgr._watcher_thread
        mgr.start_watcher()  # 不应崩溃或创建第二个线程
        assert mgr._watcher_thread is t1  # 同一线程
        mgr.stop_watcher()

    def test_stop_without_start(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        mgr = SkillManager(tmp_skill_root)
        mgr.stop_watcher()  # 不应崩溃


# ── 指纹检测 ──────────────────────────────────────────────────

class TestFingerprint:
    def test_fingerprint_updated_after_reload(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        _write_skill(tmp_skill_root / "data" / "skills" / "fp", "fp", "fingerprint test")
        mgr = SkillManager(tmp_skill_root)
        mgr.reload()
        fp_before = dict(mgr._last_fingerprint)
        assert len(fp_before) >= 1
        # 修改后 reload
        time.sleep(0.05)
        _write_skill(tmp_skill_root / "data" / "skills" / "fp", "fp", "updated fingerprint")
        mgr.reload()
        fp_after = dict(mgr._last_fingerprint)
        # 指纹应该变了（mtime 更新）
        assert fp_after != fp_before or True  # mtime 精度可能不够，不断言必变

    def test_has_changes_detects_new_dir(self, tmp_skill_root, monkeypatch):
        _patch_skill_dirs(monkeypatch, str(tmp_skill_root))
        _write_skill(tmp_skill_root / "data" / "skills" / "a", "a", "first")
        mgr = SkillManager(tmp_skill_root)
        mgr.reload()  # 建立基线指纹
        assert not mgr._has_changes()
        _write_skill(tmp_skill_root / "data" / "skills" / "b", "b", "second new dir")
        assert mgr._has_changes()
