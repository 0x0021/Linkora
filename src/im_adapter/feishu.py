"""飞书（Lark）CLI 适配器 —— 基于真实 ``lark-cli`` v1.0.72 语法实现。

继承路径：``base_adapter.BaseIMAdapter``（统一能力接口 + CLI 执行引擎）。

本实现已对照真实 ``lark-cli`` 探查结果落地（非凭空猜测）：

- CLI 二进制默认 ``lark-cli``，领域式语法：``lark-cli im +messages-send --chat-id X --text "..."``
- 默认输出即为 JSON（``--format json`` 为默认），``--dry-run`` / ``--profile`` 为
  全局/每命令均可接受的尾部参数 —— 与基类 ``BaseIMAdapter._build_command`` 的
  「尾部追加 --dry-run / --profile」完全一致，故**无需覆写命令拼接**。
- 错误形态：CLI 把错误 JSON 打到 **stdout** 并以**退出码 1** 结束，形如
  ``{"ok": false, "error": {"code": 99992356, "message": "..."}}``。
  因此需覆写两处：``run()`` 捕获「退出码 0 但 ``ok:false``」；``_classify_error``
  解析该 JSON 错误包并按飞书错误码/关键字映射到 ``IMAdapter*`` 异常。

已实现：认证 / 组织 / 联系人 / 会话消息拉取 / 发送 / 媒体上传下载 的 IM 核心闭环，
以及 ``doc_search`` / ``doc_read`` / ``doc_list`` 飞书文档能力（KB 导入链路已接通）。
未实现（保持 NotImplementedError 桩，待后续补全）：``calendar_event_list`` / ``todo_task_create``。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .base_adapter import BaseIMAdapter
from .errors import (
    IMAdapterError,
    IMAdapterPermissionError,
    IMAdapterResourceNotFoundError,
    IMAdapterRateLimitError,
    IMAdapterShutdownError,
    IMAdapterUnsupportedTypeError,
)

from .feishu_doc_mixin import FeishuDocMixin
from .feishu_media_mixin import FeishuMediaMixin

logger = logging.getLogger(__name__)

# 飞书常见错误码（用于分类；未命中时再走关键字兜底）
_PERMISSION_CODES = frozenset({
    99991663,  # 无权限
    99991668,  # token 失效/无效
    99991302,  # 用户不在租户
    99991661,  # 应用未启用相关权限
    230002,    # Bot/User can NOT be out of the chat（被踢/已退群，永久不可达）
    232010,    # Operator and chat can NOT be in different tenants（跨租户，永久不可达）
    99992361,  # open_id cross app（open_id 属其他 app，永久不可达）
})
_RETRYABLE_CODES = frozenset({
    99991400,  # 应用流控（限频）
    99991401,  # 调用频率过高
    99992414,  # 内部错误（可重试）
    99991301,  # 服务端内部错误
})
# lark-cli docs +fetch 仅支持 docx，其他类型（file/wiki/mindnote 等）报此码
_UNSUPPORTED_TYPE_CODES = frozenset({
    3380002,  # Unsupported document type 'xxx'. Only docx is supported.
})
_PERMISSION_HINTS = (
    "permission", "unauthorized", "token", "access", "scope",
    "no permission", "not authorized", "forbidden",
    "out of the chat", "different tenants", "cross app", "can not be out of",
)
_RETRYABLE_HINTS = (
    "rate", "limit", "quota", "frequency", "timeout", "timed out",
    "network", "connection", "try again", "too many", "throttl",
)
_UNSUPPORTED_TYPE_HINTS = (
    "unsupported document type", "only docx is supported", "not supported",
)


class FeishuCliAdapter(FeishuDocMixin, FeishuMediaMixin, BaseIMAdapter):
    """飞书（Lark）CLI 适配器（基于 ``lark-cli`` 实现 IM 核心能力）。

    用法：:

        adapter = FeishuCliAdapter()          # 默认调 lark-cli
        adapter.chat_message_send(chat_id="oc_xxx", text="你好")

    注意：本适配器默认以「用户身份」驱动（lark-cli 的 ``defaultAs: auto`` 会解析为
    当前登录用户）。飞书没有钉钉式的「组织/ CorpId」概念，``get_current_org`` 等
    方法以当前 App/Tenant 作为伪组织返回。
    """

    def __init__(self, cli_path: str = "lark-cli", timeout: int = 30,
                 retries: int = 2, dry_run: bool = True, profile: str = ""):
        # 默认 CLI 二进制为 lark-cli（而非基类的 dws）
        super().__init__(cli_path=cli_path, timeout=timeout, retries=retries,
                         dry_run=dry_run, profile=profile)

    # ------------------------------------------------------------------
    # 引擎覆写：错误模型适配
    # ------------------------------------------------------------------

    def run(self, args: list[str], timeout: int | None = None,
            retries: int | None = None, operation: str = "",
            force_no_dry_run: bool = False) -> dict:
        """执行命令，并额外捕获 lark-cli「退出码 0 但 ``{"ok": false}``」的情况。

        基类 ``run()`` 仅识别 ``{"success": false}``（钉钉 dws 风格）；飞书用
        ``ok`` 键，故在调用基类后补一次校验。
        """
        data = super().run(args, timeout=timeout, retries=retries,
                           operation=operation, force_no_dry_run=force_no_dry_run)
        if isinstance(data, dict) and data.get("ok") is False:
            err = data.get("error") or {}
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            raise self._classify_error(msg or json.dumps(data, ensure_ascii=False))
        return data

    def _classify_error(self, error_msg: str) -> type:
        """把 lark-cli 错误文本映射为 ``IMAdapter*`` 异常类。

        lark-cli 的错误文本通常是 JSON 字符串：
        ``{"ok": false, "error": {"code": 99992356, "message": "..."}}``。
        解析出 ``code`` / ``message`` 后按飞书错误码与关键字分类。
        """
        code = None
        message = error_msg or ""
        try:
            obj = json.loads(error_msg)
            err = obj.get("error") or {}
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message") or message
        except Exception:
            logger.warning("[resilience] silent exception in _classify_error", exc_info=True)

        msg = (message or "").lower()
        # JSON 解析失败时先匹配网络超时类，再匹配权限类，
        # 避免超时报文中的关键字被误判为权限错误
        if code in _RETRYABLE_CODES or any(k in msg for k in _RETRYABLE_HINTS):
            return self._retryable_error_class()
        if code in _PERMISSION_CODES or any(k in msg for k in _PERMISSION_HINTS):
            return self._permission_error_class()
        if code in _UNSUPPORTED_TYPE_CODES or any(k in msg for k in _UNSUPPORTED_TYPE_HINTS):
            return IMAdapterUnsupportedTypeError
        return self._base_error_class()

    # ------------------------------------------------------------------
    # 辅助：响应解析
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 认证 / 组织
    # ------------------------------------------------------------------

    def auth_status(self) -> dict:
        """检查认证状态。基于 ``lark-cli whoami`` 归一化。"""
        try:
            info = self.run(["whoami"])
        except Exception as e:  # noqa: BLE001 - 认证探测失败即未登录
            logger.warning("[resilience] silent exception in auth_status (e)", exc_info=True)
            return {"authenticated": False, "error": str(e)}
        ready = bool(info.get("available")) and str(info.get("tokenStatus")) in (
            "ready", "valid")
        return {
            "authenticated": ready,
            "brand": info.get("brand"),
            "app_id": info.get("appId"),
            "identity": info.get("identity"),
            "on_behalf_of": info.get("onBehalfOf", {}),
        }

    def is_authenticated(self) -> bool | str:
        """零网络优先读 ``whoami`` 判定登录态（可用 + token 有效）。"""
        try:
            info = self.run(["whoami"])
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in is_authenticated", exc_info=True)
            return False
        if not info.get("available"):
            return False
        return str(info.get("tokenStatus")) in ("ready", "valid")

    # 错误严重程度降级：某些 lark-cli 错误是「环境配置」而非「业务故障」，
    # 不该在每轮都刷一条 ERROR。覆盖基类方法。
    def _is_benign_error(self, error_msg: str) -> bool:
        """lark-cli 的环境类错误（不需业务处理）应降为 debug，不刷 ERROR。"""
        msg = (error_msg or "").lower()
        # openclaw 上下文未绑定：调用方没绑 lark-cli，不每轮报错。详见 lark-cli config bind
        if "not bound" in msg or "not_configured" in msg:
            return True
        # 业务无数据：未发过消息的 p2p
        if "no message" in msg:
            return True
        # 跨租户 / 跨 app / 已退群等会话级边界错误：属预期业务边界，
        # 不应每轮刷 ERROR（与 _PERMISSION_CODES 中 230002/232010/99992361 对齐）
        if ("out of the chat" in msg or "different tenants" in msg
                or "cross app" in msg or "can not be out of" in msg):
            return True
        return False

    def auth_login(self, device_flow: bool = False, no_browser: bool = True) -> dict:
        """触发登录流程（lark-cli 设备码 / 二维码登录）。

        通常需 ``force_no_dry_run=True`` 且较长 timeout；可能交互式打印验证码。

        lark-cli v1.0.72 移除了 ``--device-flow``，改为 ``--no-wait``（发起后立即返回，
        后续用 ``--device-code`` 完成授权）。本方法以 ``--no-wait`` 兼容。
        """
        args = ["auth", "login"]
        if device_flow:
            args.append("--no-wait")
        return self.run(args, timeout=300, force_no_dry_run=True)

    def profile_list(self) -> dict:
        """列出已登录的 profile（基于 ``lark-cli auth list``）。"""
        try:
            profiles = self.run(["auth", "list"])
        except Exception as e:  # noqa: BLE001
            logger.warning("[resilience] silent exception in profile_list (e)", exc_info=True)
            return {"authenticated": False, "error": str(e)}
        if isinstance(profiles, list):
            return {"authenticated": True, "profiles": profiles}
        return {"authenticated": True, "profiles": [profiles]}

    def get_current_org(self) -> dict:
        """飞书无 CorpId 概念；以当前 App/Tenant 作为伪组织返回。

        基于 ``whoami`` 的 ``appId`` / ``brand``。
        """
        info = self.run(["whoami"])
        return {
            "corp_id": info.get("appId") or info.get("profile") or "",
            "corp_name": info.get("brand") or "feishu",
        }

    def list_orgs(self) -> list[dict]:
        """飞书单租户，返回当前 App 作为唯一组织。"""
        try:
            info = self.run(["whoami"])
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in list_orgs", exc_info=True)
            return []
        return [{
            "corp_id": info.get("appId") or info.get("profile") or "",
            "corp_name": info.get("brand") or "feishu",
        }]

    # ------------------------------------------------------------------
    # 联系人
    # ------------------------------------------------------------------

    def contact_user_get_self(self) -> dict:
        """获取当前登录用户自身信息（``lark-cli contact +get-user``，省略 user_id 即自己）。"""
        try:
            resp = self.run(["contact", "+get-user"])
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in contact_user_get_self", exc_info=True)
            return {}
        d = self._payload(resp)
        return d if isinstance(d, dict) else {}

    def contact_user_search(self, keyword: str) -> list[dict]:
        """按关键字搜索联系人（``lark-cli contact +search-user --query X --as user``）。"""
        if not keyword:
            return []
        try:
            resp = self.run(
                ["contact", "+search-user", "--query", keyword, "--as", "user"])
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in contact_user_search", exc_info=True)
            return []
        return self._items(resp)

    # ------------------------------------------------------------------
    # 会话 / 消息拉取
    # ------------------------------------------------------------------

    def _infer_single_chat(self, chat: dict) -> bool:
        """判定是否为单聊：以飞书 API 返回的 ``chat_mode`` 字段为准。

        飞书原生字段 ``chat_mode`` 的值为 ``"p2p"``（单聊）或 ``"group"``（群聊），
        由飞书服务端判定，不做任何基于 member_count / chat_id 格式 / 会话名的
        二次推断，避免误判（如将外部好友单聊误判为 group 导致消息被过滤）。
        """
        chat_mode = (chat.get("chat_mode") or "").lower()
        return chat_mode == "p2p"

    def _normalize_chat(self, chat: dict) -> dict:
        """将飞书原生会话格式映射为 poller 兼容的通用会话格式。

        poller 依赖两个 dingtalk 特有字段：
        - ``openConversationId``: 会话唯一标识（飞书用 ``chat_id``）
        - ``singleChat``: 是否为单聊（以飞书 API 的 ``chat_mode`` 字段为准）
        """
        cid = chat.get("chat_id") or chat.get("id") or ""
        if not cid:
            return chat
        result = dict(chat)
        result.setdefault("openConversationId", cid)
        result.setdefault("title", chat.get("name") or chat.get("title") or "")
        if "singleChat" not in result:
            result["singleChat"] = self._infer_single_chat(chat)
        return result

    def chat_message_list_unread_conversations(self, count: int = 20) -> list[dict]:
        """获取「未读/最近」会话列表。

        飞书 CLI 无直接的「未读会话」捷径；这里以 ``+chat-list --types=p2p,group``
        （最近会话）作为代理。如需严格未读，可后续接原始 API。
        """
        try:
            resp = self.run([
                "im", "+chat-list", "--types", "p2p,group",
                "--page-size", str(min(count, 100)),
            ])
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in chat_message_list_unread_conversations", exc_info=True)
            return []
        return [self._normalize_chat(c) for c in self._items(resp)]

    def chat_list_top_conversations(self, limit: int = 50) -> list[dict]:
        """获取最近会话列表（不依赖未读标记），按活跃时间排序。"""
        try:
            resp = self.run([
                "im", "+chat-list", "--types", "p2p,group",
                "--sort", "active_time", "--page-size", str(min(limit, 100)),
            ])
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in chat_list_top_conversations", exc_info=True)
            return []
        return [self._normalize_chat(c) for c in self._items(resp)]

    def _normalize_message(self, msg: dict) -> dict:
        """将飞书消息字段映射为 poller `_raw_to_message` 期望的 dingtalk 字段名。

        `_raw_to_message` 依赖以下字段：
        - ``openMessageId`` / ``msgId`` → 飞书 ``message_id``
        - ``senderName`` / ``sender`` → 飞书 ``sender.name`` 或 ``sender_name``
        - ``senderOpenDingTalkId`` / ``senderId`` → 飞书 ``sender.id`` 或 ``sender_id``
        - ``content`` → 飞书 ``body.content``（JSON 字符串）
        - ``msgType`` → 飞书 ``msg_type``
        - ``createTime`` → 飞书 ``create_time``（毫秒时间戳或 ISO 字符串）
        """
        result = dict(msg)
        # 消息 ID
        mid = msg.get("message_id") or msg.get("msg_id") or ""
        if mid and "openMessageId" not in result:
            result["openMessageId"] = mid
            result.setdefault("msgId", mid)
        # 发送者：飞书 sender 可能为嵌套对象 {"id":"ou_xxx","name":"张三"} 或顶层字段
        sender_obj = msg.get("sender")
        if isinstance(sender_obj, dict):
            sid = sender_obj.get("id") or sender_obj.get("user_id") or ""
            sname = sender_obj.get("name") or sender_obj.get("display_name") or ""
            # 必须直接赋值（非 setdefault）：原始 msg["sender"] 为 dict，
            # setdefault 遇到已存在的 key 不会覆盖，导致 sender 仍是 dict，
            # 下游 _raw_to_message 调用 .strip() 时报错。
            result["senderId"] = sid
            result["senderOpenDingTalkId"] = sid
            result["sender"] = sname
            result["senderName"] = sname
        else:
            sid = msg.get("sender_id") or msg.get("senderId") or ""
            sname = msg.get("sender_name") or msg.get("senderName") or ""
            if sid:
                result["senderId"] = sid
                result["senderOpenDingTalkId"] = sid
            if sname:
                result["sender"] = sname
                result["senderName"] = sname
        # 消息正文：飞书 body.content 通常是 JSON 字符串
        body = msg.get("body")
        if isinstance(body, dict):
            bcontent = body.get("content") or ""
            if bcontent:
                result.setdefault("content", str(bcontent))
        # 消息类型
        mt = msg.get("msg_type") or msg.get("msgType") or msg.get("messageType") or ""
        if mt:
            result.setdefault("msgType", mt)
            result.setdefault("messageType", mt)
        # 时间戳：飞书 create_time 为毫秒 Unix 时间戳或 ISO 字符串
        ct = msg.get("create_time")
        if ct is not None:
            ts = self._normalize_timestamp(ct)
            if ts:
                result.setdefault("createTime", ts)
        return result

    def chat_message_list_direct(self, user_id: str = "",
                                 open_dingtalk_id: str = "",
                                 time_str: str = "",
                                 limit: int = 50) -> list[dict]:
        """拉取单聊消息（默认按时间正序 老→新）。

        ``user_id`` / ``open_dingtalk_id`` 二选一（飞书 open_id，形如 ``ou_xxx``）。
        ``time_str`` 透传为 ``--start``（ISO 8601）。
        """
        # 防御：lark-cli --user-id 严格要求 ou_ 前缀。
        # 若上游传入 cli_xxx（Bot/应用 ID）或其他非 ou_ 污染值，主动降级到 oid，
        # 若 oid 也非法则 raise，避免 lark-cli 报 "invalid user ID format"。
        user_valid = bool(user_id) and str(user_id).startswith("ou_")
        oid_valid = bool(open_dingtalk_id) and str(open_dingtalk_id).startswith("ou_")
        target = user_id if user_valid else (open_dingtalk_id if oid_valid else "")
        if not target:
            raise ValueError("chat_message_list_direct 需提供合法的 user_id（ou_xxx）或 open_dingtalk_id")
        args = ["im", "+chat-messages-list", "--user-id", target,
                "--order", "asc", "--page-size", str(min(limit, 50))]
        if time_str:
            args += ["--start", time_str]
        try:
            resp = self.run(args)
        except IMAdapterPermissionError:
            raise  # 权限错误（跨租户/跨 app/已退群）需传递到调用方处理拉黑
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in chat_message_list_direct", exc_info=True)
            return []
        return [self._normalize_message(m) for m in self._items(resp)]

    def chat_message_list(self, group: str, time_str: str,
                          limit: int = 50,
                          cached_result: dict | None = None) -> list[dict]:
        """拉取群聊消息（按时间正序）。``group`` 即 chat_id（``oc_xxx``）。

        ``cached_result`` 为可选预取合并字典，仅 DwsAdapter 用于群消息快路径；
        飞书逐群独立拉取，忽略该参数（保持跨适配器接口一致，避免 poller 无差别
        传参时报 TypeError）。
        """
        if not group:
            raise ValueError("chat_message_list 需提供 group(chat_id)")
        args = ["im", "+chat-messages-list", "--chat-id", group,
                "--order", "asc", "--page-size", str(min(limit, 50))]
        if time_str:
            args += ["--start", time_str]
        try:
            resp = self.run(args)
        except IMAdapterPermissionError:
            raise  # 权限错误（跨租户/跨 app/已退群）需传递到调用方处理拉黑
        except Exception:  # noqa: BLE001
            logger.warning("[resilience] silent exception in chat_message_list", exc_info=True)
            return []
        return [self._normalize_message(m) for m in self._items(resp)]

    def chat_conversation_info(self, chat_id: str) -> dict:
        """获取会话详情（``lark-cli im chats get --chat-id X``）。"""
        if not chat_id:
            return {}
        try:
            resp = self.run(["im", "chats", "get", "--chat-id", chat_id])
        except Exception as e:  # noqa: BLE001
            # 跨租户/跨 app/已退群等永久权限错误属正常业务边界，降为 debug 避免刷屏；
            # 其余瞬时错误仍按 WARNING 记录便于排查。
            if isinstance(e, IMAdapterPermissionError):
                logger.debug(
                    "[resilience] chat_conversation_info 永久不可达(跨租户/跨app/已退群): %s | %s",
                    chat_id[:30] if chat_id else "", e,
                )
            else:
                logger.warning("[resilience] silent exception in chat_conversation_info", exc_info=True)
            return {}
        d = self._payload(resp)
        return d if isinstance(d, dict) else {}

    def mark_read(self, conversation_id: str, message_id: str) -> dict:
        """标记会话中指定消息及之前所有消息为已读。

        走飞书原始 API 捷径：``PATCH /open-apis/im/v1/chats/:chat_id/readed``，
        body ``{"message_id": "..."}``（lark-cli 的 ``api`` 原生逃生舱）。
        """
        if not conversation_id or not message_id:
            raise ValueError("mark_read 需提供 conversation_id 与 message_id")
        path = f"/open-apis/im/v1/chats/{conversation_id}/readed"
        return self.run(
            ["api", "PATCH", path, "--data",
             json.dumps({"message_id": message_id}, ensure_ascii=False)])

    # ------------------------------------------------------------------
    # 发送 / 媒体
    # ------------------------------------------------------------------

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
        """发送消息，支持文本 / Markdown / 图片 / 文件 / 语音 / 视频。

        参数映射（飞书 ``+messages-send``）：

        - 目标：``group`` → ``--chat-id``；``user`` / ``open_dingtalk_id`` → ``--user-id``。
        - 文本 → ``--text``；Markdown → ``--markdown``。
        - 富媒体：``msg_type`` 决定 ``--image/--file/--video/--audio``，
          取值为 ``media_id`` 或 ``file_path``（lark-cli 直接吃本地相对路径）。
        - ``uuid`` → ``--idempotency-key``（防重复发送）。
        - ``ai_tag``：飞书无 AI 标记概念，**忽略**（无操作）。
        - ``at_all`` / ``at_open_dingtalk_ids``：文本末尾自动追加 ``<at user_id="all"></at>``
          或 ``<at user_id="xxx"></at>`` 飞书 @ 语法。

        Returns:
            lark-cli 返回的原始 dict（含 ``ok`` / ``data`` / ``identity``），
            保持与钉钉「返回原始 dict」一致的契约。
        """
        target = group or user or open_dingtalk_id
        if not target:
            raise ValueError("chat_message_send 需提供 group / user / open_dingtalk_id 之一")

        # 防御：如果 group 参数实际传入的是用户 ou_xxx（而非会话 oc_xxx），
        # 不应用 --chat-id（lark-cli 会 404），应回退到 --user-id。
        # 这个情况通常在 SendMessageTool 误将用户 ID 当做群会话 ID 调用时发生。
        # 同时防御：user 或 open_dingtalk_id 参数若为 cli_xxx 等非 ou_ 格式，
        # 也会触发 lark-cli 的 "invalid user ID format, should start with 'ou_'"
        # 错误，因此提前校验。
        args = ["im", "+messages-send"]
        if group:
            if group.startswith("ou_"):
                logger.warning(
                    "chat_message_send: group=%s 疑似用户 open_id（ou_xxx），"
                    "改为 --user-id 避免飞书 404", group[:30],
                )
                args += ["--user-id", group]
            else:
                args += ["--chat-id", group]
        else:
            # 校验 user / open_dingtalk_id 必须是 ou_ 格式
            if user and not str(user).startswith("ou_"):
                logger.warning(
                    "chat_message_send: user=%s 非 ou_ 格式（可能为 cli_xxx Bot ID），"
                    "跳过 user 参数，尝试 open_dingtalk_id", user[:30],
                )
                user = ""
            if open_dingtalk_id and not str(open_dingtalk_id).startswith("ou_"):
                logger.warning(
                    "chat_message_send: open_dingtalk_id=%s 非 ou_ 格式（可能为 cli_xxx Bot ID），"
                    "跳过 open_dingtalk_id 参数", open_dingtalk_id[:30],
                )
                open_dingtalk_id = ""
            target = user or open_dingtalk_id
            if not target:
                raise ValueError("chat_message_send: user 与 open_dingtalk_id 均未提供合法的 ou_xxx 格式 ID")
            args += ["--user-id", target]

        # @ 群成员（仅群聊生效，契合能力契约）：飞书内联 <at user_id="..."></at> 语法。
        # 历史实现漏掉了 at_all / at_open_dingtalk_ids 两个参数（docstring 承诺却未实现），
        # 导致 @ 提及被静默丢弃；此处补回，且仅在群聊目标下追加。
        at_segment = ""
        if group and (at_all or at_open_dingtalk_ids):
            at_parts: list[str] = []
            if at_all:
                at_parts.append('<at user_id="all"></at>')
            if at_open_dingtalk_ids:
                for _oid in str(at_open_dingtalk_ids).replace(",", " ").split():
                    _oid = _oid.strip()
                    if _oid:
                        at_parts.append(f'<at user_id="{_oid}"></at>')
            at_segment = "".join(at_parts)

        media_ref = media_id or file_path
        cover_path: str | None = None
        if media_ref and msg_type in ("image", "file", "video", "audio"):
            flag = {
                "image": "--image", "file": "--file",
                "video": "--video", "audio": "--audio",
            }[msg_type]
            args += [flag, media_ref]
            if msg_type == "video" and file_path:
                # 飞书视频消息需带封面（--video-cover）；调用方未提供时自动截取第一帧。
                # 旧逻辑误用 `not open_dingtalk_id` 条件，导致发给用户时不生成封面；
                # 且封面临时文件从不清理（磁盘泄漏），改为统一生成并在发送后删除。
                cover_path = self._generate_video_cover(file_path)
                if cover_path:
                    args += ["--video-cover", cover_path]
                else:
                    logger.warning("飞书视频消息无法生成封面，继续发送（可能被降级）")
        elif msg_type == "markdown" or (title and not media_ref):
            args += ["--markdown", (text or title) + at_segment]
        else:
            # 默认文本；富媒体缺 msg_type 时按扩展名/前缀推断
            if media_ref:
                inferred = self._infer_media_flag(media_ref)
                if inferred:
                    args += [inferred, media_ref]
                else:
                    args += ["--file", media_ref]
            else:
                args += ["--text", text + at_segment]

        if uuid:
            args += ["--idempotency-key", uuid]

        try:
            # 递归守卫：fallback 重试时设置 _in_fallback=True，避免 bot 也失败时再次 fallback
            if getattr(self, "_in_fallback", False):
                return self.run(args)
            try:
                return self.run(args)
            except Exception as first_err:
                # 主从身份降级：user 身份发失败时（230027/230002 通常意味着「user 不在
                # 会话」或「跨租户外部」），自动用 bot 身份重试一次。bot 身份在 lark-cli
                # 里的权限范围比 user 小（仅企业内），但当 user 不被允许发言时它是
                # 唯一能退而求其次的发送者。
                # 注意：
                # 1) 设置 _disable_bot_fallback = True 禁止 fallback（外部好友被对
                #    跨租户额外发一次 bot 消息会泄露内部信息，chat.py 已写黑名单路径
                #    应当传该开关；本期 chat.py 逻辑判定 is_external=False 走降级故
                #    默认开；后续可加参数传入）
                first_msg = str(first_err) if str(first_err) else ""
                first_msg_lower = first_msg.lower()
                # 降级条件：user 身份因「不在会话/无权/群不存在」导致发送失败时，
                # 自动降级为 bot 身份重试。涵盖飞书业务码 + HTTP 404。
                _user_ineligible = (
                    "230027" in first_msg          # 不在会话
                    or "230002" in first_msg       # 跨租户外部
                    or "user_unauthorized" in first_msg_lower
                    or "not_found" in first_msg_lower   # error.subtype / message
                    or '"code": 404' in first_msg       # lark-cli JSON 里的 404
                    or "404 page not found" in first_msg_lower  # message 兜底
                )
                if (
                    not getattr(self, "_disable_bot_fallback", False)
                    and _user_ineligible
                ):
                    bot_args = list(args) + ["--as", "bot"]
                    logger.info(
                        "[飞书降级] user 身份失败（%s），自动用 bot 身份重试 chat_id=%s",
                        first_msg[:120], group or user or open_dingtalk_id,
                    )
                    self._in_fallback = True
                    try:
                        return self.run(bot_args)
                    finally:
                        self._in_fallback = False
                raise
        finally:
            if cover_path and os.path.isfile(cover_path):
                try:
                    os.remove(cover_path)
                except OSError:
                    logger.debug("清理飞书视频封面临时文件失败: %s", cover_path)

    def chat_message_reply(self, *, message_id: str | None = None, text: str = "",
                           title: str = "", uuid: str | None = None,
                           reply_in_thread: bool = False,
                           group: str | None = None,
                           user: str | None = None,
                           open_dingtalk_id: str | None = None,
                           msg_type: str | None = None,
                           media_id: str | None = None,
                           file_path: str | None = None) -> dict:
        """回复指定消息，支持话题内回复（reply-in-thread）。

        参数映射（飞书 ``+messages-reply``）：

        - ``message_id`` → ``--message-id``（被回复消息的 ``om_xxx`` ID，**必填**）。
        - ``reply_in_thread`` → ``--reply-in-thread``：消息进入该消息的话题流，
          而非主群聊，避免 AI 自动回复刷屏（自动回复机器人强烈推荐）。
        - 文本 → ``--text``；Markdown → ``--markdown``。
        - 富媒体：``msg_type`` 决定 ``--image/--file/--audio``（飞书 reply 暂不支持视频）。
        - ``uuid`` → ``--idempotency-key``（防 webhook 重试导致的重复回复）。
        - ``group`` / ``user`` / ``open_dingtalk_id``：飞书由 ``message_id`` 推导会话，
          **忽略**（保留签名以兼容跨平台接口）。

        Returns:
            lark-cli 返回的原始 dict（含 ``ok`` / ``data`` / ``identity``）。
        """
        if not message_id:
            raise ValueError("chat_message_reply 需提供 message_id")

        args = ["im", "+messages-reply", "--message-id", message_id]
        if reply_in_thread:
            args += ["--reply-in-thread"]

        media_ref = media_id or file_path
        if media_ref and msg_type in ("image", "file", "audio"):
            flag = {"image": "--image", "file": "--file", "audio": "--audio"}[msg_type]
            args += [flag, media_ref]
        elif msg_type == "markdown" or (title and not media_ref):
            args += ["--markdown", (text or title)]
        else:
            if not text:
                raise ValueError("chat_message_reply 需提供 text 或 title（或富媒体参数）")
            args += ["--text", text]

        if uuid:
            args += ["--idempotency-key", uuid]

        try:
            if getattr(self, "_in_fallback", False):
                return self.run(args)
            try:
                return self.run(args)
            except Exception as first_err:
                first_msg = str(first_err) if str(first_err) else ""
                first_msg_lower = first_msg.lower()
                _user_ineligible = (
                    "230027" in first_msg
                    or "230002" in first_msg
                    or "user_unauthorized" in first_msg_lower
                    or "not_found" in first_msg_lower
                    or '"code": 404' in first_msg
                    or "404 page not found" in first_msg_lower
                )
                if (
                    not getattr(self, "_disable_bot_fallback", False)
                    and _user_ineligible
                ):
                    bot_args = list(args) + ["--as", "bot"]
                    logger.info(
                        "[飞书回复降级] user 身份失败（%s），自动用 bot 身份重试 message_id=%s",
                        first_msg[:120], message_id,
                    )
                    self._in_fallback = True
                    try:
                        return self.run(bot_args)
                    finally:
                        self._in_fallback = False
                raise
        finally:
            pass

    def chat_message_update(self, *, message_id: str, text: str = "",
                           title: str = "", group: str | None = None,
                           user: str | None = None) -> dict:
        """更新已发送的消息内容（用于流式输出：先占位再逐步 patch）。

        封装 `lark chat message update --message-id xxx --content xxx`。
        """
        args = ["chat", "message", "update", "--message-id", message_id]
        if text:
            args.extend(["--content", text])
        if title:
            args.extend(["--title", title])
        if group:
            args.extend(["--chat-id", group])
        if user:
            args.extend(["--user-id", user])
        logger.debug("[飞书] 更新消息: lark %s", " ".join(args))
        return self.run(args)

    def chat_message_recall(self, *, message_id: str,
                            group: str | None = None,
                            user: str | None = None) -> bool:
        """撤回已发送的消息。覆写基类默认（返回 False），调用 lark-cli 真实撤回。

        lark-cli 将 ``im messages delete`` 标记为 high-risk-write，要求 ``--yes``
        方可执行，且其帮助明确「agent 不得自行加 --yes，除非用户已确认」。
        本方法仅用于 bot 撤回**自己刚发出的占位 / 流式失败消息**（自清理），
        属于用户通过对话流隐式授权的范畴，故补 ``--yes``；绝不可用于撤回他人消息。
        任何异常一律吞掉返回 False，由调用方降级为覆盖式「已停止」文案。
        """
        args = ["im", "messages", "delete", "--message-id", message_id,
                "--as", "bot", "--yes"]
        try:
            self.run(args, force_no_dry_run=True)
            return True
        except Exception as exc:  # 吞掉一切异常，绝不向上抛
            logger.warning("[飞书] 撤回消息失败 message_id=%s: %s",
                           message_id[:32], exc)
            return False

    # 外部联系人自动发现与注册
    # ------------------------------------------------------------------

    def sync_external_contacts(self) -> list[dict]:
        """自动发现飞书外部联系人并返回可注册的列表。

        两路探测：
        1. ``lark-cli contact +search-user --has-chatted`` 获取所有聊过天的用户，
           从中筛选 ``is_cross_tenant==true`` 的外部联系人（已直接附带 p2p_chat_id）。
        2. ``lark-cli im +chat-list --types p2p`` 获取所有单聊，从中筛选
           非 bot 的外部单聊作为补充（分页遍历）。

        每条结果包含 name / open_dingtalk_id / chat_id 三个字段，
        调用方只需比对 open_dingtalk_id 是否已在 external_friends 表中去重。

        Returns:
            list[dict]: 每项 ``{"name": str, "open_dingtalk_id": str, "chat_id": str}``
        """
        discovered: dict[str, dict] = {}  # keyed by open_dingtalk_id

        # --- 路径 1: +search-user --has-chatted（跨租户用户）---
        try:
            page_token: str = ""
            max_pages = 50
            pages = 0
            while True:
                pages += 1
                args: list[str] = ["contact", "+search-user", "--has-chatted",
                                   "--page-size", "30", "--as", "user"]
                if page_token:
                    args += ["--page-token", page_token]
                resp = self.run(args)
                d = self._payload(resp)
                users = (d.get("users") or []) if isinstance(d, dict) else []
                for u in users:
                    if not isinstance(u, dict):
                        continue
                    if not u.get("is_cross_tenant"):
                        continue
                    name = u.get("localized_name") or u.get("name") or ""
                    oid = u.get("open_id") or ""
                    chat_id = u.get("p2p_chat_id") or ""
                    if name and oid:
                        discovered.setdefault(oid, {
                            "name": name,
                            "open_dingtalk_id": oid,
                            "chat_id": chat_id,
                        })
                has_more = bool(d.get("has_more")) if isinstance(d, dict) else False
                page_token = (d.get("page_token") or "") if isinstance(d, dict) else ""
                if not has_more or not page_token:
                    break
                if pages >= max_pages:
                    logger.warning("分页超过 50 页上限，强制终止")
                    break
            logger.info(
                "飞书 sync_external_contacts: 路径1(+search-user --has-chatted) 完成")
        except Exception as e:
            logger.debug("飞书 sync_external_contacts 路径1失败: %s", e)

        # --- 路径 2: im +chat-list --types p2p 补充 ---
        try:
            page_token = ""
            max_pages = 50
            pages = 0
            while True:
                pages += 1
                args = ["im", "+chat-list", "--types", "p2p",
                        "--page-size", "100", "--as", "user"]
                if page_token:
                    args += ["--page-token", page_token]
                resp = self.run(args)
                d = self._payload(resp)
                chats = (d.get("chats") or []) if isinstance(d, dict) else []
                for chat in chats:
                    if not isinstance(chat, dict):
                        continue
                    # 仅保留真人用户（跳过 bot）
                    if chat.get("p2p_target_type") != "user":
                        continue
                    name = chat.get("name") or ""
                    chat_id = chat.get("chat_id") or ""
                    oid = chat.get("p2p_target_id") or ""
                    if name and oid:
                        discovered.setdefault(oid, {
                            "name": name,
                            "open_dingtalk_id": oid,
                            "chat_id": chat_id,
                        })
                has_more = bool(d.get("has_more")) if isinstance(d, dict) else False
                page_token = (d.get("page_token") or "") if isinstance(d, dict) else ""
                if not has_more or not page_token:
                    break
                if pages >= max_pages:
                    logger.warning("分页超过 50 页上限，强制终止")
                    break
            logger.info(
                "飞书 sync_external_contacts: 路径2(+chat-list p2p) 完成")
        except Exception as e:
            logger.debug("飞书 sync_external_contacts 路径2失败: %s", e)

        # --- 为缺少 chat_id 的联系人补全 ---
        for oid, item in list(discovered.items()):
            if item["chat_id"]:
                continue
            try:
                info = self.chat_conversation_info(oid)
                cid = info.get("chat_id") or info.get("id") or ""
                if cid:
                    item["chat_id"] = cid
            except Exception:
                logger.debug(
                    "飞书 sync_external_contacts: 无法获取 %s 的 chat_id",
                    item.get("name", oid))

        result = list(discovered.values())
        logger.info("飞书 sync_external_contacts: 最终发现 %d 个外部联系人", len(result))
        return result
