"""平台白名单单一真源一致性测试（T-B3）。

平台白名单在仓内曾有三份副本：security.py(死代码)、web/routers/sync.py(活)、
config_models.py 的 Literal(活)。现已收敛为 src.constants.SUPPORTED_PLATFORMS 单一真源，
并删除死代码。本测试守护「加平台时三处必须同步」这一不变量。
"""
from __future__ import annotations

from typing import get_args

from src.config_models import PlatformConfig
from src.constants import SUPPORTED_PLATFORMS


def test_supported_platforms_is_frozen_source_of_truth():
    assert isinstance(SUPPORTED_PLATFORMS, frozenset)
    assert SUPPORTED_PLATFORMS == frozenset({"dingtalk", "feishu", "wecom"})


def test_config_literal_matches_supported_platforms():
    # src.config_models.PlatformConfig.adapter_type 用
    # Literal["dingtalk", "feishu", "wecom"] 表达平台类型，必须与
    # src.constants.SUPPORTED_PLATFORMS 完全一致，防「加平台漏改一处」漂移。
    field = PlatformConfig.model_fields["adapter_type"]
    literal_args = set(get_args(field.annotation))
    assert literal_args == SUPPORTED_PLATFORMS


def test_sync_router_uses_shared_whitelist():
    # web 层同步路由不再另持 _KNOWN_PLATFORMS 副本，直接引用单一真源。
    import web.routers.sync as sync_router

    assert not hasattr(sync_router, "_KNOWN_PLATFORMS")
    assert sync_router.SUPPORTED_PLATFORMS is SUPPORTED_PLATFORMS
