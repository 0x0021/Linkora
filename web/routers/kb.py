"""知识库（RAG）路由：文档增删改查 / URL 导入(SSRF 防护) / 重建索引 / 查询 / 对话 / 统计。

从 `web/api.py` 抽取（原 713–1266 行），业务逻辑不变。
- get_store / load_config / CONFIG_PATH / _get_embedding_client 经 `_api`（惰性代理）做属性访问，
  以尊重测试对 `web.api.*` 的 monkeypatch；代理首次访问时才 import web.api，避免顶层循环导入。
- SSRF 防护 is_ssrf_safe 已迁至 web.security。
- 飞书导入业务逻辑已迁至 src.kb.feishu_importer。
- RagQuery / RagChatQuery / KbDocumentCreate 模型自 web.schemas 导入。
- split_text / SQLiteStore 自对应叶子模块直接导入。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import JSONResponse
from src.memory.sqlite_store import SQLiteStore
from src.tools.utils import split_text

class _LazyApiModule:
    """惰性代理：首次属性访问时才 import web.api，消除 kb↔api 顶层循环导入。

    web/api.py 在文件末尾 `from web.routers.kb import router`，而本模块的 router 在
    `import web.api` 之后才定义。若本模块先于 web.api 被导入，api 在 kb 半初始化阶段
    取 router 会因尚未定义而抛 AttributeError。改为惰性解析后，本模块顶层不再依赖
    web.api，循环依赖消除；同时后续 `_api.get_store()` 等调用仍指向实时 web.api 模块，
    不影响测试对 `web.api.*` 的 monkeypatch。
    """

    def __getattr__(self, name: str):
        import web.api as _mod
        return getattr(_mod, name)


_api = _LazyApiModule()
from web.schemas import RagQuery, RagChatQuery, KbDocumentCreate
from web.dependencies import logger, run_sync
from web.errors import SAFE_OPERATION_FAILED
from web.security import is_ssrf_safe, ssrf_safe_get, build_playwright_launch_args
from src.kb.feishu_importer import (
    FeishuImportError,
    import_single_feishu_doc,
    import_feishu_folder,
    search_feishu_docs as _feishu_doc_search,
)

router = APIRouter()


@router.get("/api/kb/documents")
async def list_kb_documents(status: str = "", doc_type: str = "",
                            limit: int = 50, offset: int = 0):
    try:
        def _work():
            store = _api.get_store()
            limit_ = max(1, min(limit, 500))
            offset_ = max(0, offset)
            docs, total = store._kb_repo.list_kb_documents(status=status, doc_type=doc_type,
                                           limit=limit_, offset=offset_)
            stats = store._kb_repo.kb_stats()
            return {"documents": docs, "stats": stats, "total": total}
        return await run_sync(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/kb/documents/{doc_id}")
async def get_kb_document(doc_id: int):
    try:
        def _work():
            store = _api.get_store()
            doc = store._kb_repo.get_kb_document(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")
            chunks = store._kb_repo.list_kb_chunks(doc_id)
            doc["chunks"] = chunks
            doc["content"] = "\n\n".join(c.get("content", "") for c in chunks if c.get("content"))
            return {"document": doc}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/kb/documents")
async def create_kb_document(doc: KbDocumentCreate):
    try:
        def _work():
            store = _api.get_store()
            # 查重检查
            dup = store._kb_repo.check_duplicate_document(
                title=doc.title,
                content=doc.content,
            )
            if dup["duplicate"]:
                return {
                    "success": False,
                    "duplicate": True,
                    "reason": dup["reason"],
                    "existing_doc": dup["doc"],
                    "message": f"文档重复：{dup['reason']}，已有文档《{dup['doc'].get('title', '')}》",
                }

            doc_id = store._kb_repo.add_kb_document(
                title=doc.title,
                doc_type=doc.doc_type,
                source=doc.source,
                content=doc.content,
            )

            _rag = _api.get_rag_config()
            chunks = split_text(doc.content, max_len=_rag["chunk_size"], overlap=_rag["chunk_overlap"])
            store._kb_repo.add_kb_chunks(doc_id, chunks)

            config = _api._get_cfg()
            embed_failed = 0
            if config.embedding.enabled:
                embed_client = _api._get_embedding_client(config.embedding)
                all_chunks = store._kb_repo.list_kb_chunks(doc_id)
                for chunk in all_chunks:
                    emb = embed_client.embed_with_retry(chunk["content"])
                    if emb:
                        store._kb_repo.update_chunk_embedding(chunk["id"], emb)
                    else:
                        embed_failed += 1
                        # 标记 retry_pending 以便周期性重试
                        store._kb_repo.mark_chunk_retry_pending(chunk["id"])
            if embed_failed:
                logger.warning("[KB] 文档 %d 有 %d 个分块向量化失败（已跳过）", doc_id, embed_failed)

            # 当 embed_failed 比例超过 20% 时，标记入库失败（数据质量过低，不可静默）
            failed_ratio = embed_failed / max(len(chunks), 1)
            is_success = failed_ratio <= 0.2
            return {"success": is_success, "id": doc_id, "chunks": len(chunks),
                    "embed_failed": embed_failed,
                    "message": ("文档添加成功，已分 {} 个块".format(len(chunks))
                               + (f"，{embed_failed} 个块向量化失败" if embed_failed else ""))
                               + (f"，因向量化失败比例 {failed_ratio:.0%} 超过 20%，标记为失败" if not is_success else "")}
        return await run_sync(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/kb/documents/{doc_id}")
async def update_kb_document(doc_id: int, body: dict | None = None):
    """更新知识库文档（标题、内容等，用于清理干扰字符）。"""
    try:
        def _work():
            store = _api.get_store()
            doc = store._kb_repo.get_kb_document(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")
            if not body:
                raise HTTPException(status_code=400, detail="请求体不能为空")
            # kb_documents 表没有 content 列，内容存储在 kb_chunks 中
            allowed = {"title", "doc_type", "source"}
            updates = {k: v for k, v in body.items() if k in allowed}
            updated_keys = list(updates.keys())
            if updates:
                store._kb_repo.update_kb_document(doc_id, **updates)
            # 如果内容变了，需要重新分块和索引
            if "content" in body:
                from src.tools.utils import split_text
                old_chunks = store._kb_repo.list_kb_chunks(doc_id)
                for oc in old_chunks:
                    store._kb_repo.delete_kb_chunk(oc["id"])
                _rag = _api.get_rag_config()
                chunks = split_text(body["content"], max_len=_rag["chunk_size"], overlap=_rag["chunk_overlap"])
                store._kb_repo.add_kb_chunks(doc_id, chunks)
                config = _api._get_cfg()
                embed_failed = 0
                if config.embedding.enabled:
                    embed_client = _api._get_embedding_client(config.embedding)
                    all_chunks = store._kb_repo.list_kb_chunks(doc_id)
                    for chunk in all_chunks:
                        emb = embed_client.embed_with_retry(chunk["content"])
                        if emb:
                            store._kb_repo.update_chunk_embedding(chunk["id"], emb)
                        else:
                            embed_failed += 1
                if embed_failed:
                    logger.warning("[KB] 文档 %d 有 %d 个分块向量化失败（已跳过）", doc_id, embed_failed)
                updated_keys.append("content")
            if updated_keys:
                return {"success": True, "updated": updated_keys}
            raise HTTPException(status_code=400, detail="没有可更新的字段")
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/kb/documents/{doc_id}")
async def delete_kb_document(doc_id: int):
    try:
        def _work():
            store = _api.get_store()
            store._kb_repo.delete_kb_document(doc_id)
        await run_sync(_work)
        return {"success": True, "message": "文档删除成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/kb/import-url")
def import_kb_from_url(body: dict | None = None):
    """从 URL 导入网页内容到知识库。
    
    请求体：
    - url: 网页 URL
    - title: 可选，自定义标题（默认用网页 title）
    - doc_type: 可选，文档类型（默认 'web'）
    
    流程：
    1. 用 requests 获取网页 HTML
    2. 用 BeautifulSoup 提取标题和正文
    3. 调用 create_kb_document 存入知识库
    """
    if not body or 'url' not in body:
        raise HTTPException(status_code=400, detail="缺少 url 字段")
    
    url = body['url'].strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")

    # —— SSRF 防护：协议白名单 + 禁止访问内网/保留地址 ——
    if not is_ssrf_safe(url):
        raise HTTPException(status_code=400, detail="URL 非法或指向内网/保留地址，已拒绝")

    try:
        import requests
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin

        # 1. 获取网页内容（先尝试 requests，失败则用 Playwright 渲染 JS）
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        html_content = None
        use_playwright = False

        try:
            # verify=True 强制校验 TLS 证书；timeout=(connect, read)
            # 不自动跟随重定向：重定向目标可能指向内网（SSRF 绕过），需逐跳重校验。
            resp = ssrf_safe_get(url, headers=headers, timeout=(10, 30),
                                 stream=True)
            # 手动跟随重定向，每跳都重做 SSRF 校验（最多 5 跳）
            redirects = 0
            while resp.is_redirect and redirects < 5:
                loc = resp.headers.get("Location")
                resp.close()
                if not loc:
                    break
                next_url = urljoin(url, loc)
                if not is_ssrf_safe(next_url):
                    raise HTTPException(status_code=400,
                                        detail="重定向目标指向内网/保留地址，已拒绝")
                url = next_url
                resp = ssrf_safe_get(url, headers=headers, timeout=(10, 30),
                                    stream=True)
                redirects += 1
            if resp.status_code == 200:
                # 限制响应大小（最大 10MB），防止内存耗尽
                max_size = 10 * 1024 * 1024
                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > max_size:
                    resp.close()
                    raise HTTPException(status_code=400, detail="响应过大（超过 10MB 限制）")
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_size:
                        resp.close()
                        raise HTTPException(status_code=400, detail="响应过大（超过 10MB 限制）")
                raw_bytes = b''.join(chunks)
                encoding = resp.encoding or 'utf-8'
                html_content = raw_bytes.decode(encoding, errors='replace')
                # 检测是否需要 JS 渲染
                temp_soup = BeautifulSoup(html_content, 'html.parser')
                body_text = temp_soup.get_text(strip=True) if temp_soup.find('body') else ''
                # 如果 body 内容太少（<100字符），可能是 JS 渲染的页面
                if len(body_text) < 100:
                    use_playwright = True
            else:
                # 非 200（含最终仍是 3xx）走 JS 渲染兜底
                use_playwright = True
        except HTTPException:
            raise
        except Exception:
            use_playwright = True

        # 如果需要 JS 渲染，用 Playwright
        if use_playwright or html_content is None:
            try:
                from playwright.sync_api import sync_playwright

                def _abort_internal_route(route):
                    """拦截 Playwright 发出的每个请求，阻断对内网/保留地址的访问
                    （含页面内 JS 触发的重定向、子资源加载等）。"""
                    if not is_ssrf_safe(route.request.url):
                        return route.abort()
                    return route.continue_()

                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True, args=build_playwright_launch_args(url))
                    page = browser.new_page()
                    page.route("**/*", _abort_internal_route)
                    page.goto(url, timeout=30000, wait_until='domcontentloaded')
                    # 等待网络空闲
                    try:
                        page.wait_for_load_state('networkidle', timeout=10000)
                    except Exception:
                        pass  # 超时也没关系，继续
                    import time
                    time.sleep(2)  # 多等 2 秒让 JS 渲染
                    html_content = page.content()
                    browser.close()
            except Exception as pw_e:
                if html_content is None:
                    raise HTTPException(status_code=500, detail=f"获取网页失败（JS渲染也失败）：{str(pw_e)}")
                # Playwright 失败但 requests 有内容，继续使用 requests 的内容
        
        if not html_content:
            raise HTTPException(status_code=500, detail="无法获取网页内容")
        
        # 2. 解析 HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 提取标题
        page_title = body.get('title') or ''
        if not page_title:
            title_tag = soup.find('title')
            if title_tag:
                page_title = title_tag.get_text(strip=True)
        if not page_title:
            page_title = url.split('/')[-1] or url
        
        # 提取正文（简单策略：去掉 script/style，取 body 文本）
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        
        body_tag = soup.find('body')
        if body_tag:
            content = body_tag.get_text(separator='\n', strip=True)
        else:
            content = soup.get_text(separator='\n', strip=True)
        
        # 清理多余空行
        import re
        content = re.sub(r'\n{3,}', '\n\n', content).strip()
        
        if not content:
            raise HTTPException(status_code=500, detail="无法提取网页正文")
        
        # 3. 存入知识库（复用 create_kb_document 逻辑）
        from src.config import load_config
        from src.memory.store_factory import get_store
        from src.tools.utils import split_text
        
        store = get_store()
        # 查重
        dup = store._kb_repo.check_duplicate_document(title=page_title, content=content)
        if dup["duplicate"]:
            return {
                "success": False,
                "duplicate": True,
                "reason": dup["reason"],
                "existing_doc": dup["doc"],
                "message": f"文档重复：{dup['reason']}，已有文档《{dup['doc']['title']}》"
            }

        # 创建文档
        doc_type = body.get('doc_type', 'web')
        source = f'web:{url}'
        doc_id = store._kb_repo.add_kb_document(
            title=page_title,
            content=content,
            doc_type=doc_type,
            source=source,
        )

        # 分块
        _rag = _api.get_rag_config()
        chunks = split_text(content, max_len=_rag["chunk_size"], overlap=_rag["chunk_overlap"])
        store._kb_repo.add_kb_chunks(doc_id, chunks)

        # Embedding
        # 注：此处原写作未定义的 `_cfg`，embedding 启用时 URL 导入必抛 NameError；
        # 统一改用 _api._get_cfg()，与本文件其他路由一致。
        _cfg = _api._get_cfg()
        embed_failed = 0
        if _cfg.embedding.enabled:
            embed_client = _api._get_embedding_client(_cfg.embedding)
            all_chunks = store._kb_repo.list_kb_chunks(doc_id)
            for chunk in all_chunks:
                emb = embed_client.embed_with_retry(chunk["content"])
                if emb:
                    store._kb_repo.update_chunk_embedding(chunk["id"], emb)
                else:
                    embed_failed += 1
                    store._kb_repo.mark_chunk_retry_pending(chunk["id"])
        if embed_failed:
            logger.warning("[KB-URL] 文档 %d 有 %d 个分块向量化失败（已跳过）", doc_id, embed_failed)

        failed_ratio = embed_failed / max(len(chunks), 1)
        is_success = failed_ratio <= 0.2
        return {
            "success": is_success,
            "id": doc_id,
            "title": page_title,
            "chunks": len(chunks),
            "content_length": len(content),
            "embed_failed": embed_failed,
            "message": ("网页导入成功，已分 {} 个块".format(len(chunks))
                       + (f"，{embed_failed} 个块向量化失败" if embed_failed else "")
                       + (f"，因向量化失败比例 {failed_ratio:.0%} 超过 20%，标记为失败" if not is_success else ""))
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[URL导入] 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/api/kb/documents/{doc_id}/reindex")
async def reindex_kb_document(doc_id: int):
    try:
        def _work():
            config = _api._get_cfg()
            if not config.embedding.enabled:
                raise HTTPException(status_code=400, detail="Embedding 未启用")

            store = _api.get_store()
            doc = store._kb_repo.get_kb_document(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="文档不存在")

            embed_client = _api._get_embedding_client(config.embedding)
            chunks = store._kb_repo.list_kb_chunks(doc_id)
            indexed = 0
            for chunk in chunks:
                emb = embed_client.embed(chunk["content"])
                if emb:
                    store._kb_repo.update_chunk_embedding(chunk["id"], emb)
                    indexed += 1

            store._kb_repo.update_kb_document(doc_id, status="indexed")
            return {"success": True, "indexed": indexed,
                    "message": f"重建索引完成，共 {indexed} 个块"}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/kb/reindex")
async def reindex_all_kb():
    """重建所有文档的 Embedding 索引（不改变分块）。"""
    try:
        def _work():
            config = _api._get_cfg()
            if not config.embedding.enabled:
                raise HTTPException(status_code=400, detail="Embedding 未启用")

            from src.memory.store_factory import get_store
            store = get_store()
            docs, _ = store._kb_repo.list_kb_documents()

            embed_client = _api._get_embedding_client(config.embedding)

            total_indexed = 0
            for doc in docs:
                # 获取该文档的所有分块
                chunks = store._kb_repo.list_kb_chunks(doc["id"])

                # 重新生成 Embedding（带重试，覆盖冷启动/抖动导致的瞬时失败）
                for chunk in chunks:
                    emb = embed_client.embed_with_retry(chunk["content"])
                    if emb:
                        store._kb_repo.update_chunk_embedding(chunk["id"], emb)

                store._kb_repo.update_kb_document(doc["id"], status="indexed")
                total_indexed += len(chunks)

            return {
                "success": True,
                "docs": len(docs),
                "chunks": total_indexed,
                "message": f"重建索引完成：{len(docs)} 篇文档，共 {total_indexed} 个块"
            }
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[批量重建索引] 失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/kb/query")
async def kb_query(query: RagQuery):
    try:
        def _work():
            config = _api._get_cfg()
            if not config.embedding.enabled:
                raise HTTPException(status_code=400, detail="Embedding 未启用")

            embed_client = _api._get_embedding_client(config.embedding)

            query_embedding = embed_client.embed(query.query)
            if not query_embedding:
                raise HTTPException(status_code=500, detail="查询向量生成失败")

            store = _api.get_store()
            results = store._kb_repo.search_kb(
                query_embedding, top_k=query.top_k, min_similarity=query.min_similarity,
                query_text=query.query)

            return {
                "success": True,
                "query": query.query,
                "results": results,
            }
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/kb/chat")
async def kb_chat(query: RagChatQuery):
    try:
        def _work():
            config = _api._get_cfg()
            if not config.embedding.enabled:
                raise HTTPException(status_code=400, detail="Embedding 未启用")

            embed_client = _api._get_embedding_client(config.embedding)

            query_embedding = embed_client.embed(query.query)
            if not query_embedding:
                raise HTTPException(status_code=500, detail="查询向量生成失败")

            store = _api.get_store()
            results = store._kb_repo.search_kb(query_embedding, top_k=query.top_k)

            response = {
                "success": True,
                "query": query.query,
                "results": results,
                "context": "",
                "answer": "",
                "sources": [],
                "llm_status": "skipped",       # 4 态：skipped / unavailable / failed / success
                "llm_skip_reason": "",        # 当 unavailable / failed 时填充原因
            }

            if results:
                context_parts = []
                sources = []
                for i, r in enumerate(results):
                    context_parts.append(f"[{i+1}] {r['title']}: {r['content']}")
                    sources.append({
                        "title": r["title"],
                        "doc_type": r["doc_type"],
                        "source": r["source"],
                        "url": r.get("url", ""),
                        "similarity": r["similarity"],
                    })
                context = "\n\n".join(context_parts)
                response["context"] = context
                response["sources"] = sources

                if not query.use_llm:
                    # 用户明确未启用 LLM
                    response["llm_status"] = "skipped"
                elif not (config.llm.api_key and config.llm.api_key.strip()):
                    # 用户想用 LLM，但服务未配置 API Key
                    response["llm_status"] = "unavailable"
                    response["llm_skip_reason"] = "LLM 未配置 API Key（请在系统配置中填写 llm.api_key）"
                else:
                    try:
                        from src.llm.client import LLMClient
                        llm_client = LLMClient(config.llm)
                        messages = [
                            {"role": "system", "content": "你是一个知识助手，请根据提供的参考资料回答用户的问题。回答要简洁准确，并在末尾标注引用来源编号。"},
                            {"role": "user", "content": f"参考资料：\n{context}\n\n用户问题：{query.query}"},
                        ]
                        llm_resp = llm_client.chat(messages)
                        response["answer"] = llm_resp.content or ""
                        response["llm_status"] = "success" if response["answer"] else "failed"
                        if response["llm_status"] == "failed":
                            response["llm_skip_reason"] = "LLM 返回为空（可能上游限流或模型不可用）"
                    except Exception as e:
                        # 真实异常仅进服务端日志（含 traceback）；响应体不暴露内部错误，
                        # 避免 LLM 上游异常（可能含 API Key / 模型内部错误）泄露给客户端
                        logger.warning("[KB] LLM 问答调用失败: %s", e, exc_info=True)
                        response["llm_status"] = "failed"
                        response["llm_skip_reason"] = SAFE_OPERATION_FAILED
                        response["answer"] = "（知识库问答暂时不可用，请稍后重试）"

            return response
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/kb/stats")
async def kb_stats():
    try:
        def _work():
            store = _api.get_store()
            return store._kb_repo.kb_stats()
        return await run_sync(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---- /api/kb/parse-document（原 1132–1174，随主 kb 块抽出） ----

@router.post("/api/kb/parse-document")
async def parse_document(request: Request, file: UploadFile = File(...)):
    """解析上传的文档（支持 PDF、PPT、Word、图片），返回提取的文本内容。"""
    # 文件大小限制：50MB — 在读文件之前先检查 Content-Length，避免大文件撑爆内存
    MAX_UPLOAD_SIZE = 50 * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        return JSONResponse(
            status_code=413,
            content={
                "success": False,
                "error": f"文件过大（最大 50MB），请求体大小: {int(content_length) / 1024 / 1024:.1f}MB"
            },
        )
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        return JSONResponse(
            status_code=413,
            content={"success": False, "error": f"文件过大（最大 50MB），当前大小: {len(contents) / 1024 / 1024:.1f}MB"}
        )
    try:
        def _work():
            from src.tools.parse_document import DocumentParser
            suffix = Path(file.filename).suffix.lower()

            # 文件类型白名单
            ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx',
                                  '.xls', '.xlsx', '.txt', '.md', '.png', '.jpg', '.jpeg'}
            if suffix not in ALLOWED_EXTENSIONS:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": f"不支持的文件类型: {suffix}，支持的格式: {', '.join(sorted(ALLOWED_EXTENSIONS))}"})

            # 保存为临时文件
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(contents)
                tmp_path = tmp.name

            try:
                # 调用统一解析器
                parser = DocumentParser(_api.get_app_instance().config if _api.get_app_instance() else None)
                text = parser.parse(tmp_path)

                if not text.strip():
                    return {
                        "success": False,
                        "error": f"文档内容为空或无法提取文本（文件类型: {suffix}）"
                    }

                return {
                    "success": True,
                    "text": text,
                    "length": len(text),
                    "file_type": suffix,
                }
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        return await run_sync(_work)

    except Exception as e:
        logger.error("文档解析失败: %s", e, exc_info=True)
        return {"success": False, "error": SAFE_OPERATION_FAILED}


@router.post("/api/kb/import-from-feishu")
async def import_from_feishu(body: dict | None = None):
    """从飞书文档导入内容到当前平台知识库。

    请求体：
    - doc_token: 飞书文档 token（必填）
    - title: 可选，自定义标题（默认用飞书文档标题）
    - doc_type: 可选，文档类型（默认 'feishu'）
    - folder_token: 可选，批量导入时列出某文件夹下所有文档

    流程：
    1. 通过飞书适配器读取飞书文档内容
    2. 调用 create_kb_document 逻辑存入知识库

    仅飞书平台可用；其他平台调用返回 400。
    """
    from web.dependencies import get_current_platform

    platform = get_current_platform()
    if platform != "feishu":
        raise HTTPException(
            status_code=400,
            detail="从飞书文档导入仅在飞书平台可用，当前平台: {}".format(platform),
        )

    if not body:
        raise HTTPException(status_code=400, detail="请求体不能为空")

    doc_token = body.get("doc_token", "").strip()
    folder_token = body.get("folder_token", "").strip()

    if not doc_token and not folder_token:
        raise HTTPException(status_code=400, detail="doc_token 或 folder_token 至少提供一个")

    try:
        def _work():
            store = _api.get_store()
            config = _api._get_cfg()
            rag_config = _api.get_rag_config()

            if folder_token:
                return import_feishu_folder(
                    store,
                    folder_token,
                    doc_type=body.get("doc_type", "feishu"),
                    rag_config=rag_config,
                    config=config,
                )

            title = body.get("title", "").strip()
            doc_type = body.get("doc_type", "feishu")
            entity_type = body.get("entity_type", "").strip()

            return import_single_feishu_doc(
                store,
                doc_token,
                title=title,
                doc_type=doc_type,
                entity_type=entity_type,
                rag_config=rag_config,
                config=config,
            )
        return await run_sync(_work)

    except FeishuImportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error("飞书文档导入失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/kb/feishu-docs")
async def search_feishu_docs(query: str = "", page_size: int = 10):
    """搜索飞书文档（需要当前平台为 feishu）。

    调用飞书适配器的 doc_search，返回可导入的文档列表预览。
    """
    from web.dependencies import get_current_platform

    platform = get_current_platform()
    if platform != "feishu":
        raise HTTPException(
            status_code=400,
            detail="飞书文档搜索仅在飞书平台可用，当前平台: {}".format(platform),
        )

    if not query:
        return {"documents": [], "message": "请输入搜索关键词"}

    try:
        docs = await run_sync(_feishu_doc_search, query, page_size=page_size)
        return {"documents": docs}
    except FeishuImportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.error("飞书文档搜索失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
