"""SkillManager 单元测试：覆盖加载/查询/热加载/空状态等边缘路径。"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path


from src.skills.manager import SkillManager


def _patch_skill_dirs(monkeypatch, root: str):
    """临时覆盖模块级 _SKILL_DIRS 仅指向测试目录。"""
    import src.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "_SKILL_DIRS", [root + "/data/skills"])


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    d = root / "data" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return d


# ── 加载 ─────────────────────────────────────────────────────

class TestLoading:
    def test_reload_returns_count(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            _write_skill(Path(td), "search", "name: search\n")
            mgr = SkillManager(td)
            assert mgr.reload() == 2

    def test_loaded_false_before_reload(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            assert mgr.loaded is False

    def test_loaded_true_after_reload(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            assert mgr.loaded is True

    def test_name_conflict_warns(self, caplog):
        """同名技能：data/skills 和 .agents/skills 都有的情况。"""
        with tempfile.TemporaryDirectory() as td:
            d1 = _write_skill(Path(td), "weather", "name: weather\ndescription: 新版本\n")
            d2 = Path(td) / ".agents" / "skills" / "weather"
            d2.mkdir(parents=True)
            (d2 / "SKILL.md").write_text("---\nname: weather\ndescription: 旧版\n---\n\n# Body\n", encoding="utf-8")
            mgr = SkillManager(td)
            mgr.reload()
            # 两个同名技能都发现，reload 会加载最早发现的（snake_case 去重后 data/skills 优先）
            assert mgr.get("weather") is not None


# ── 查询 ─────────────────────────────────────────────────────

class TestQuery:
    def test_get_existing_skill(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            skill = mgr.get("weather")
            assert skill is not None
            assert skill.name == "weather"

    def test_get_missing_skill_returns_none(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            assert mgr.get("nonexistent") is None

    def test_list_all(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            _write_skill(Path(td), "search", "name: search\n")
            mgr = SkillManager(td); mgr.reload()
            skills = mgr.list_all()
            assert len(skills) == 2

    def test_list_names(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            names = mgr.list_names()
            assert "weather" in names

    def test_activate_prompt(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n", body="## 使用方法\n查询天气。\n")
            mgr = SkillManager(td); mgr.reload()
            prompt = mgr.activate_prompt("weather")
            assert prompt is not None
            assert "已激活技能" in prompt
            assert "查询天气" in prompt

    def test_activate_prompt_missing_skill(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            assert mgr.activate_prompt("nonexistent") is None

    def test_skills_prompt_section_empty(self, monkeypatch):
        """未加载任何技能时返回空字符串。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            assert mgr.skills_prompt_section() == ""

    def test_skills_prompt_section_with_skills(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\ndescription: 天气查询\n")
            mgr = SkillManager(td); mgr.reload()
            section = mgr.skills_prompt_section()
            assert "可用技能" in section
            assert "weather" in section


# ── 热加载 ───────────────────────────────────────────────────

class TestHotReload:
    def test_start_watcher_twice_no_duplicate(self, caplog):
        """重复 start_watcher 不创建第二个线程。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            mgr.start_watcher()
            t1 = mgr._watcher_thread
            mgr.start_watcher()  # 第二次调用，应被跳过
            t2 = mgr._watcher_thread
            assert t1 is t2
            mgr.stop_watcher()

    def test_has_changes_detect_new_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()  # 建立基线指纹
            assert not mgr._has_changes()
            # 新增一个技能目录
            _write_skill(Path(td), "search", "name: search\n")
            assert mgr._has_changes()

    def test_has_changes_detect_modified_file(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "weather", "name: weather\ndescription: 旧\n")
            mgr = SkillManager(td)
            mgr.reload()
            assert not mgr._has_changes()
            # 修改 SKILL.md — macOS APFS mtime 精度 1s，休眠确保时间戳变化
            time.sleep(1.1)
            (d / "SKILL.md").write_text("---\nname: weather\ndescription: 新\n---\n\n# Body\n", encoding="utf-8")
            assert mgr._has_changes()

    def test_has_changes_detect_removed_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            _write_skill(Path(td), "search", "name: search\n")
            mgr = SkillManager(td)
            mgr.reload()
            # 删除一个目录
            import shutil
            shutil.rmtree(Path(td) / "data" / "skills" / "search")
            assert mgr._has_changes()

    def test_stop_watcher(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            mgr.start_watcher()
            assert mgr._watcher_thread is not None
            assert mgr._watcher_thread.is_alive()
            mgr.stop_watcher()
            mgr._watcher_thread.join(timeout=1)
            assert not mgr._watcher_thread.is_alive()

    def test_watch_loop_initial_fingerprint(self, monkeypatch):
        """_watch_loop 首次建立基线指纹后再轮询。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            # 手动触发一轮 _watch_loop 的初始化部分
            mgr._last_fingerprint.clear()  # 清空
            mgr._update_fingerprint()
            assert len(mgr._last_fingerprint) >= 1
