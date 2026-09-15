"""SkillManager 补充测试：空目录回退、OSError 处理、stop_watcher 未启动线程等边缘路径。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock


from src.skills.manager import SkillManager


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    d = root / "data" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return d


class TestHotReloadEdge:
    def test_stop_watcher_not_started(self):
        """未启动热加载时 stop_watcher 不崩溃。"""
        with tempfile.TemporaryDirectory() as td:
            mgr = SkillManager(td)
            mgr.stop_watcher()  # 应无异常

    def test_start_watcher_after_stop(self):
        """stop 后重新 start 应能正常工作。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            mgr.start_watcher()
            mgr.stop_watcher()
            mgr._watcher_thread.join(timeout=2)
            # 线程已停止，重新启动
            mgr.start_watcher()
            assert mgr._watcher_thread.is_alive()
            mgr.stop_watcher()

    def test_update_fingerprint_empty_dir(self):
        """空技能目录下 _update_fingerprint 使用目录自身的 mtime。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data" / "skills" / "empty_dir"
            d.mkdir(parents=True, exist_ok=True)
            mgr = SkillManager(td)
            mgr._last_fingerprint.clear()
            # 即使 discover 返回此目录，内部无文件时走 default=p.stat().st_mtime
            mgr._update_fingerprint()
            # 空指纹（因为 discover 不会返回无 SKILL.md 的目录
            # 但 _update_fingerprint 本身会处理 discover 返回的目录）

    def test_update_fingerprint_oserror(self):
        """stat 失败时记账为 0.0（而非跳过该目录），避免与 _has_changes 互摆。

        旧实现在 _update_fingerprint 里「跳过」失败目录（不留 key），而
        _has_changes 里记为 0.0（留 key）→ 两边目录集合恒不一致 → 每一轮轮询
        都判定「有变更」并触发 reload，形成死循环。统一记 0.0 后两边一致。
        """
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()

            with mock.patch.object(mgr, "discover", return_value=["/nonexistent/path"]):
                mgr._update_fingerprint()
                assert mgr._last_fingerprint == {"/nonexistent/path": 0.0}
                # 同一 discover 结果下不得再判为「有变更」（防轮询死循环）
                assert mgr._has_changes() is False

    def test_pycache_does_not_trigger_reload(self, monkeypatch):
        """技能自带脚本执行生成 __pycache__ 不得触发热重载。

        注意：SkillManager.discover() 走模块级 _SKILL_DIRS（含项目真实技能目录），
        故必须 patch 它，否则断言的对象是开发机上的真实技能，而非本测试的临时目录。
        """
        import os
        import src.skills.loader as loader_mod

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setattr(loader_mod, "_SKILL_DIRS", [td + "/data/skills"])
            d = _write_skill(Path(td), "weather", "name: weather\n")
            scripts = d / "scripts"
            scripts.mkdir()
            (scripts / "run.py").write_text("print(1)", encoding="utf-8")
            mgr = SkillManager(td)
            mgr.reload()
            assert mgr.discover()  # 前置：确认扫到的是本测试的技能目录
            assert mgr._has_changes() is False

            # 模拟脚本被执行：生成字节码缓存
            cache = scripts / "__pycache__"
            cache.mkdir()
            (cache / "run.cpython-314.pyc").write_bytes(b"\x00")
            assert mgr._has_changes() is False

            # 对照：技能目录里多出一个 __pycache__ 目录本身也不得被当成新技能
            assert all("__pycache__" not in p for p in mgr.discover())

            # 对照：真正修改技能文件仍必须被检出
            (d / "SKILL.md").write_text("---\nname: weather\ndescription: 改了\n---\n")
            os.utime(d / "SKILL.md", (9e9, 9e9))   # 显式拉开 mtime，避免同秒抖动
            assert mgr._has_changes() is True

    def test_has_changes_oserror_stat(self):
        """_has_changes 在 stat 失败时将 mtime 设为 0.0。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            # Mock discover 返回一个不存在路径
            with mock.patch.object(mgr, "discover", return_value=["/nonexistent/path"]):
                result = mgr._has_changes()
                # 新目录集与指纹不同 → 有变化
                assert result is True

    def test_same_name_keeps_higher_priority_dir(self, monkeypatch):
        """同名技能按 _SKILL_DIRS 顺序取先出现者：data/skills 覆盖 src/skills。

        回归来源：原实现「后加载覆盖先加载」，而 src/skills 排在 _SKILL_DIRS 末位
        → 用户放进 data/skills 的自定义副本被静默忽略，与 loader 文档声明的
        「data/skills 优先级最高」正好相反。
        """
        import src.skills.loader as loader_mod

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setattr(
                loader_mod, "_SKILL_DIRS",
                [td + "/data/skills", td + "/src/skills"],
            )
            user_dir = _write_skill(Path(td), "dup", "name: dup\ndescription: 用户自定义")
            builtin_dir = Path(td) / "src" / "skills" / "dup"
            builtin_dir.mkdir(parents=True)
            (builtin_dir / "SKILL.md").write_text(
                "---\nname: dup\ndescription: 仓库内置\n---\n", encoding="utf-8"
            )

            mgr = SkillManager(td)
            assert mgr.reload() == 1          # 同名去重后仍是 1 个技能
            loaded = mgr.get("dup")
            assert loaded is not None
            assert loaded.description == "用户自定义"
            # discover 会对目录做 resolve（macOS 上 /var -> /private/var），故按 realpath 比
            assert loaded.source_path == str((user_dir / "SKILL.md").resolve())
            assert "src/skills" not in loaded.source_path

    def test_has_changes_same_fingerprint(self):
        """指纹无变化时返回 False。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            assert mgr._has_changes() is False
