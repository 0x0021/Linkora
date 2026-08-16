"""T-A3: 配置全量落盘副作用修复验证。

修复前 _save_and_reload 走 new_config.model_dump() 全量 dump，会把 Pydantic 默认值
（如整个 poller 段，live 配置本无此段）静默注入 config.yaml。修复后改为直接读盘原始
YAML dict，只改写 llm.persona_style_prompt / llm.persona_style_prompts 两个目标字段，
其余 key 原样保留。

核心验收：保存口吻覆盖后，配置文件除目标字段外的 key 集合完全不变（不注入 poller 段）。

全程使用 tmp_path 临时配置，绝不触碰真实 config.yaml。
"""
from __future__ import annotations

import yaml

from src.config import AppConfig
from web.routers import persona


_SAMPLE_DISK = {
    "dws": {"cli_path": "/usr/bin/true"},
    "llm": {
        "api_key": "",
        "model": "gpt-4o",
        "persona_style_prompt": "原始全局口吻",
        "persona_style_prompts": {"dingtalk": "钉钉专属口吻"},
    },
    "logging": {"level": "INFO"},
    # live 配置 web.auth_enabled 通常关闭；此处显式关掉，避免 AppConfig 校验
    # （auth_enabled=True 且 auth_password 为空会被安全默认拒绝启动）影响测试构造。
    "web": {"auth_enabled": False},
    # 注意：live 配置根本没有 root 级 poller（poller 在 platform 块内），
    # 此处刻意不写 root poller，以验证不会从默认值注入。
}


def _setup_tmp_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(_SAMPLE_DISK, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr("web.api.CONFIG_PATH", str(cfg_file))
    # 备份目录也重定向到 tmp，避免污染真实 data/config-backups/
    # 注意：CONFIG_BACKUP_ROOT 是 Path 对象（data_path 返回 Path），不能用 str。
    monkeypatch.setattr("web.api.CONFIG_BACKUP_ROOT", tmp_path / "backups")
    return cfg_file


def test_save_global_override_keeps_key_set_unchanged(tmp_path, monkeypatch):
    """保存全局口吻覆盖后：除目标字段外 key 集合完全不变，且不注入 poller 段。"""
    cfg_file = _setup_tmp_config(tmp_path, monkeypatch)

    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    new_config = AppConfig(**data)
    new_config.llm.persona_style_prompt = "新的全局口吻覆盖"

    persona._save_and_reload(new_config)

    written = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    # 1) 绝不注入 root poller 段
    assert "poller" not in written
    # 2) 顶层 key 集合完全不变
    assert set(written.keys()) == set(_SAMPLE_DISK.keys())
    # 3) llm 子 key 集合完全不变（仅 persona_style_prompt 值变化）
    assert set(written["llm"].keys()) == set(_SAMPLE_DISK["llm"].keys())
    assert written["llm"]["persona_style_prompt"] == "新的全局口吻覆盖"
    # 4) 其他 llm 字段原样保留（未被默认值覆盖）
    assert written["llm"]["api_key"] == ""
    assert written["llm"]["model"] == "gpt-4o"
    assert written["llm"]["persona_style_prompts"] == {"dingtalk": "钉钉专属口吻"}


def test_save_platform_override_keeps_key_set_unchanged(tmp_path, monkeypatch):
    """保存按平台口吻覆盖后：persona_style_prompts 整体写入，但 key 集合仍不变。"""
    cfg_file = _setup_tmp_config(tmp_path, monkeypatch)

    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    new_config = AppConfig(**data)
    new_config.llm.persona_style_prompts = {
        "dingtalk": "钉钉专属口吻",
        "wecom": "企微专属口吻",
    }

    persona._save_and_reload(new_config)

    written = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert "poller" not in written
    assert set(written.keys()) == set(_SAMPLE_DISK.keys())
    assert set(written["llm"].keys()) == set(_SAMPLE_DISK["llm"].keys())
    # 平台覆盖整体保留（含新增的 wecom 键），原 dingtalk 键不变
    assert written["llm"]["persona_style_prompts"] == {
        "dingtalk": "钉钉专属口吻",
        "wecom": "企微专属口吻",
    }
    # 全局口吻未被改动
    assert written["llm"]["persona_style_prompt"] == "原始全局口吻"
