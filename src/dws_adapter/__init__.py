"""钉钉 CLI 适配器（DingTalk Workspace CLI / ``dws``）——组合根。

原 dws_adapter.py 单文件拆分：引擎钩子/聊天/媒体/文档联系人/认证组织/
OA 审批/听记/知识库各自独立为 mixin 子模块，此处多继承重建 DwsAdapter。
对外导出保持完全兼容：``from src.dws_adapter import DwsAdapter, DwsError, ...`` 无需改动。
"""
from __future__ import annotations

from src.im_adapter.base_adapter import BaseIMAdapter
from src.dws_adapter.base import DwsAdapterBaseMixin
from src.dws_adapter.chat import DwsAdapterChatMixin
from src.dws_adapter.media import DwsAdapterMediaMixin
from src.dws_adapter.doc_contact import DwsAdapterDocMixin
from src.dws_adapter.auth_org import DwsAdapterAuthOrgMixin
from src.dws_adapter.oa_approval import DwsAdapterOaApprovalMixin
from src.dws_adapter.minutes import DwsAdapterMinutesMixin
from src.dws_adapter.wiki import DwsAdapterWikiMixin
from src.dws_adapter.core import (
    DwsError, DwsRetryableError, DwsNonRetryableError, DwsPermissionError,
    is_permission_error, is_org_config_problem, classify_dws_error,
    _NO_BROWSER_ENV,
)
# 兼容重导出：原 dws_adapter.py 曾直接暴露（供 chat 发送链路使用）
from src.im_adapter.markdown_fix import normalize_markdown_for_platform
from src.im_adapter.message_format import classify_message_format


class DwsAdapter(DwsAdapterBaseMixin, DwsAdapterChatMixin,
                 DwsAdapterMediaMixin, DwsAdapterDocMixin, DwsAdapterAuthOrgMixin,
                 DwsAdapterOaApprovalMixin, DwsAdapterMinutesMixin,
                 DwsAdapterWikiMixin, BaseIMAdapter):
    # 钉钉 markdown 子集不支持 GFM 表格：发送前需转换（见 chat_message_send/update）
    supports_markdown_tables = False
