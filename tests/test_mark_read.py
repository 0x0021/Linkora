"""标记已读（mark_read）能力测试。

覆盖：
- DwsAdapter.mark_read 正确拼装 `chat mark-read --conversation-id X --message-id Y`
- poller 成功处理消息后调用 mark_read；处理失败时【不】调用（避免误标失败消息）
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dws_adapter import DwsAdapter  # noqa: E402
from src.models import Message  # noqa: E402
from src.poller import MessagePoller  # noqa: E402


def _make_poller(tmp_db, dws):
    from src.config import PollerConfig
    cfg = PollerConfig()
    store = MagicMock()
    store._message_repo.is_message_processed.return_value = False
    poller = MessagePoller(
        config=cfg, dws=dws, store=store,
        current_user_id="u1", current_user_name="测试",
    )
    return poller, store


def test_adapter_mark_read_command():
    """适配器正确拼装 mark-read 命令。"""
    dws = DwsAdapter(cli_path="dws")
    dws.run = MagicMock(return_value={"ok": True})
    dws.mark_read("conv-123", "msg-456")
    called_args = dws.run.call_args[0][0]
    assert called_args[:2] == ["chat", "mark-read"]
    assert "--conversation-id" in called_args
    assert "conv-123" in called_args
    assert "--message-id" in called_args
    assert "msg-456" in called_args


def test_adapter_mark_read_missing_arg():
    """缺参数时抛 ValueError。"""
    dws = DwsAdapter(cli_path="dws")
    with pytest.raises(ValueError):
        dws.mark_read("", "msg-1")
    with pytest.raises(ValueError):
        dws.mark_read("conv-1", "")


def test_poller_saves_message_on_success(tmp_db_path):
    """handler 成功 → 保存消息到 store + 标记已处理。

    mark_read 已移至 main._send_reply 中执行，poller 不再直接调用。
    """
    dws = MagicMock()
    poller, store = _make_poller(tmp_db_path, dws)

    msg = Message(
        msg_id="m1", chat_id="conv-1", chat_type="group",
        chat_name="群", msg_type="text",
        sender_id="peer", sender_name="对方", content="你好",
        timestamp=__import__("datetime").datetime.now(), raw={},
    )
    poller.poll_once = MagicMock(return_value=[msg])

    calls = {"n": 0}
    def handler(m):
        calls["n"] += 1
        poller._running = False

    poller.run_loop(handler)

    assert calls["n"] == 1
    # 消息已落库
    store._message_repo.save_message.assert_called()


def test_poller_no_mark_read_on_failure(tmp_db_path):
    """handler 抛错 → 不调用 dws（避免误标失败消息），但仍落库。"""
    dws = MagicMock()
    poller, store = _make_poller(tmp_db_path, dws)

    msg = Message(
        msg_id="m2", chat_id="conv-2", chat_type="single",
        chat_name="单聊", msg_type="text",
        sender_id="peer", sender_name="对方", content="hi",
        timestamp=__import__("datetime").datetime.now(), raw={},
    )
    poller.poll_once = MagicMock(return_value=[msg])

    def handler(m):
        poller._running = False
        raise RuntimeError("处理失败")
    poller.run_loop(handler)

    dws.assert_not_called()
    # 失败也仍去重标记 + 落库
    store._message_repo.save_message.assert_called()


def test_poller_mark_read_respects_config_off(tmp_db_path):
    """mark_read_after_process=false 时 poller 仍正常保存消息。"""
    dws = MagicMock()
    poller, store = _make_poller(tmp_db_path, dws)
    poller.config.mark_read_after_process = False

    msg = Message(
        msg_id="m3", chat_id="conv-3", chat_type="group",
        chat_name="群", msg_type="text",
        sender_id="peer", sender_name="对方", content="yo",
        timestamp=__import__("datetime").datetime.now(), raw={},
    )
    poller.poll_once = MagicMock(return_value=[msg])

    def handler(m):
        poller._running = False

    poller.run_loop(handler)

    store._message_repo.save_message.assert_called()
