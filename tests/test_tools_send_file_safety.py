from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.tools.media import is_allowed_local_path
from src.tools.chat import SendMessageTool


def test_is_allowed_local_path_rejects_system_files():
    # 系统机密文件绝不允许作为可发送路径
    assert is_allowed_local_path("/etc/passwd") is False
    assert is_allowed_local_path("/Users/user/.ssh/id_rsa") is False
    # 目录穿越也应被拒
    assert is_allowed_local_path("/tmp/../etc/passwd") is False


def test_is_allowed_local_path_allows_data_and_tmp(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_text("x")  # 需是真实文件（isfile 在调用方校验，这里只需路径合法）
    # tmp_path 落在 _ALLOWED_ROOTS（系统临时目录）内
    assert is_allowed_local_path(str(f)) is True
    # data/ 目录
    data_file = Path("data/recv_files/test.pdf")
    data_file.parent.mkdir(parents=True, exist_ok=True)
    if data_file.exists():
        data_file.unlink()
    assert is_allowed_local_path(str(data_file)) is True


def test_send_message_rejects_disallowed_file_path():
    tool = SendMessageTool(dws=MagicMock(), store=None, self_user_id="")
    res = tool.execute({
        "chat_id": "peer123",
        "chat_type": "single",
        "msg_type": "file",
        "file_path": "/etc/passwd",
    })
    assert isinstance(res, dict) and res.get("error"), res
    assert "安全限制" in res["error"]
    # 未触达底层发送
    tool.dws.chat_message_send.assert_not_called()


def test_send_message_rejects_disallowed_image_path():
    tool = SendMessageTool(dws=MagicMock(), store=None, self_user_id="")
    res = tool.execute({
        "chat_id": "peer123",
        "chat_type": "single",
        "msg_type": "image",
        "file_path": "/etc/passwd",
    })
    assert isinstance(res, dict) and res.get("error"), res
    assert "安全限制" in res["error"]
    tool.dws.chat_message_send.assert_not_called()


def test_send_message_allows_local_file(tmp_path):
    local = tmp_path / "合同.pdf"
    local.write_bytes(b"%PDF-1.4 fake")
    tool = SendMessageTool(dws=MagicMock(), store=None, self_user_id="")
    res = tool.execute({
        "chat_id": "peer123",
        "chat_type": "single",
        "msg_type": "file",
        "file_path": str(local),
        "text": "请看附件",
    })
    assert res.get("success") is True, res
    tool.dws.chat_message_send.assert_called_once()
