"""dev 模式「文件变更热重启」的合抖判定测试。

回归来源：原实现在 3s 合抖窗口后用 ``cur2 != last``（上一轮基线）判断「是否仍在
变化」，而变更一旦发生，last 与当前签名**必然不同** → 判定恒为「仍在变化」→
重启分支永远不可达，`--dev` 热重启静默失效（线上日志 0 次「检测到文件变更」，
改完代码毫无反应，只有手动重启才生效）。
"""

from __future__ import annotations

from src.platform.lifecycle import _dev_change_settled


class TestDevChangeSettled:
    def test_settled_when_signature_stops_changing(self):
        """等待窗口内签名不再变化 → 判定为已落定（可热重启）。"""
        assert _dev_change_settled(2.0, 2.0) is True

    def test_still_changing_when_signature_keeps_moving(self):
        """等待窗口内又变了 → 不重启，等下一轮。"""
        assert _dev_change_settled(2.0, 3.0) is False

    def test_must_not_compare_against_previous_baseline(self):
        """锁定回归：变更已落定（cur2 == cur）必须判定为可重启。

        旧实现拿 last（=1.0，变更前的基线）去比，会错误地返回「仍在变化」，
        导致热重启永不触发。
        """
        baseline, detected, after_debounce = 1.0, 2.0, 2.0
        assert detected != baseline  # 前置：确实发生了变更
        assert _dev_change_settled(detected, after_debounce) is True

    def test_zero_signature_counts_as_settled(self):
        """监视目录被清空（签名回落到 0.0）同样算落定，不得被当成「仍在变化」。"""
        assert _dev_change_settled(0.0, 0.0) is True
