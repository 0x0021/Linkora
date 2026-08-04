"""CLI 版本自检与后台更新模块的单测。

通过 mock ``subprocess.run`` 与 ``_resolve_binary``，覆盖：
  - 版本解析
  - 首跑记录
  - 官方有更新（各 CLI 自带 check_cmd）→ 后台执行自带 update_cmd
  - 本地版本升级检测（changed）
  - 未安装跳过
"""

from __future__ import annotations

import subprocess

import pytest

import src.utils.cli_version_checker as vc


VERSION_MAP = {
    "wecom-cli": "wecom-cli 0.1.9",
    "lark-cli": "lark-cli version 1.0.78",
    "dws": "dws version v1.0.49 (b794d80, 2026-07-07T16:17:29Z)",
}


def _make_fake_run(upstream_names=(), upgrade_ok=True):
    """构造 subprocess.run 替身：按各 CLI 真实命令分发返回。

    - ``<bin> --version`` → 返回 VERSION_MAP 中对应版本
    - ``lark-cli update --check --json`` → JSON（含 outdated / latest）
    - ``dws upgrade --check`` → 文本（关键词命中表示有更新）
    - ``npm view @wecom/cli version`` → 纯版本号
    - 各 CLI 的 update 命令 → returncode 0/1（按 upgrade_ok）
    """
    def _run(cmd, *a, **k):
        c = list(cmd)
        name = c[0].split("/")[-1]
        if c and c[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, VERSION_MAP.get(name, ""), "")
        # lark-cli update --check --json
        if name == "lark-cli" and "update" in c and "--check" in c:
            if "lark-cli" in upstream_names:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"current":"1.0.78","latest":"1.0.80","outdated":true}', "")
            return subprocess.CompletedProcess(
                cmd, 0, '{"current":"1.0.78","latest":"1.0.78","outdated":false}', "")
        # dws upgrade --check
        if name == "dws" and "upgrade" in c and "--check" in c:
            if "dws" in upstream_names:
                return subprocess.CompletedProcess(cmd, 0, "检查完成：发现有新版本可升级", "")
            return subprocess.CompletedProcess(cmd, 0, "已是最新版本", "")
        # npm view @wecom/cli version
        if c[:3] == ["npm", "view", "@wecom/cli"]:
            ver = "0.2.0" if "wecom-cli" in upstream_names else "0.1.9"
            return subprocess.CompletedProcess(cmd, 0, ver, "")
        # 各 CLI 的 update 命令（lark-cli update / dws upgrade -y / npm install -g ...）
        if (name == "lark-cli" and "update" in c and "--check" not in c) or \
           (name == "dws" and "upgrade" in c and "--check" not in c) or \
           (c[:2] == ["npm", "install"]):
            return subprocess.CompletedProcess(cmd, 0 if upgrade_ok else 1, "ok", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return _run


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(vc, "_resolve_binary", lambda name: f"/fake/{name}")
    yield


def test_fetch_version_parses(patched, monkeypatch):
    monkeypatch.setattr(vc.subprocess, "run", _make_fake_run())
    assert vc.fetch_version(vc.CLI_DEFINITIONS["wecom-cli"], "/fake/wecom-cli") == "0.1.9"
    assert vc.fetch_version(vc.CLI_DEFINITIONS["lark-cli"], "/fake/lark-cli") == "1.0.78"
    assert vc.fetch_version(vc.CLI_DEFINITIONS["dws"], "/fake/dws") == "1.0.49"


def test_first_run_records_all(tmp_path, patched, monkeypatch):
    from src.paths import set_data_dir
    set_data_dir(str(tmp_path / "data"))
    monkeypatch.setattr(vc.subprocess, "run", _make_fake_run())
    result = vc.run_checks(str(tmp_path))
    assert result["wecom-cli"]["status"] == "recorded"
    assert result["lark-cli"]["status"] == "recorded"
    assert result["dws"]["status"] == "recorded"
    # 状态文件已写入
    state_file = tmp_path / "data" / "cli_versions.json"
    assert state_file.is_file()
    import json
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["wecom-cli"]["installed"] == "0.1.9"


def test_upstream_update_triggers_upgrade(tmp_path, patched, monkeypatch):
    from src.paths import set_data_dir
    set_data_dir(str(tmp_path / "data"))
    # wecom-cli 官方有更新（npm 版本号 0.2.0 > 已装 0.1.9）→ 后台 npm i -g
    # dws 官方有更新（关键词命中）→ 后台 dws upgrade -y
    monkeypatch.setattr(vc.subprocess, "run",
                        _make_fake_run(upstream_names=("wecom-cli", "dws")))
    result = vc.run_checks(str(tmp_path))
    assert result["wecom-cli"]["status"] == "update_available"
    assert result["wecom-cli"]["update_status"] == "updated"
    assert result["dws"]["status"] == "update_available"
    assert result["dws"]["update_status"] == "updated"
    # lark-cli 无上游更新（首跑）→ 仅记录
    assert result["lark-cli"]["status"] == "recorded"


def test_lark_json_check_parses_outdated(tmp_path, patched, monkeypatch):
    from src.paths import set_data_dir
    set_data_dir(str(tmp_path / "data"))
    # lark-cli 官方有更新（JSON latest > current）→ 后台 lark-cli update
    monkeypatch.setattr(vc.subprocess, "run", _make_fake_run(upstream_names=("lark-cli",)))
    result = vc.run_checks(str(tmp_path))
    assert result["lark-cli"]["status"] == "update_available"
    assert result["lark-cli"]["update_status"] == "updated"


def test_local_upgrade_detected_as_changed(tmp_path, patched, monkeypatch):
    # 预置旧版本状态（wecom-cli 0.1.8），且上游无更新
    state = {"wecom-cli": {"installed": "0.1.8", "previous": "0.1.8", "last_checked": "x"}}
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "cli_versions.json").write_text(
        __import__("json").dumps(state), encoding="utf-8")
    # 当前版本升到 0.2.0，上游无更新
    local_map = dict(VERSION_MAP)
    local_map["wecom-cli"] = "wecom-cli 0.2.0"
    monkeypatch.setattr(vc, "VERSION_MAP", local_map) if hasattr(vc, "VERSION_MAP") else None
    # 通过替换 _make_fake_run 的 VERSION_MAP 不行（闭包），改用环境变量式：直接 monkeypatch fetch 用的值
    import src.utils.cli_version_checker as vc2
    monkeypatch.setattr(vc2, "CLI_DEFINITIONS", dict(vc2.CLI_DEFINITIONS))
    # 直接 patch fetch_version 行为：让 wecom-cli 返回 0.2.0
    orig_fetch = vc2.fetch_version
    def fake_fetch(spec, binary, timeout=15):
        if spec.name == "wecom-cli":
            return "0.2.0"
        return orig_fetch(spec, binary, timeout)
    monkeypatch.setattr(vc2, "fetch_version", fake_fetch)
    monkeypatch.setattr(vc2.subprocess, "run", _make_fake_run(upstream_names=()))
    result = vc2.run_checks(str(tmp_path))
    assert result["wecom-cli"]["status"] == "changed"
    # 无上游更新、非首跑 → 不应触发 brew upgrade
    assert "update_status" not in result["wecom-cli"]


def test_not_installed_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_resolve_binary", lambda name: None)
    monkeypatch.setattr(vc.subprocess, "run", _make_fake_run())
    result = vc.run_checks(str(tmp_path))
    assert result["wecom-cli"]["status"] == "not_installed"
    assert result["dws"]["status"] == "not_installed"
