"""DwsAdapter 媒体能力 mixin（上传/下载/会话信息/已读回执）。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class DwsAdapterMediaMixin(DwsAdapterBase):
    def media_upload(self, file_path: str, media_type: str = "image") -> str:
        """上传本地媒体文件到钉钉，返回 media_id（供 image 消息发送使用）。

        封装 `dws chat media upload --file <path> --type <type>`。
        该命令需要真实应用凭证（DWS_CLIENT_ID / DWS_CLIENT_SECRET），不支持
        --dry-run 预览，故强制 force_no_dry_run=True 走真实调用。

        media_type: image（默认）/ voice / video / file。
        返回从响应 JSON 中提取的 mediaId；若命令形态变化或鉴权失败会抛出清晰错误。
        """
        if not os.path.isfile(file_path):
            raise ValueError(f"media_upload: 文件不存在: {file_path}")
        args = ["chat", "media", "upload", "--file", file_path, "--type", media_type]
        logger.info("[DWS] 上传媒体: %s (type=%s)", file_path, media_type)
        try:
            data = self.run(args, force_no_dry_run=True)
        except Exception as e:
            raise RuntimeError(
                f"媒体上传失败（dws chat media upload）: {e}。"
                f"请确认已通过 dws auth login 配置应用凭证（DWS_CLIENT_ID/SECRET）。"
            ) from e
        # dws 在鉴权/业务失败时以 {"error": {...}} 形式返回（success 字段缺失，run 不会抛）
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"媒体上传被拒绝: {msg}")
        mid = self._extract_media_id(data)
        if not mid:
            raise RuntimeError(f"媒体上传成功但未解析到 mediaId，原始返回: {str(data)[:300]}")
        return mid

    @staticmethod
    def _extract_media_id(data: Any) -> str:
        """从 chat media upload 的响应 JSON 中提取 mediaId（兼容多种结构）。"""
        if not isinstance(data, dict):
            return ""
        # 1) 顶层平铺字段
        for key in ("mediaId", "media_id"):
            if data.get(key):
                return str(data[key])
        # 2) 常见容器：result / data / media
        for container in (data.get("result"), data.get("data"), data.get("media")):
            if isinstance(container, dict):
                for key in ("mediaId", "media_id"):
                    if container.get(key):
                        return str(container[key])
        # 3) 任意一层嵌套 dict 中含 mediaId / media_id
        for value in data.values():
            if isinstance(value, dict):
                for key in ("mediaId", "media_id"):
                    if value.get(key):
                        return str(value[key])
        return ""

    def download_media(self, *, media_id: str, message_id: str,
                       conversation_id: str, output_path: str) -> str:
        """下载聊天中的图片/视频/语音等资源到本地。

        封装 `dws chat message download-media`。返回写入的本地文件路径。
        media_id: 消息内容中的 mediaId；message_id: openMessageId；
        conversation_id: openConversationId（即 chat_id）。

        复用基类 _run_download：拼命令 → subprocess → 校验产物非空（短路空图进入 OCR 链路）。
        """
        args = [
            "chat", "message", "download-media",
            "--type", "mediaId",
            "--resource-id", media_id,
            "--message-id", message_id,
            "--open-conversation-id", conversation_id,
            "--output", output_path,
        ]
        logger.info("[DWS] 下载媒体: mediaId=%s msg=%s conv=%s -> %s",
                    media_id[:20], message_id[:20], conversation_id[:20], output_path)
        return self._run_download(args, output_path)

    def chat_conversation_info(self, chat_id: str) -> dict:
        """获取会话详情。chat_id 即 openConversationId。"""
        data = self.run(["chat", "conversation-info", "--group", chat_id],
                       operation="chat_conversation_info", force_no_dry_run=True)
        result = self._get_result(data)
        return result if isinstance(result, dict) else {}

    def mark_read(self, conversation_id: str, message_id: str) -> dict:
        """标记会话中指定消息及之前的所有消息为已读。

        Args:
            conversation_id: 会话 openConversationId（群聊/单聊通用）
            message_id:      该会话内某条消息的 openMessageId；标记它及之前全部为已读
        Returns:
            dws CLI 返回的字典；失败抛出对应 DwsError 子类
        """
        if not conversation_id or not message_id:
            raise ValueError("mark_read 需要 conversation_id 与 message_id")
        data = self.run([
            "chat", "mark-read",
            "--conversation-id", conversation_id,
            "--message-id", message_id,
        ], operation="chat_mark_read", force_no_dry_run=True)
        result = self._get_result(data)
        return result if isinstance(result, dict) else {}
