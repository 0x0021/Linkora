"""IM 适配器统一抽象基类。

本模块把「能力接口定义 + 共享默认实现 + 模板方法」整合为一个统一基类，
作为飞书 / 企业微信 / 钉钉三个平台适配器的共同祖先。

继承链条：:

    base.BaseIMAdapter           ← CLI 执行引擎（拼命令 / subprocess / 重试 / 错误分类）
      └── base_adapter.BaseIMAdapter  ← 统一能力接口 + 共享实现（本模块）
            ├── FeishuCliAdapter
            ├── WecomCliAdapter
            └── DwsAdapter

本类定义 23 个能力方法的统一接口（迁移自 ``capabilities.IMCapabilitySkeleton``），
并提供三平台共用的默认实现与工具方法：
- ``use_org`` / ``mark_read`` / ``chat_conversation_info`` 等单租户默认实现
- ``_normalize_chat`` 模板方法（字段提取由子类钩子提供）
- ``_normalize_timestamp`` / ``_items`` / ``_payload`` / ``_parse_time`` 通用工具
- ``chat_list_top_conversations`` 委托到 ``chat_message_list_unread_conversations``
"""
from __future__ import annotations

import abc
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from .base import BaseIMAdapter as _CLIEngine

logger = logging.getLogger(__name__)


