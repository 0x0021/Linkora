"""P2-14 回归：config_manage update 的提示必须准确，不得谎称"下次轮询生效"。

背景：config_manage 仅把改动写入 config.yaml 磁盘，运行中的进程不会自动热加载，
绝大多数配置项需重启服务才生效。旧文案"将在下次轮询时生效"会误导运维认为无需重启。
"""

from __future__ import annotations

import yaml


from src.tools.management import ConfigManageTool


class TestConfigManageRestartHint:
    def test_update_message_mentions_restart(self, tmp_path, monkeypatch):
        config_data = {"poller": {"interval_seconds": 10, "merge_window_seconds": 3},
                       "web": {"auth_enabled": False}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)

        tool = ConfigManageTool()
        r = tool.execute({
            "action": "update",
            "section": "poller",
            "key": "interval_seconds",
            "value": "20",
        })
        assert r.get("success") is True
        msg = r.get("message", "")
        assert "重启" in msg, "应提示需重启服务"
        assert "下次轮询" not in msg, "旧误导文案不应再出现"
