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

        ⚠️ 版本提示（2026-09-15 核实 v1.0.61 与 v1.0.62-beta.8）：dws 已**下线**
        ``chat media upload``——它现在只返回 validation error（"已下线，当前 CLI 不提供
        通用的本地文件到 mediaId 的上传能力"）。官方替代路径：
          • 本地图片/文件 → ``chat message send --msg-type file --file <本地路径>``
            （Linkora 的 ``chat_message_send(msg_type="image", file_path=...)``
             已自动降级走此路径，无需经过本方法）
          • 已有 mediaId → ``chat message send --msg-type image --media-id <mediaId>``
        本方法仍保留真实调用（而非硬编码抛错）：若 dws 日后恢复该能力，功能自动回归。

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
            if "已下线" in str(msg) or "deprecated" in str(msg).lower():
                raise RuntimeError(
                    "dws 已下线 chat media upload（不再支持「本地文件 → mediaId」的通用上传）。"
                    "替代：直接用 send_message 传 file_path（msg_type=image 时会自动降级为"
                    "文件消息发送），或对已有 mediaId 用 msg_type=image + media_id。"
                )
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

        封装 dws v1.0.63 的 Shortcut ``chat +messages-resource-download``，
        返回写入的本地文件路径。
        media_id: 消息内容中的 mediaId；message_id: openMessageId；
        conversation_id: openConversationId（即 chat_id）。

        为什么用 Shortcut 而非原子 ``chat message download-media``（2026-10-22 升级）：
        Shortcut 版本自带三项原实现不具备的安全属性（实测逐条验证）：
          - **路径沙箱**：``--output`` 只接受工作目录内的相对路径，拒绝绝对路径、
            拒绝 ``..`` 逃逸；原实现直接接受任意绝对路径，媒体 id 一旦被污染
            （``content`` 里塞 ``mediaId=...&text=`` 之类）就可能写到任意位置；
          - **原子落盘**：整文件临时落盘后原子发布，失败不留下半截文件
            （原实现失败时可能残留 0 字节文件，被下游当成「下载到空图」）；
          - **默认不覆盖**：目标已存在时拒绝写入，需显式 ``--overwrite``。

        ⚠️ Shortcut 要求 ``--output`` 是**工作目录内的相对路径**，故这里把子进程
        的 cwd 切到目标文件所在目录、只传文件名。目标目录不存在时先创建
        （Shortcut 自身不建目录）。

        ⚠️ 必须传 ``--overwrite``：Shortcut 默认拒绝覆盖已存在文件，而 Linkora
        的调用方（``_resolve_image_content`` 等）允许同名文件被重新下载
        （原实现是覆盖语义）。不加该 flag 时，已存在的目标文件会让整次下载
        失败 → 表现为「图片下载失败」。安全属性（路径沙箱 / 原子落盘）不受影响。
        """
        if not media_id or not message_id or not conversation_id:
            raise ValueError("download_media 需提供 media_id / message_id / conversation_id")
        target = os.path.abspath(output_path)
        target_dir = os.path.dirname(target) or "."
        filename = os.path.basename(target)
        if not filename:
            raise ValueError(f"download_media: output_path 无有效文件名: {output_path}")
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            raise OSError(f"download_media: 无法创建目标目录 {target_dir}: {e}") from e

        args = [
            "chat", "+messages-resource-download",
            "--type", "mediaId",
            "--resource-id", media_id,
            "--message-id", message_id,
            "--open-conversation-id", conversation_id,
            "--output", filename,
            "--overwrite",
        ]
        logger.info("[DWS] 下载媒体(Shortcut): mediaId=%s msg=%s conv=%s -> %s",
                    media_id[:20], message_id[:20], conversation_id[:20], target)
        # cwd 切到目标目录 → --output 传文件名即「工作目录内相对路径」。
        # _run_download 负责校验产物存在且非空（含 10MB 上限）。
        return self._run_download(args, target, cwd=target_dir)

    def chat_conversation_info(self, chat_id: str) -> dict:
        """获取会话详情。chat_id 即 openConversationId。

        ⚠️ dws 返回的是**嵌套**结构：``result.conversationInfo.{title, memberCount,
        singleChat, ownerNick, ...}``（实测 2026-10-22）。此处向上取一层
        ``conversationInfo`` 摊平返回，使调用方（``GetConversationInfoTool``）
        能按扁平 key 取值——否则 title / memberCount / singleChat 全部取不到，
        工具会误报「标题为空、单聊、人数 0」。
        """
        data = self.run(["chat", "conversation-info", "--group", chat_id],
                       operation="chat_conversation_info", force_no_dry_run=True)
        result = self._get_result(data)
        if not isinstance(result, dict):
            return {}
        # result 形如 {"conversationInfo": {...}}；有的版本直接摊平，两种都兼容。
        inner = result.get("conversationInfo")
        if isinstance(inner, dict):
            return inner
        return result

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
