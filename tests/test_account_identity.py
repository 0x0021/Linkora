"""账号身份解析器单测：三平台分支 + 兜底 + 缓存。"""

import subprocess


import src.memory.account_identity as ai


def _fake_run(stdout_map):
    """构造 subprocess.run 替身：按首个参数分发的返回。"""
    def _run(cmd, *a, **k):
        c = list(cmd)
        name = c[0].split("/")[-1]
        if name in stdout_map:
            return subprocess.CompletedProcess(cmd, 0, stdout_map[name], "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return _run


def test_feishu_appid(monkeypatch):
    monkeypatch.setattr(ai, "_find_cli", lambda p: "/fake/lark-cli")
    monkeypatch.setattr(ai.subprocess, "run", _fake_run({
        "lark-cli": '{"appId":"cli_aae219493f389bea","onBehalfOf":{"openId":"ou_abc"}}',
    }))
    ai.invalidate_cache()
    assert ai.resolve_account_id("feishu") == "feishu:cli_aae219493f389bea"


def test_feishu_cli_missing_fallback(monkeypatch):
    monkeypatch.setattr(ai, "_find_cli", lambda p: None)
    ai.invalidate_cache()
    assert ai.resolve_account_id("feishu") == "feishu:unknown"


def test_dingtalk_active_profile(monkeypatch):
    monkeypatch.setattr(ai, "_find_cli", lambda p: "/fake/dws")
    # 激活 profile 以 JSON 格式返回
    monkeypatch.setattr(ai.subprocess, "run", _fake_run({
        "dws": '{"primaryProfile": {"corpId": "corp1234567890", "name": "Acme Corp"}, "profiles": [{"corpId": "corp0987654321", "name": "Other Corp"}]}',
    }))
    ai.invalidate_cache()
    assert ai.resolve_account_id("dingtalk") == "dingtalk:corp1234567890"


def test_dingtalk_fallback_corp_id(monkeypatch):
    monkeypatch.setattr(ai, "_find_cli", lambda p: None)
    ai.invalidate_cache()
    assert ai.resolve_account_id("dingtalk", fallback_corp_id="corpCfg") == "dingtalk:corpCfg"


def test_wecom_sha256(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp_config.enc"
    cfg.write_bytes(b"some-encrypted-bytes")
    monkeypatch.setattr(ai, "_WECHAT_CFG_CANDIDATES", [str(cfg)])
    ai.invalidate_cache()
    import hashlib
    expected = "wecom:" + hashlib.sha256(b"some-encrypted-bytes").hexdigest()[:16]
    assert ai.resolve_account_id("wecom") == expected


def test_wecom_no_config_fallback(monkeypatch):
    monkeypatch.setattr(ai, "_WECHAT_CFG_CANDIDATES", ["/nonexistent/path.enc"])
    ai.invalidate_cache()
    assert ai.resolve_account_id("wecom") == "wecom"


def test_cache_avoids_repeated_shell(monkeypatch):
    calls = {"n": 0}

    def _run(cmd, *a, **k):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, '{"appId":"cli_x"}', "")
    monkeypatch.setattr(ai, "_find_cli", lambda p: "/fake/lark-cli")
    monkeypatch.setattr(ai.subprocess, "run", _run)
    ai.invalidate_cache()
    assert ai.resolve_account_id("feishu") == "feishu:cli_x"
    assert ai.resolve_account_id("feishu") == "feishu:cli_x"
    assert calls["n"] == 1  # 第二次命中缓存，未再 shell-out
