from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)

# 允许上传的根目录：项目 data/ 与系统临时目录（OCR 输出、生成图表等落于此）。
# 任何解析后越出这些根的路径（含 ../ 越界、符号链接逃逸）一律拒绝，
# 防止提示注入诱导上传 /etc/passwd、SSH 私钥、.env 等敏感文件。
# 注意对根目录也做 realpath：macOS 上 /tmp 是 -> /private/tmp 的符号链接，
# 而 tempfile.gettempdir() 通常指向 /private/var/folders/.../T（pytest 的 tmp_path
# 也落在此）。两者解析后路径不同，故一并纳入，确保「显式写 /tmp」与
# 「系统/测试临时目录」都能上传。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED_ROOTS = [
    os.path.realpath(str(_PROJECT_ROOT / "data")),
    os.path.realpath("/tmp"),
    os.path.realpath(tempfile.gettempdir()),
]


def is_allowed_local_path(file_path: str) -> bool:
    """判断本地文件路径是否落在允许根目录内（data/ 与 /tmp）。

    供 send_message 发送 file/audio/video/image 前的越权校验复用，
    防止提示注入诱导分身把 /etc/passwd、SSH 私钥、.env 等敏感文件发出去。
    """
    try:
        real = os.path.realpath(file_path)
    except OSError:
        return False
    roots = list(_ALLOWED_ROOTS)
    return any(real == r or real.startswith(r + os.sep) for r in roots)


class ImageUploadTool(BaseTool):
    name = "upload_image"
    display_name = "上传媒体"
    short_description = "上传本地图片/语音/视频/文件到钉钉，返回 media_id 供发送富媒体消息使用"
    description = (
        "上传本地媒体文件（图片/语音/视频/文件）到钉钉，返回 media_id，"
        "随后可用 send_message（msg_type=image 等）携带该 media_id 发送。\n"
        "何时用：\n"
        "• 你手头只有本地文件路径（如刚生成的图表、截图、二维码、附件），需要先把文件上传到钉钉拿到 media_id。\n"
        "• 需要复用同一个 media_id（例如发给多个会话、或先上传后由程序拼装消息）。\n"
        "• 注意：send_message 在 msg_type=image 且只传 file_path 时也会自动上传，"
        "  因此本工具主要用于「需要显式拿到 media_id」的场景。\n"
        "参数：\n"
        "• file_path（必填）：本地文件路径，仅允许项目 data/ 与 /tmp 下（安全限制，防越权读取敏感文件）。\n"
        "• media_type（可选）：image（默认）/ voice / video / file。\n"
        "底层封装 dws `chat media upload --file <path> --type <type>`，需要真实应用凭证。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.media（单一真源）
    intent_categories = ["domain.media"]
    # 钉钉媒体上传专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "本地文件路径（必填），如 /tmp/chart.png、data/tmp_images/xxx/ocr_xxx.png"
            },
            "media_type": {
                "type": "string",
                "enum": ["image", "voice", "video", "file"],
                "description": "媒体类型，默认 image。voice=语音, video=视频, file=通用文件"
            },
        },
        "required": ["file_path"],
    }

    def __init__(self, dws: DwsAdapter, config=None):
        self.dws = dws
        self.config = config

    def _is_allowed_path(self, file_path: str) -> bool:
        """解析真实路径，确认未越出允许根目录（防 ../ 与符号链接逃逸）。"""
        try:
            real = os.path.realpath(file_path)
        except OSError:
            return False
        roots = list(_ALLOWED_ROOTS)
        # 允许配置里显式指定的临时目录（如 image_temp_dir）一并放开
        if self.config and getattr(self.config, "poller", None):
            itd = getattr(self.config.poller, "image_temp_dir", None)
            if itd:
                roots.append(os.path.realpath(itd))
        return any(real == r or real.startswith(r + os.sep) for r in roots)

    def execute(self, args: dict) -> str | dict:
        file_path = (args.get("file_path") or "").strip()
        media_type = (args.get("media_type") or "image").strip().lower()

        if not file_path:
            return {"error": "file_path 必填（本地文件路径）"}
        if not self._is_allowed_path(file_path):
            return {"error": f"安全限制：仅允许上传 data/ 与 /tmp 下的文件，拒绝: {file_path}"}
        if not os.path.isfile(file_path):
            return {"error": f"文件不存在或不是普通文件: {file_path}"}
        if media_type not in ("image", "voice", "video", "file"):
            return {"error": f"不支持的 media_type: {media_type}（应为 image/voice/video/file）"}

        try:
            media_id = self.dws.media_upload(file_path, media_type)
        except Exception as e:
            logger.error("上传媒体失败: %s", e)
            return {"error": f"上传失败: {e}"}

        return {
            "success": True,
            "media_id": media_id,
            "media_type": media_type,
            "file_path": file_path,
            "hint": "可将此 media_id 传给 send_message 的 media_id 参数（msg_type=image）发送",
        }
