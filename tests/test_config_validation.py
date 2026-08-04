"""回归：config.yaml 校验日志去重（①）与固定结构自由 dict 段 key 校验（LOW#3）。

- 相同配置内容重复 load_config 仅打印一次「校验通过」，避免 Web 每个请求刷屏。
- memory.conversation_summary 等固定结构 dict 段内部拼错 key 应被捕获告警。
"""
from __future__ import annotations

import logging
import os
import tempfile

from src.config import AppConfig, load_config, validate_config_keys


def test_validate_config_keys_detects_fixed_dict_typo():
    """LOW#3：固定结构自由 dict 段（如 memory.conversation_summary）内部拼错 key 应被捕获。"""
    raw = {
        "memory": {
            "conversation_summary": {
                "enabled": True,
                "max_messages_per_conversation": 50,
                "summary_interval_hours": 24,
                "summary_ratio": 0.4,
                "summary_ratio_typo": 0.5,  # 拼错
            }
        },
        "poller": {"interval_seconds": 5},
        "web": {"auth_enabled": False},  # 合法 web 段，避免空密码触发启动校验
    }
    config = AppConfig(**raw)
    warnings = validate_config_keys(raw, config)
    assert any("conversation_summary" in w and "summary_ratio_typo" in w for w in warnings)


def test_load_config_validates_log_once_per_unique_content(caplog):
    """① 配置校验日志按内容去重：相同内容重复 load 仅打印一次。"""
    import src.config as cfg_mod
    cfg_mod._last_validated_sig = None
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("poller:\n  interval_seconds: 5\nweb:\n  auth_enabled: false\n")
        path = f.name
    try:
        with caplog.at_level(logging.INFO, logger="src.config"):
            load_config(path)
            load_config(path)  # 同内容再读
        info_lines = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "校验通过" in r.getMessage()
        ]
        assert len(info_lines) == 1, info_lines
    finally:
        os.unlink(path)
        cfg_mod._last_validated_sig = None
