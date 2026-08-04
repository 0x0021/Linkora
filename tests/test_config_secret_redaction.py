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
