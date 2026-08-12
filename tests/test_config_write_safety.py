"""config.yaml 写入安全回归（P0 配置红线）。

锁死历史事故「config.yaml 被整体覆盖成 example 模板，丢全部真实定制」：
断言 config_manage 的 update 动作是「加载 → 单键合入 → 原子写」，
而非「用默认/模板 dict 整体 dump 覆盖」，因此改一个键不会丢其他键（含未知 key）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

import src.tools.management as mgt
from src.tools.management import ConfigManageTool

REPO_ROOT = Path(__file__).resolve().parents[1]


def _base_config() -> dict:
    # 以真实 example 模板为基准（合法），保证 load_config 校验通过
    example = yaml.safe_load(
        (REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8")
    )
    # 关闭 web 鉴权，避免触发 auth_password 必填校验（与保参测试无关）
    example.setdefault("web", {})["auth_enabled"] = False
    # 显式注入 update 所需的字段，避免 example 结构漂移导致 key 不存在
    example.setdefault("llm", {})["temperature"] = 0.5
    example.setdefault("tools", {})["enabled"] = True
    example.setdefault("tools", {})["available"] = ["a", "b"]
    example.setdefault("platforms", [{"id": "dingtalk", "poller": {"interval_seconds": 30}}])
    return example


def _write_tmp_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return p


def test_update_preserves_all_other_keys(monkeypatch, tmp_path):
    cfg = _base_config()
    cfg["my_unknown_flag"] = "keep-me"  # 未知 key，绝不能被整体覆盖丢弃
    p = _write_tmp_config(tmp_path, cfg)
    monkeypatch.setattr(mgt, "get_config_path", lambda: str(p))

    res = ConfigManageTool().execute(
        {
            "action": "update",
            "section": "llm",
            "key": "temperature",
            "value": "0.3",
        }
    )
    assert res.get("success") is True

    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    # 改的键生效
    assert reloaded["llm"]["temperature"] == 0.3
    # 同段其他键保留
    assert reloaded["llm"]["model"]
    # 其他段保留
    assert reloaded["tools"]["enabled"] is not None
    # 未知 key 保留（P0 红线核心：整体覆盖会丢它）
    assert reloaded.get("my_unknown_flag") == "keep-me"
    # 平台块保留
    assert any(pl.get("id") == "dingtalk" for pl in reloaded.get("platforms", []))


def test_update_merges_section_not_overwrite(monkeypatch, tmp_path):
    cfg = _base_config()
    p = _write_tmp_config(tmp_path, cfg)
    monkeypatch.setattr(mgt, "get_config_path", lambda: str(p))

    res = ConfigManageTool().execute(
        {
            "action": "update",
            "section": "tools",
            "key": "enabled",
            "value": True,
        }
    )
    assert res.get("success") is True

    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["tools"]["enabled"] is True
    # 同段其他键不被整体覆盖丢掉
    assert reloaded["tools"]["available"] == ["a", "b"]
