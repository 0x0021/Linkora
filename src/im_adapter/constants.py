"""IM 适配器共享常量。

**独立成模块（不依赖任何其它模块）以避免循环导入**：``src.im_adapter.base``（CLI
执行引擎）会重导出飞书 / 企微适配器，而 ``src.im_adapter.wecom`` 与
``src.im_adapter.base`` 都需要下载大小上限——常量放在这里双方都能安全导入，
不必让任一方向形成 import 环。
"""

# 媒体 / 图片下载大小上限（10MB）：OCR 场景一张图通常几 KB ~ 几 MB，
# 早期 50MB 过大，易被恶意链接或异常响应打爆磁盘。企微 ``download_media``
# （base64 解码后）与通用下载路径共用同一上限。
MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024
