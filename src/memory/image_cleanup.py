"""清理消息时连带删除磁盘孤儿图片（``data/tmp_images`` 下）。

消息删除路径（定期清理 / 单条撤回 / 批量删会话）必须把 DB 行引用的本地图片一并
删除，否则图片成孤儿文件永久累积（磁盘泄漏）。图片文件名含 ``msg_id``
（``ocr_<msg_id>.png`` / ``card_<key>.png``），与消息 1:1，可直接删除。

路径基准：``<db_path 所在目录>/tmp_images``，与 ``config_models.DEFAULT_TMP_IMAGES_DIR``
（``data_path("tmp_images")``）一致，且对 data-dir 覆盖也正确（不依赖 config 对象）。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def purge_orphan_images(db_path: str, rel_paths: list[str]) -> int:
    """删除相对 ``data/tmp_images`` 的孤儿图片文件，返回成功删除的文件数。

    仅删存在且可删的文件；任何异常（权限/并发/路径越界）静默跳过，不影响主流程。
    ⚠️ 路径越界护栏：``image_path`` 虽经 ``safe_path_component`` 清掉 ``/``，仍做一层
    ``base.resolve() in p.parents`` 校验，防止极端情况下 ``../`` 穿越删到系统文件。
    """
    if not rel_paths:
        return 0
    base = (Path(db_path).resolve().parent / "tmp_images").resolve()
    removed = 0
    for rel in rel_paths:
        if not rel:
            continue
        try:
            p = (base / rel).resolve()
            if base != p and base not in p.parents:
                logger.warning("跳过越界图片路径（疑似注入，不删除）: %s", rel)
                continue
            if p.is_file():
                p.unlink()
                removed += 1
        except OSError as _exc:
            logger.debug("清理孤儿图片失败（已忽略）: %s | %s", rel, _exc)
    return removed
