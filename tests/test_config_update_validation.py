"""update_config 落盘前敏感字段校验回归测试。

验证 web.routers.config._validate_update_config：
- web.port 越界/特权端口被拒（400）
- storage.path / logging.file / storage.backup_dir 禁止写入系统路径
- 合法路径与端口通过校验
"""
import os
import tempfile

import pytest


def _cfg(**kw):
    from web.schemas import ConfigUpdate
    return ConfigUpdate(**kw)


def test_rejects_port_over_range():
    from web.routers.config import _validate_update_config
    with pytest.raises(Exception) as e:
        _validate_update_config(_cfg(web_port=70000))
    assert e.value.status_code == 400


def test_rejects_privileged_port():
    from web.routers.config import _validate_update_config
    with pytest.raises(Exception) as e:
        _validate_update_config(_cfg(web_port=80))
    assert e.value.status_code == 400


def test_rejects_forbidden_path():
    from web.routers.config import _validate_update_config
    with pytest.raises(Exception) as e:
        _validate_update_config(_cfg(storage_path="/etc/passwd"))
    assert e.value.status_code == 400


def test_accepts_valid_path_and_port():
    from web.routers.config import _validate_update_config
    d = tempfile.mkdtemp()
    target = os.path.join(d, "db.sqlite")
    # 不抛异常即通过
    _validate_update_config(_cfg(web_port=8888, storage_path=target))
