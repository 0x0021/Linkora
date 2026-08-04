"""飞书文档导入业务逻辑。

纯业务逻辑，不依赖 FastAPI。直接依赖 FeishuCliAdapter / SQLiteStore。
由 web/routers/kb.py 的 HTTP 层调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.memory.embedding import EmbeddingClient
from src.shared_state import get_app_instance
from src.tools.utils import split_text

logger = logging.getLogger(__name__)


class FeishuImportError(Exception):
    """飞书导入业务异常，由 HTTP 层转换为对应的 HTTPException。"""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _get_feishu_adapter():
    """获取飞书适配器实例。

    Raises:
        FeishuImportError: 应用未启动或飞书适配器不可用。
    """
    inst = get_app_instance()
    if inst is None:
        raise FeishuImportError("应用未启动（共享状态不可用）", status_code=503)

    for pctx in inst.platforms.values():
        if pctx.adapter_type == "feishu":
            return pctx.dws

    raise FeishuImportError("飞书适配器不可用", status_code=503)


def _create_embedding_client(config: Any) -> EmbeddingClient | None:
    """根据配置创建 EmbeddingClient，embedding 未启用则返回 None。"""
    if not config.embedding.enabled:
        return None
    return EmbeddingClient(config.embedding)


def import_single_feishu_doc(
    store: Any,
    doc_token: str,
    title: str = "",
    doc_type: str = "feishu",
    entity_type: str = "",
    rag_config: dict | None = None,
    config: Any = None,
) -> dict:
    """导入单篇飞书文档到知识库。

    Args:
        store: SQLiteStore 实例。
        doc_token: 飞书文档 token。
        title: 文档标题，空则从飞书文档获取。
        doc_type: 文档类型标签。
        entity_type: 文档实体类型（走正确的导入回退链路）。
        rag_config: RAG 配置 dict，含 chunk_size / chunk_overlap。
        config: 应用配置（用于 embedding）。

    Returns:
        dict: {"success": bool, "id": int, "title": str, "chunks": int,
               "embed_failed": int, "message": str}

    Raises:
        FeishuImportError: 适配器不可用 / 文档读取失败 / 内容为空。
    """
    if rag_config is None:
        rag_config = {"chunk_size": 800, "chunk_overlap": 200}

    feishu_adapter = _get_feishu_adapter()

    # 读取飞书文档内容
    doc_data = feishu_adapter.doc_read(doc_token, entity_type=entity_type or None)
    if not doc_data:
        raise FeishuImportError(
            "无法读取飞书文档 {}".format(doc_token),
            status_code=404,
        )
    if isinstance(doc_data, dict) and doc_data.get("error"):
        err = doc_data.get("error")
        if err == "auth":
            raise FeishuImportError(
                doc_data.get("message", "飞书认证失败，请检查 lark-cli 登录状态"),
                status_code=401,
            )
        raise FeishuImportError(
            doc_data.get("message", "飞书文档读取失败: {}".format(err)),
            status_code=400,
        )

    content = doc_data.get("content", "")
    if not content:
        raise FeishuImportError("飞书文档内容为空", status_code=400)

    if not title:
        title = doc_data.get("title", doc_token)

    source = "feishu://{}".format(doc_token)

    doc_id = store._kb_repo.add_kb_document(
        title=title,
        doc_type=doc_type,
        source=source,
        content=content,
    )

    chunks = split_text(
        content,
        max_len=rag_config["chunk_size"],
        overlap=rag_config["chunk_overlap"],
    )
    store._kb_repo.add_kb_chunks(doc_id, chunks)

    embed_failed = 0
    if config is not None:
        embed_client = _create_embedding_client(config)
        if embed_client is not None:
            all_chunks = store._kb_repo.list_kb_chunks(doc_id)
            for chunk in all_chunks:
                emb = embed_client.embed_with_retry(chunk["content"])
                if emb:
                    store._kb_repo.update_chunk_embedding(chunk["id"], emb)
                else:
                    embed_failed += 1

    return {
        "success": True,
        "id": doc_id,
        "title": title,
        "chunks": len(chunks),
        "embed_failed": embed_failed,
        "message": "飞书文档「{}」导入成功，已分 {} 个块".format(title, len(chunks)),
    }


def import_feishu_folder(
    store: Any,
    folder_token: str,
    doc_type: str = "feishu",
    rag_config: dict | None = None,
    config: Any = None,
) -> dict:
    """批量导入飞书文件夹下所有文档。

    Args:
        store: SQLiteStore 实例。
        folder_token: 飞书文件夹 token。
        doc_type: 文档类型标签。
        rag_config: RAG 配置 dict。
        config: 应用配置（用于 embedding）。

    Returns:
        dict: {"success": bool, "imported_count": int, "failed_count": int,
               "imported": list, "failed": list, "message": str}
    """
    if rag_config is None:
        rag_config = {"chunk_size": 800, "chunk_overlap": 200}

    feishu_adapter = _get_feishu_adapter()

    imported: list[dict] = []
    failed: list[dict] = []
    page_token = ""

    while True:
        result = feishu_adapter.doc_list(
            folder_token=folder_token,
            page_token=page_token,
        )
        items = result.get("items", [])
        if not items:
            break

        for item in items:
            item_token = (
                item.get("doc_token")
                or item.get("token")
                or item.get("id", "")
            )
            item_title = item.get("title") or item.get("name", "")
            if not item_token:
                continue
            try:
                r = import_single_feishu_doc(
                    store,
                    item_token,
                    item_title,
                    doc_type,
                    rag_config=rag_config,
                    config=config,
                )
                imported.append({
                    "token": item_token,
                    "title": item_title,
                    "id": r.get("id"),
                })
            except Exception as e:
                logger.warning("飞书文档 '%s' 导入失败: %s", item_title, str(e))
                failed.append({
                    "token": item_token,
                    "title": item_title,
                    "error": str(e),
                })

        if not result.get("has_more"):
            break
        page_token = result.get("page_token", "")

    return {
        "success": len(failed) == 0,
        "imported_count": len(imported),
        "failed_count": len(failed),
        "imported": imported,
        "failed": failed,
        "message": "飞书文件夹导入完成：成功 {} 篇，失败 {} 篇".format(
            len(imported), len(failed),
        ),
    }


def search_feishu_docs(query: str, page_size: int = 10) -> list[dict]:
    """搜索飞书文档。

    Args:
        query: 搜索关键词。
        page_size: 每页数量。

    Returns:
        list[dict]: 飞书文档列表。

    Raises:
        FeishuImportError: 适配器不可用。
    """
    feishu_adapter = _get_feishu_adapter()
    return feishu_adapter.doc_search(query, page_size=page_size)
