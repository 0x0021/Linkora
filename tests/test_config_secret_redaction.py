"""配置导出/导入脱敏回归测试。

覆盖：
- #1 HIGH：secondary_fallback_api_key（及所有 *_api_key/*_token/*_secret 变体）必须被脱敏，
  此前 _SECRET_KEYS 遗漏 secondary_fallback_api_key，导致导出 YAML 明文泄露二级备用密钥。
- #2：导入还原哨兵时，current 应来自磁盘文件原值；若磁盘无明文（密钥走环境变量），
  哨兵还原为空串，绝不把 env 注入的真实密钥落盘。
"""
from __future__ import annotations

from web.routers.config import (
    REDACTED_SENTINEL,
    _is_secret_key,
    _redact_secrets,
    _restore_secrets,
)


def test_secondary_fallback_api_key_is_redacted():
    dump = {"llm": {"secondary_fallback_api_key": "sk-secret-123"}}
    out = _redact_secrets(dump)
    assert out["llm"]["secondary_fallback_api_key"] == REDACTED_SENTINEL


def test_secret_key_suffixes_covered():
    dump = {
        "a": {"my_api_key": "x", "svc_token": "y", "db_secret": "z", "admin_password": "p"},
    }
    out = _redact_secrets(dump)
    assert out["a"]["my_api_key"] == REDACTED_SENTINEL
    assert out["a"]["svc_token"] == REDACTED_SENTINEL
    assert out["a"]["db_secret"] == REDACTED_SENTINEL
    assert out["a"]["admin_password"] == REDACTED_SENTINEL


def test_non_secret_fields_not_redacted():
    dump = {"llm": {"model": "glm-4", "base_url": "https://x/v1"}}
    out = _redact_secrets(dump)
    assert out["llm"]["model"] == "glm-4"
    assert out["llm"]["base_url"] == "https://x/v1"


def test_is_secret_key_helper():
    assert _is_secret_key("secondary_fallback_api_key")
    assert _is_secret_key("api_key")
    assert _is_secret_key("hf_token")
    assert not _is_secret_key("model")
    assert not _is_secret_key("base_url")


def test_restore_secrets_from_empty_disk_keeps_redacted():
    """#2：磁盘无明文（密钥走 env）时，导入文件里的哨兵应还原为空串，
    绝不可被 env 真实密钥填充落盘。"""
    imported = {"llm": {"api_key": REDACTED_SENTINEL}}
    current = {"llm": {}}  # 磁盘 YAML 中 api_key 为空（env 提供，不落盘）
    _restore_secrets(imported, current)
    assert imported["llm"]["api_key"] == ""


def test_restore_secrets_from_disk_plaintext():
    """磁盘原本就有明文密钥时，哨兵还原为磁盘明文（保持 round-trip 兼容）。"""
    imported = {"llm": {"api_key": REDACTED_SENTINEL}}
    current = {"llm": {"api_key": "disk-plain-123"}}
    _restore_secrets(imported, current)
    assert imported["llm"]["api_key"] == "disk-plain-123"


def test_apply_wecom_platform_writes_credentials():
    """D-1 回归：Web 提交的企微凭证必须真正写入 platforms[wecom].adapter，
    不再被 _apply_wecom_platform 空壳静默丢弃。"""
    from web.routers.config import _apply_wecom_platform
    from web.schemas import ConfigUpdate
    from src.config import AppConfig

    cfg = AppConfig(web={"auth_enabled": False})
    update = ConfigUpdate(
        wecom_corp_id="wwabcd",
        wecom_corp_secret="SECRET_X",
        wecom_agent_id="1000002",
        wecom_token="TK",
        wecom_encoding_aes_key="AESKEY",
    )
    _apply_wecom_platform(update, cfg)

    wecom = next(p for p in cfg.platforms if p.id == "wecom")
    assert wecom.adapter.wecom_corp_id == "wwabcd"
    assert wecom.adapter.wecom_corp_secret == "SECRET_X"
    assert wecom.adapter.wecom_agent_id == "1000002"
    assert wecom.adapter.wecom_token == "TK"
    assert wecom.adapter.wecom_encoding_aes_key == "AESKEY"


def test_apply_wecom_platform_keeps_existing_on_blank():
    """空串/None 不应覆盖已保存的企微凭证（避免空白表单在另一次保存时误清）。"""
    from web.routers.config import _apply_wecom_platform
    from web.schemas import ConfigUpdate
    from src.config import AppConfig

    cfg = AppConfig(web={"auth_enabled": False})
    _apply_wecom_platform(
        ConfigUpdate(wecom_corp_id="wwabcd", wecom_corp_secret="SECRET_X"), cfg
    )
    # 第二次只提交 agent_id，corp_id 留空串、corp_secret 留 None
    _apply_wecom_platform(
        ConfigUpdate(wecom_agent_id="1000002", wecom_corp_id="", wecom_corp_secret=None),
        cfg,
    )

    wecom = next(p for p in cfg.platforms if p.id == "wecom")
    assert wecom.adapter.wecom_corp_id == "wwabcd", "空串不应清掉已保存的 corp_id"
    assert wecom.adapter.wecom_corp_secret == "SECRET_X", "None 不应清掉已保存的 corp_secret"
    assert wecom.adapter.wecom_agent_id == "1000002"
