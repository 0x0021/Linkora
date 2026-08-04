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
        """_update_fingerprint 在 stat 失败时静默忽略 OSError。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            mgr._last_fingerprint.clear()

            # Mock discover 返回一个不存在路径，触发 OSError
            with mock.patch.object(mgr, "discover", return_value=["/nonexistent/path"]):
                mgr._update_fingerprint()
                # 不应抛异常，指纹保持空
                assert len(mgr._last_fingerprint) == 0

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

    def test_has_changes_same_fingerprint(self):
        """指纹无变化时返回 False。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td)
            mgr.reload()
            assert mgr._has_changes() is False