class BaseIMAdapter(_CLIEngine, abc.ABC):
    """IM 平台适配器统一抽象基类。

    继承 ``base.BaseIMAdapter`` 的执行引擎，同时提供：
    - 23 个能力方法的统一抽象接口（需子类实现）
    - 单租户平台的共享默认实现
    - 模板方法模式的消息/会话归一化
    - 通用工具方法
    """

    # ==================================================================
    # 平台 markdown 能力标志
    # ==================================================================
    # 是否原生支持 GFM 表格渲染：飞书(Lark)支持=True；
    # 钉钉 / 企业微信的 markdown 子集不支持表格，置 False（发送前转换）。
    supports_markdown_tables: bool = True

    # ==================================================================
    # 通用工具（平台无关，可直接复用）
    # ==================================================================

    @staticmethod
    def _payload(resp: Any) -> Any:
        """取响应包里的 ``data`` 字段（兼容无 data 包裹的情况）。"""
        if isinstance(resp, dict):
            d = resp.get("data")
            if isinstance(d, dict):
                return d
        return resp

    def _items(self, resp: Any) -> list[dict]:
        """从列表响应里取条目数组（兼容 chats/items/messages 多种嵌套）。"""
        d = self._payload(resp)
        if isinstance(d, dict):
            for key in ("items", "chats", "messages", "data"):
                val = d.get(key)
                if isinstance(val, list):
                    return val
                # data.data 二级嵌套（个别命令）
                if isinstance(val, dict):
                    inner = val.get("items") or val.get("chats") or val.get("messages")
                    if isinstance(inner, list):
                        return inner
        if isinstance(d, list):
            return d
        return []

    @staticmethod
    def _parse_time(time_str: str, fallback: datetime) -> datetime:
        """尽力把 ``time_str`` 解析为 datetime；失败回退 ``fallback``。"""
        s = (time_str or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError as _exc:
                logger.debug(f"_parse_time: swallowed exception: {_exc}")
                continue
        try:
            return datetime.fromtimestamp(float(s))
        except (ValueError, OverflowError, OSError):
            return fallback

    def _time_window(self, time_str: str = "", default_days: int = 6) -> tuple[str, str]:
        """生成时间窗 ``(begin_time, end_time)`` 字符串。

        以当前时间作为 end，向前回溯 ``default_days`` 天作为 begin。
        ``time_str`` 指定时用 ``_parse_time`` 解析。
        """
        end = datetime.now()
        if time_str:
            begin = self._parse_time(time_str, end - timedelta(days=default_days))
        else:
            begin = end - timedelta(days=default_days)
        fmt = "%Y-%m-%d %H:%M:%S"
        return begin.strftime(fmt), end.strftime(fmt)

    @staticmethod
    def _normalize_timestamp(ct: int | float | str | None) -> str:
        """毫秒时间戳 → datetime 字符串（feishu/企微共用）。

        ``ct`` 可为毫秒 Unix 时间戳（int/float）、ISO 字符串或 None。
        返回 ``"%Y-%m-%d %H:%M:%S"`` 格式字符串，失败返回空串。
        """
        if ct is None:
            return ""
        if isinstance(ct, str):
            return ct
        if isinstance(ct, (int, float)):
            try:
                if ct > 1e12:
                    ct = ct / 1000.0
                dt = datetime.fromtimestamp(ct)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError) as _exc:
                logger.debug(f"_normalize_timestamp: swallowed exception: {_exc}")
                return str(ct)
        return ""

    # ==================================================================
    # 模板方法钩子（子类提供平台特有字段提取逻辑）
    # ==================================================================

    @abc.abstractmethod
    def _infer_single_chat(self, chat: dict) -> bool:
        """判定会话是否为单聊（平台特有逻辑）。

        - 飞书：基于 ``chat_mode`` 字段（``"p2p"`` → True）
        - 企微：基于 ``chat_type`` 字段（``1`` → True）
        - 钉钉：基于 ``singleChat`` 字段
        """
        ...

    # ==================================================================
    # 共享默认实现：_normalize_chat 模板方法
    # ==================================================================

    def _normalize_chat(self, chat: dict) -> dict:
        """将平台原生会话映射为 poller 兼容格式（模板方法）。

        poller 依赖两个 dingtalk 特有字段：
        - ``openConversationId``: 会话唯一标识
        - ``singleChat``: 是否为单聊

        子类无需覆写本方法；本方法会自动提取 ``chat_id``、``chat_name``，
        并调用 ``_infer_single_chat`` 判定单聊类型。
        """
        # 提取 chat_id：各平台常用字段依次尝试
        cid = ""
        for field in ("chat_id", "chatid", "id", "openConversationId", "chatId"):
            val = chat.get(field)
            if val:
                cid = str(val)
                break
        if not cid:
            return chat
        result = dict(chat)
        result.setdefault("openConversationId", cid)
        # 提取标题
        title = ""
        for field in ("name", "title", "chat_name", "chatName"):
            val = chat.get(field)
            if val:
                title = str(val)
                break
        result.setdefault("title", title or cid)
        # 单聊判定
        if "singleChat" not in result:
            result["singleChat"] = self._infer_single_chat(chat)
        return result

    # ==================================================================
    # 共享默认实现：单租户 / 能力不可用时的方法
    # ==================================================================

    def use_org(self, corp_id: str) -> bool:
        """切换到指定组织。单租户平台（飞书/企微）无操作，返回 True。

        钉钉（DwsAdapter）需覆写：调用 ``dws profile use <corp_id>``。
        """
        return True

    def mark_read(self, conversation_id: str, message_id: str) -> dict:
        """标记已读。默认空操作（企微无此能力）。

        飞书/钉钉需覆写调用平台 CLI 对应命令。
        """
        return {}

    def chat_conversation_info(self, chat_id: str) -> dict:
        """获取会话详情。默认返回空 dict（企微无此能力）。"""
        return {}

    def chat_list_top_conversations(self, limit: int = 50) -> list[dict]:
        """获取最近会话列表。默认委托到 ``chat_message_list_unread_conversations``（企微模式）。"""
        return self.chat_message_list_unread_conversations(count=limit)

    # ==================================================================
    # 认证 / 组织（抽象方法，需子类实现）
    # ==================================================================

    @abc.abstractmethod
    def auth_status(self) -> dict:
        """检查认证状态。返回 ``{"authenticated": bool, ...}``。"""
        ...

    @abc.abstractmethod
    def auth_login(self, device_flow: bool = False, no_browser: bool = True) -> dict:
        """触发登录流程。"""
        ...

    @abc.abstractmethod
    def profile_list(self) -> dict:
        """列出已登录 profile。"""
        ...

    @abc.abstractmethod
    def get_current_org(self) -> dict:
        """返回当前组织 ``{"corp_id": str, "corp_name": str}``。"""
        ...

    @abc.abstractmethod
    def list_orgs(self) -> list[dict]:
        """列出已登录组织。"""
        ...

    @abc.abstractmethod
    def is_authenticated(self) -> bool | str:
        """判定登录态。返回 ``True / False / "org_not_configured"``。"""
        ...

    # ==================================================================
    # 联系人（抽象方法，需子类实现）
    # ==================================================================

    @abc.abstractmethod
    def contact_user_get_self(self) -> dict:
        """获取当前用户自身信息。"""
        ...

    @abc.abstractmethod
    def contact_user_search(self, keyword: str) -> list[dict]:
        """按关键字搜索联系人。"""
        ...

    # ==================================================================
    # 会话 / 消息拉取（抽象方法，需子类实现）
    # ==================================================================

    @abc.abstractmethod
    def chat_message_list_unread_conversations(self, count: int = 20) -> list[dict]:
        """获取未读/最近会话列表。"""
        ...

    @abc.abstractmethod
    def chat_message_list_direct(self, user_id: str = "",
                                  open_dingtalk_id: str = "",
                                  time_str: str = "",
                                  limit: int = 50) -> list[dict]:
        """拉取单聊消息。"""
        ...

    @abc.abstractmethod
    def chat_message_list(self, group: str, time_str: str,
                          limit: int = 50,
                          cached_result: dict | None = None) -> list[dict]:
        """拉取群聊消息。``cached_result`` 为可选预取合并字典（仅 DwsAdapter 优化用，
        其它适配器忽略）。加入该参数以保持跨适配器接口一致，避免 poller 无差别传参时
        非 dws 适配器报 TypeError。"""
        ...

    @abc.abstractmethod
    def chat_message_list_all(self, start: str, end: str,
                              limit: int = 50,
                              max_pages: int | None = None) -> dict:
        """按时间范围拉取所有消息（自动分页聚合）。

        Args:
            max_pages: 分页上限（dws 适配器使用；其他适配器可忽略）。
        """
        ...

    # ==================================================================
    # 发送 / 媒体（抽象方法，需子类实现）
    # ==================================================================

    @abc.abstractmethod
    def chat_message_send(self, *, group: str | None = None,
                          user: str | None = None,
                          open_dingtalk_id: str | None = None,
                          title: str = "", text: str = "",
                          uuid: str | None = None,
                          ai_tag: bool | None = None,
                          msg_type: str | None = None,
                          media_id: str | None = None,
                          file_path: str | None = None,
                          at_all: bool = False,
                          at_open_dingtalk_ids: str | None = None) -> dict:
        """发送消息。"""
        ...

    @abc.abstractmethod
    def chat_message_reply(self, *, message_id: str | None = None, text: str = "",
                           title: str = "", uuid: str | None = None,
                           reply_in_thread: bool = False,
                           group: str | None = None,
                           user: str | None = None,
                           open_dingtalk_id: str | None = None,
                           msg_type: str | None = None,
                           media_id: str | None = None,
                           file_path: str | None = None) -> dict:
        """回复指定消息。"""
        ...

    @abc.abstractmethod
    def chat_message_update(self, *, message_id: str, text: str = "",
                           title: str = "", group: str | None = None,
                           user: str | None = None) -> dict:
        """更新已发送的消息内容。"""
        ...

    def chat_message_recall(self, *, message_id: str,
                            group: str | None = None,
                            user: str | None = None) -> bool:
        """撤回/删除已发送的消息。默认不支持，返回 False。

        子类（飞书/企微/钉钉）若 CLI 提供对应能力应覆写本方法。
        实现必须吞掉一切异常并返回 bool，绝不可向上抛，否则会
        打断流式回复失败时的降级逻辑。
        """
        logging.getLogger(__name__).debug(
            "[IM] 当前适配器不支持撤回消息: %s", type(self).__name__)
        return False

    @abc.abstractmethod
    def media_upload(self, file_path: str, media_type: str = "image") -> str:
        """上传本地媒体文件，返回 media_id。"""
        ...

    @abc.abstractmethod
    def download_media(self, *, media_id: str, message_id: str,
                       conversation_id: str, output_path: str) -> str:
        """下载媒体文件到本地。"""
        ...

    # ==================================================================
    # 文档 / 日历 / 待办（抽象方法，需子类实现）
    # ==================================================================

    @abc.abstractmethod
    def doc_search(self, query: str, page_size: int = 10) -> list[dict]:
        """搜索知识库/文档。"""
        ...

    @abc.abstractmethod
    def doc_read(self, node_id: str, content_format: str = "markdown") -> dict:
        """读取文档内容。"""
        ...

    def calendar_event_list(self, start: str = "", end: str = "") -> list[dict]:
        """列出日历事件。默认返回空列表（企微不支持）。"""
        return []

    def todo_task_create(self, title: str, executors: str,
                         due: str = "", priority: str = "") -> dict:
        """创建待办任务。默认抛 NotImplementedError（仅钉钉支持）。"""
        raise NotImplementedError(
            f"todo_task_create not implemented for {self.__class__.__name__}")

    def oa_approval_redirect_task(self, *, task_id: str, to_actioner_id: str,
                                  remark: str = "") -> dict:
        """转交 OA 审批任务给其他人。默认抛 NotImplementedError（当前仅钉钉支持）。

        飞书/企微若 CLI 提供对应能力应覆写本方法；上层
        DingTalkApprovalProvider 等 Provider 会捕获异常并转成失败回执。
        """
        raise NotImplementedError(
            f"oa_approval_redirect_task not implemented for {self.__class__.__name__}")
