"""飞书媒体能力混入（Mixin）—— 媒体上传 / 下载 / 媒体类型推断。

混入 ``FeishuCliAdapter``，通过 MRO 访问 ``BaseIMAdapter`` 提供的
``_run_download()`` 等核心依赖。
"""
from __future__ import annotations
from .im_mixins_base import IMAdapterBase

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class FeishuMediaMixin(IMAdapterBase):
    """飞书媒体能力混入 —— 提供媒体上传 / 下载 / 类型推断等能力。

    不直接继承任何基类；所有依赖（``self._run_download()`` 等）均由最终类
    ``FeishuCliAdapter`` 的 MRO 在运行时提供。
    """

    @staticmethod
    def _infer_media_flag(ref: str) -> str | None:
        """按 media_id 前缀/扩展名推断 ``+messages-send`` 的媒体参数名。"""
        r = ref.lower()
        if r.startswith("img_") or r.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return "--image"
        if r.startswith("file_") or r.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".txt")):
            return "--file"
        if r.startswith("va_") or r.endswith((".mp4", ".mov")):
            return "--video"
        if r.endswith((".opus", ".ogg", ".wav", ".mp3")):
            return "--audio"
        return None

    @staticmethod
    def _infer_resource_type(media_id: str) -> str:
        """把飞书 media_key 映射为 ``+messages-resources-download`` 的 ``--type`` 取值。

        飞书 media_key 前缀约定：``img_``=图片 / ``file_``=文件 / ``va_``=视频 /
        ``om_``(或 ``voice_``)=语音(音频)。旧实现仅区分 ``img_``→image 与其余→file，
        会把视频/语音错判成 file 导致下载命令失败；此处按前缀 + 扩展名完整推断，
        未知类型回退 file，避免下载命令构造失败。
        """
        r = (media_id or "").lower()
        if r.startswith("img_"):
            return "image"
        if r.startswith("va_"):
            return "video"
        if r.startswith("om_") or r.startswith("voice_"):
            return "audio"
        if r.startswith("file_"):
            return "file"
        if r.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            return "image"
        if r.endswith((".mp4", ".mov", ".m4v")):
            return "video"
        if r.endswith((".opus", ".ogg", ".wav", ".mp3", ".m4a")):
            return "audio"
        return "file"

    @staticmethod
    def _generate_video_cover(video_path: str) -> str | None:
        """为视频消息自动生成封面图（截取第一帧）。

        使用 ffmpeg 将视频首帧导出为同目录下的 ``.cover.jpg`` 临时文件。
        ffmpeg 不可用时返回 None，由调用方决定降级策略。
        """
        import subprocess
        if not os.path.isfile(video_path):
            return None
        cover_path = video_path + ".cover.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-vframes", "1",
                 "-q:v", "2", cover_path],
                check=True, capture_output=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, OSError) as e:
            logger.debug("生成视频封面失败 %s: %s", video_path, e)
            return None
        return cover_path if os.path.isfile(cover_path) else None

    def media_upload(self, file_path: str, media_type: str = "image") -> str:
        """上传本地媒体文件，返回「媒体引用」。

        飞书约定：``+messages-send`` 的 ``--image/--file/--video/--audio`` 直接吃
        **本地相对路径**，故本方法主要做「存在性校验 + 规范化」，返回该路径作为
        媒体引用（调用 ``chat_message_send(media_id=path)`` 即可）。

        注：若后续需 ``img_`` 形式的 media_key（例如以 bot 身份），可在此接入
        ``lark-cli im images create``（该命令为 bot-only）。当前以路径方式保证
        用户身份下可用。

        media_type: image（默认）/ voice / video / file。文件不存在抛 ``ValueError``。
        """
        if not file_path or not os.path.exists(file_path):
            raise ValueError(f"media_upload: 文件不存在 {file_path!r}")
        return file_path

    def download_media(self, *, media_id: str, message_id: str,
                       conversation_id: str, output_path: str) -> str:
        """下载聊天中的图片 / 文件到本地，返回写入的本地路径。

        飞书 ``+messages-resources-download`` 的 ``--output`` 仅接受**相对路径**
        （禁止 ``..`` 穿越），故把 cwd 设为输出目录、``--output`` 传文件名，
        复用基类 ``_run_download`` 的重试与校验逻辑。

        ``conversation_id`` 在飞书下载命令中无需（仅需 message_id + file_key），
        保留参数以对齐接口契约。
        """
        if not media_id or not message_id:
            raise ValueError("download_media 需提供 media_id 与 message_id")

        out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
        out_name = os.path.basename(output_path) or (media_id or "resource")
        res_type = self._infer_resource_type(media_id)

        args = ["im", "+messages-resources-download",
                "--message-id", message_id,
                "--file-key", media_id,
                "--type", res_type,
                "--output", out_name]
        return self._run_download(args, output_path, cwd=out_dir)

