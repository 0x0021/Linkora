"""F12 加固回归：skillhub CLI 自动安装的安全边界。

选项 B：默认关闭 + 白名单 URL + SHA256 钉值校验 + 非 shell 执行 + fail-closed。
"""
import hashlib
from pathlib import Path
from unittest import mock

import pytest

from src.config import AppConfig, SkillHubConfig
from web import dependencies as dep


@pytest.fixture
def no_skillhub_on_path():
    with mock.patch("web.dependencies.shutil.which", return_value=None):
        yield


def test_auto_install_disabled_by_default(no_skillhub_on_path):
    """默认未启用自动安装时，绝不触发远端拉取，返回明确指引。"""
    cfg = AppConfig(web={"auth_enabled": False})  # skillhub.auto_install 默认 False
    with mock.patch.object(dep, "SKILLHUB_INSTALL_SHA256", ""), \
         mock.patch("web.api._get_cfg", return_value=cfg):
        ok, err = dep._ensure_skillhub_cli()
    assert ok is False
    assert "auto_install=false" in err


def test_auto_install_enabled_but_no_pin_fails_closed(no_skillhub_on_path):
    """开启自动安装但未钉死 SHA256 时，fail-closed 拒绝（不退化成 curl|bash）。"""
    cfg = AppConfig(skillhub=SkillHubConfig(auto_install=True), web={"auth_enabled": False})
    with mock.patch.object(dep, "SKILLHUB_INSTALL_SHA256", ""), \
         mock.patch("web.api._get_cfg", return_value=cfg):
        ok, err = dep._ensure_skillhub_cli()
    assert ok is False
    assert "SHA256" in err


def test_install_url_whitelist():
    assert dep._skillhub_install_url_allowed(dep.SKILLHUB_INSTALL_URL) is True
    assert dep._skillhub_install_url_allowed("https://evil.example.com/x.sh") is False
    assert dep._skillhub_install_url_allowed("not-a-url") is False


def test_sha256_mismatch_rejected(no_skillhub_on_path):
    """脚本哈希与钉值不符时拒绝执行。"""
    cfg = AppConfig(skillhub=SkillHubConfig(auto_install=True), web={"auth_enabled": False})
    fake_script = b"echo malicious\n"
    with mock.patch.object(dep, "SKILLHUB_INSTALL_SHA256", "deadbeef" * 8), \
         mock.patch("web.api._get_cfg", return_value=cfg), \
         mock.patch("web.dependencies._download_to_file",
                    side_effect=lambda url, dest, timeout=60: dest.write_bytes(fake_script)):
        ok, err = dep._ensure_skillhub_cli()
    assert ok is False
    assert "SHA256 校验失败" in err


def test_auto_install_success_runs_non_shell(no_skillhub_on_path):
    """全链路 happy path：下载→校验通过→以非 shell 列表形式执行安装脚本。"""
    cfg = AppConfig(skillhub=SkillHubConfig(auto_install=True), web={"auth_enabled": False})
    fake_script = b"#!/bin/bash\necho ok\n"
    sha = hashlib.sha256(fake_script).hexdigest()
    captured = {}
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return mock.Mock(returncode=0, stderr="")

    def which_side(x):
        # 首次（顶部「已安装」检查）返回 None；安装完成后的复检返回路径
        calls["n"] += 1
        return None if calls["n"] == 1 else "/usr/local/bin/skillhub"

    with mock.patch.object(dep, "SKILLHUB_INSTALL_SHA256", sha), \
         mock.patch("web.api._get_cfg", return_value=cfg), \
         mock.patch("web.dependencies._download_to_file",
                    side_effect=lambda url, dest, timeout=60: dest.write_bytes(fake_script)), \
         mock.patch("web.dependencies.subprocess.run", side_effect=fake_run), \
         mock.patch("web.dependencies.shutil.which", side_effect=which_side):
        ok, err = dep._ensure_skillhub_cli()

    assert ok is True
    # 非 shell 执行：命令是列表而非字符串；不得出现 shell=True 式的字符串拼接
    assert isinstance(captured["cmd"], list)
    assert captured["cmd"][0] == "bash"
    assert captured["cmd"][-1] == "--cli-only"
