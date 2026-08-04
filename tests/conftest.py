"""Pytest 共享 fixtures 和配置。

提供项目级测试辅助：
- 临时数据库路径
- Mock DWS Adapter
- 测试用 Message 工厂函数
- 规则引擎 fixture
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.models import Message


# ============ 临时目录与数据库 ============

@pytest.fixture
def tmp_db_path():
    """提供临时 SQLite 数据库路径，测试后自动清理。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test-linkora.db"


# ============ Message 工厂 ============

def make_message(
    msg_id: str = "test-msg-001",
    chat_id: str = "test-chat-001",
    chat_type: str = "single",
    chat_name: str | None = "测试用户",
    sender_id: str = "sender-001",
    sender_name: str = "张三",
    content: str = "你好",
    msg_type: str = "text",
    timestamp: datetime | None = None,
    role: str = "user",
    raw: dict | None = None,
) -> Message:
    """快速构造测试用 Message 对象。"""
    return Message(
        msg_id=msg_id,
        chat_id=chat_id,
        chat_type=chat_type,
        chat_name=chat_name,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        msg_type=msg_type,
        timestamp=timestamp or datetime(2026, 7, 7, 12, 0, 0),
        raw=raw if raw is not None else {},
        role=role,
    )


@pytest.fixture
def msg_single_chat():
    """单聊消息 fixture。"""
    return make_message(chat_type="single", sender_name="张三")


@pytest.fixture
def msg_group_chat():
    """群聊消息 fixture。"""
    return make_message(chat_type="group", chat_name="技术交流群", sender_name="李四")


@pytest.fixture
def msg_empty_content():
    """空内容消息 fixture。"""
    return make_message(content="")


# ============ Mock DWS Adapter ============

@pytest.fixture
def mock_dws():
    """Mock DWS Adapter，避免真实 CLI 调用。"""
    dws = MagicMock()
    dws.dry_run = True
    dws.cli_path = "dws"
    dws.timeout = 30
    dws.retries = 2
    dws.profile = ""
    
    # 默认返回空结果
    dws.run.return_value = {}
    dws.contact_user_get_self.return_value = {
        "orgEmployeeModel": {
            "userId": "test-user-001",
            "orgUserName": "测试用户",
            "orgName": "测试公司",
            "depts": [{"deptName": "技术部"}],
        }
    }
    dws.chat_message_list_unread_conversations.return_value = []
    dws.chat_message_list_direct.return_value = []
    dws.chat_message_list.return_value = []
    dws.auth_status.return_value = {"authenticated": True}
    
    return dws


# ============ 规则引擎 Fixture ============

@pytest.fixture
def rule_engine_config():
    """提供基础规则引擎配置字典。"""
    return {
        "enabled": True,
        "blacklist": {
            "users": [],
            "groups": [],
        },
        "whitelist": {
            "enabled": False,
            "users": [],
            "groups": [],
        },
        "keywords": [],
        "stop_words": [
            "的,是,在,我,你,他,她,它,们",
            "好的,嗯,哦,啊,了,吧,呢,哈",
            "谢谢,感谢,请问,麻烦,帮我,帮忙,您好",
        ],
        "intent_filter": {
            "enabled": True,
            "business_keywords": ["问题", "故障", "无法", "报错", "异常", "帮助", "怎么", "如何"],
            "thank_you": ["谢谢", "感谢", "多谢", "thanks", "thank you"],
            "acknowledge": ["收到", "好的", "ok", "明白", "了解", "知道了"],
            "closing": ["再见", "拜拜", "bye", "先这样", "回头聊"],
            "polite": ["你好", "您好", "早", "早安", "下午好", "晚上好"],
            "pure_thank_max_length": 20,
        },
    }


@pytest.fixture
def rule_engine(rule_engine_config, tmp_db_path):
    """提供规则引擎实例（无数据库依赖）。"""
    from src.config import RulesConfig
    
    config = RulesConfig(**rule_engine_config)
    
    from src.rule_engine import RuleEngine
    engine = RuleEngine(config=config, db_store=None)
    return engine
