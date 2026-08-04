"""测试 config.py 边缘路径。"""

import pytest
from src.config import load_config


def test_load_config_file_not_found():
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        load_config("/tmp/nonexistent_config_file_12345.yaml")


def test_validate_config_keys_clean():
    """合法 config 不应产生任何未知 key 警告。"""
    from src.config import AppConfig, validate_config_keys

    raw = {
        "tools": {"enabled": True, "available": ["kb_search"]},
        "llm": {"model": "x", "temperature": 0.5},
        # fail-closed 兼容：提供合法 web 段，避免空密码触发启动校验（本测试只验 key 拼写）
        "web": {"auth_enabled": False},
    }
    config = AppConfig(**raw)
    warnings = validate_config_keys(raw, config)
    assert warnings == []


def test_validate_config_keys_detects_typo():
    """拼错的 key（如 llm_throtle）应被捕获为未知 key。"""
    from src.config import AppConfig, validate_config_keys

    raw = {
        "llm_throtle": {"max_retries": 3},  # 拼写错误，正确应为 llm_throttle
        "tools": {"enabled": True},
        "web": {"auth_enabled": False},  # 合法 web 段，避免空密码触发启动校验
    }
    config = AppConfig(**raw)
    warnings = validate_config_keys(raw, config)
    assert len(warnings) == 1
    assert "llm_throtle" in warnings[0]
    assert "root" in warnings[0]


def test_validate_config_keys_detects_nested_typo():
    """嵌套段内的拼错 key 也应被定位到对应层级。"""
    from src.config import AppConfig, validate_config_keys

    raw = {"tools": {"allow_skill_tools": True, "availabel": ["x"]},  # availabel 拼错
           "web": {"auth_enabled": False}}
    config = AppConfig(**raw)
    warnings = validate_config_keys(raw, config)
    assert len(warnings) == 1
    assert "root.tools" in warnings[0]
    assert "availabel" in warnings[0]


def test_validate_config_keys_ignores_known_lists():
    """list 类型字段（如 tools.available）的值不应被误判为未知 key。"""
    from src.config import AppConfig, validate_config_keys

    raw = {"tools": {"available": ["kb_search", "search_doc"]},
           "web": {"auth_enabled": False}}
    config = AppConfig(**raw)
    warnings = validate_config_keys(raw, config)
    assert warnings == []


def test_poller_config_list_all_paging_defaults():
    """list-all 分页上限与窗口钳制默认值合理（可经 config 调，但应有 sane 默认）。"""
    from src.config import PollerConfig

    cfg = PollerConfig()
    assert cfg.list_all_max_pages == 50          # 原硬编码 20，活跃群易触顶
    assert cfg.list_all_max_window_days == 14    # 实时轮询窗口上限，深度回填走 sync_history
    assert cfg.list_all_time_window_minutes == 30
    assert cfg.list_all_first_run_minutes == 5
