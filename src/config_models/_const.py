"""配置模型的共享常量（P2-7 第 3 步外移）。

原先这四个常量定义在 ``src/config_models/__init__.py``，被各配置模型直接引用
（如 ``StorageConfig.path`` 默认取 ``DEFAULT_STORAGE_PATH``）。模型类按领域
外移到子模块后，若常量仍留在 ``__init__.py``，子模块就得 import 包本身 ——
形成「子模块 → 包 → 子模块」的循环依赖。

故把常量提取到本模块（**零依赖**，只依赖 ``src.paths``）：
``platform`` / ``app`` 等子模块运行时 import 本模块即可，无环。

注意：这是**运行时** import 而非 TYPE_CHECKING —— Pydantic v2 依据
``cls.__module__`` 取该模块 globals 求值注解，常量若不可求值，字段默认值
会变成不可用的 ``PydanticUndefinedAnnotation``。
"""
from __future__ import annotations

from src.paths import data_path, get_data_dir

DEFAULT_DATA_DIR = str(get_data_dir())
DEFAULT_STORAGE_PATH = str(data_path("linkora.db"))
DEFAULT_TMP_IMAGES_DIR = str(data_path("tmp_images"))
DEFAULT_BACKUP_DIR = str(data_path("backups"))

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_STORAGE_PATH",
    "DEFAULT_TMP_IMAGES_DIR",
    "DEFAULT_BACKUP_DIR",
]
