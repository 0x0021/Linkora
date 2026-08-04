"""钉钉文档管理（列表/详情/删除/搜索/同步/自动同步/导入知识库/更新）路由。

从 `web/api.py` 抽取（原 700–937 行，跳过 ExternalFriendCreate 类定义），
业务逻辑不变。
- get_store / get_dws / load_config / CONFIG_PATH / _get_embedding_client
  经 `import web.api as _api` 做属性访问，以尊重测试对 `web.api.*` 的 monkeypatch。
- DingTalkDocSync / DingTalkDocImportKb / AutoSyncUpdate 模型自 web.api 导入。
- split_text 自 src.tools.utils 直接导入（叶子模块）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from src.tools.utils import split_text

import web.api as _api
from web.schemas import DingTalkDocSync, DingTalkDocImportKb, AutoSyncUpdate
from web.dependencies import logger, run_sync

router = APIRouter()


@router.get("/api/dingtalk-docs")
async def list_dingtalk_docs(keyword: str = "", limit: int = 100):
    try:
        limit = max(1, min(limit, 500))
        def _work():
            store = _api.get_store()
            return store._docs_repo.list_dingtalk_docs(keyword=keyword, limit=limit)
        docs = await run_sync(_work)
        return {"docs": docs, "total": len(docs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/dingtalk-docs/{doc_id}")
async def get_dingtalk_doc(doc_id: str):
    try:
        def _work():
            store = _api.get_store()
            doc = store._docs_repo.get_dingtalk_doc(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")
            return {"doc": doc}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/dingtalk-docs/{doc_id}")
async def delete_dingtalk_doc(doc_id: str):
    try:
        def _work():
            store = _api.get_store()
            store._docs_repo.delete_dingtalk_doc(doc_id)
        await run_sync(_work)
        return {"success": True, "message": "文档删除成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




# (external_friends 路由 → web/routers/external_friends.py)
@router.post("/api/dingtalk-docs/search")
async def search_dingtalk_docs(body: DingTalkDocSync):
    try:
        def _work():
            dws = _api.get_dws()
            return dws.doc_search(body.query or "", page_size=20)
        results = await run_sync(_work)
        return {"docs": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/dingtalk-docs/sync/{doc_id}")
async def sync_dingtalk_doc(doc_id: str):
    try:
        def _work():
            dws = _api.get_dws()
            doc_content = dws.doc_read(doc_id, content_format="markdown")

            title = doc_content.get("title", "") or doc_content.get("name", "")
            doc_type = doc_content.get("type", "") or "doc"
            url = doc_content.get("url", "")
            content = doc_content.get("content", "") or doc_content.get("markdown", "")
            last_modified = doc_content.get("lastModified", "") or doc_content.get("modified_at", "")

            store = _api.get_store()
            store._docs_repo.upsert_dingtalk_doc(
                doc_id=doc_id,
                title=title,
                doc_type=doc_type,
                url=url,
                content=content,
                last_modified=last_modified,
            )
            doc = store._docs_repo.get_dingtalk_doc(doc_id)
            return {"success": True, "doc": doc, "message": "文档同步成功"}
        return await run_sync(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/dingtalk-docs/sync-batch")
async def sync_dingtalk_docs_batch(body: DingTalkDocSync):
    try:
        def _work():
            dws = _api.get_dws()
            results = dws.doc_search(body.query or "", page_size=20)
            store = _api.get_store()
            synced = 0
            for doc in results:
                doc_id = doc.get("docId") or doc.get("id") or doc.get("nodeId")
                title = doc.get("title") or doc.get("name", "")
                doc_type = doc.get("type") or doc.get("docType", "")
                url = doc.get("url", "")
                if doc_id and title:
                    store._docs_repo.upsert_dingtalk_doc(
                        doc_id=doc_id,
                        title=title,
                        doc_type=doc_type,
                        url=url,
                    )
                    synced += 1
            return {"success": True, "synced": synced, "message": f"已同步 {synced} 篇文档元数据"}
        return await run_sync(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/dingtalk-docs/{doc_id}/auto-sync")
async def set_dingtalk_doc_auto_sync(doc_id: str, body: AutoSyncUpdate):
    try:
        def _work():
            store = _api.get_store()
            doc = store._docs_repo.get_dingtalk_doc(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")
            store._docs_repo.set_doc_auto_sync(doc_id, body.auto_sync)
            return {
                "success": True,
                "doc_id": doc_id,
                "auto_sync": body.auto_sync,
                "message": "已开启自动同步" if body.auto_sync else "已关闭自动同步",
            }
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/dingtalk-docs/import-kb")
async def import_dingtalk_doc_to_kb(body: DingTalkDocImportKb):
    try:
        def _work():
            store = _api.get_store()
            doc = store._docs_repo.get_dingtalk_doc(body.doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在，请先同步文档内容")
            if not doc.get("content"):
                dws = _api.get_dws()
                doc_content = dws.doc_read(body.doc_id, content_format="markdown")
                content = doc_content.get("content") or doc_content.get("markdown", "")
                last_modified = doc_content.get("lastModified", "") or doc_content.get("modified_at", "")
                store._docs_repo.upsert_dingtalk_doc(
                    doc_id=body.doc_id,
                    title=doc["title"],
                    doc_type=doc.get("doc_type", ""),
                    url=doc.get("url", ""),
                    content=content,
                    last_modified=last_modified,
                )
                doc = store._docs_repo.get_dingtalk_doc(body.doc_id)

            title = doc["title"]
            content = doc.get("content", "")
            if not content:
                raise HTTPException(status_code=400, detail="文档内容为空，无法导入")

            # 查重检查
            dup = store._kb_repo.check_duplicate_document(
                title=title,
                content=content,
                source_id=body.doc_id,
                url=doc.get("url", ""),
            )
            if dup["duplicate"]:
                return {
                    "success": False,
                    "duplicate": True,
                    "reason": dup["reason"],
                    "existing_doc": dup["doc"],
                    "message": f"文档重复：{dup['reason']}，已有文档《{dup['doc'].get('title', '')}》",
                }

            kb_doc_id = store._kb_repo.add_kb_document(
                title=title,
                doc_type="dingtalk",
                source="dingtalk",
                source_id=body.doc_id,
                url=doc.get("url", ""),
                content=content,
            )
            chunks = split_text(content, max_len=_api._get_cfg().rag.chunk_size, overlap=_api._get_cfg().rag.chunk_overlap)
            store._kb_repo.add_kb_chunks(kb_doc_id, chunks)

            config = _api._get_cfg()
            if config.embedding.enabled:
                embed_client = _api._get_embedding_client(config.embedding)
                all_chunks = store._kb_repo.list_kb_chunks(kb_doc_id)
                for chunk in all_chunks:
                    emb = embed_client.embed_with_retry(chunk["content"])
                    if emb:
                        store._kb_repo.update_chunk_embedding(chunk["id"], emb)

            return {
                "success": True,
                "kb_doc_id": kb_doc_id,
                "chunks": len(chunks),
                "message": f"已导入知识库，共 {len(chunks)} 个分块",
            }
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/dingtalk-docs/{doc_id}")
async def update_dingtalk_doc(doc_id: str, body: dict = None):
    """更新钉钉文档（标题、内容等，用于清理干扰字符）。"""
    try:
        def _work():
            store = _api.get_store()
            doc = store._docs_repo.get_dingtalk_doc(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")
            if not body:
                raise HTTPException(status_code=400, detail="请求体不能为空")
            # 只允许更新特定字段
            allowed = {"title", "content", "doc_type"}
            updates = {k: v for k, v in body.items() if k in allowed}
            if updates:
                store._docs_repo.update_dingtalk_doc(doc_id, **updates)
                return {"success": True, "updated": list(updates.keys())}
            raise HTTPException(status_code=400, detail="没有可更新的字段")
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

