from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable

from src.dws_adapter import DwsAdapter, DwsNonRetryableError, DwsError
from src.memory.sqlite_store import SQLiteStore
from src.tools.utils import cross_process_lock, split_text

logger = logging.getLogger(__name__)


class DocSyncScheduler:
    """钉钉文档定时自动同步器。

    - 定期轮询已标记为"自动同步"的钉钉文档
    - 检测内容变化（通过 last_modified 或内容哈希）
    - 内容变化时自动重新导入知识库
    """

    def __init__(self, dws: DwsAdapter, db_path: str,
                 sync_interval_seconds: int = 3600,
                 embedding_client=None,
                 on_sync: Callable[[dict], None] | None = None,
                 config: Any = None):
        self.dws = dws
        self.db_path = db_path
        self.sync_interval = sync_interval_seconds
        self.embedding_client = embedding_client
        self.on_sync = on_sync
        self.config = config  # 应用配置（可选）；用于读取 rag.chunk_hard_max 安全天花板

        self._running = False
        self._thread: threading.Thread | None = None
        # 进程内串行化：避免调度线程与手动触发并发执行 _run_sync。
        self._sync_lock = threading.Lock()

    def _store(self) -> SQLiteStore:
        """在后台线程中创建独立的 SQLiteStore，避免跨线程连接问题。"""
        from src.memory.store_factory import get_store
        return get_store(self.db_path)

    def _get_doc_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]

    def _sync_single_doc(self, doc_id: str) -> dict:
        """同步单个文档，返回同步结果。"""
        result = {"doc_id": doc_id, "changed": False, "error": None}
        store = self._store()

        try:
            # 获取远程文档内容
            remote = self.dws.doc_read(doc_id, content_format="markdown")
            remote_content = remote.get("content") or remote.get("markdown", "")
            remote_modified = remote.get("lastModified", "") or remote.get("modified_at", "")

            if not remote_content:
                result["error"] = "文档内容为空"
                return result

            # 获取本地文档信息
            local = store._docs_repo.get_dingtalk_doc(doc_id)
            if local:
                local_content = local.get("content", "")
                local_modified = local.get("last_modified", "")

                # 判断是否需要更新
                content_changed = self._get_doc_hash(remote_content) != self._get_doc_hash(local_content)
                time_changed = remote_modified != local_modified

                if not content_changed and not time_changed:
                    logger.debug("文档 %s 未变更，跳过同步", doc_id)
                    return result

                # 内容哈希是真相源：仅当内容真正变化时才重做知识库索引
                # （重新分块 + 逐块调用 embedding，烧 API 配额）。
                # 若内容未变但钉钉 lastModified 时间戳格式微变（带/不带毫秒、
                # 时区、空格分隔等），只是对齐本地缓存的 last_modified 即可，
                # 避免每次轮询都无谓重嵌入。
                if not content_changed and time_changed:
                    logger.info("文档 %s 内容未变、仅时间戳变更，对齐本地缓存后跳过重嵌入", doc_id)
                    store._docs_repo.upsert_dingtalk_doc(
                        doc_id=doc_id,
                        title=local.get("title", ""),
                        doc_type=local.get("doc_type", ""),
                        url=local.get("url", ""),
                        content=local_content,
                        last_modified=remote_modified,
                    )
                    return result

                logger.info("文档 %s 已变更（内容=%s，时间=%s），正在同步...",
                            doc_id, content_changed, time_changed)

            # 更新钉钉文档缓存
            title = remote.get("title", "") or (local.get("title", "") if local else "")
            store._docs_repo.upsert_dingtalk_doc(
                doc_id=doc_id,
                title=title,
                doc_type=remote.get("doc_type", ""),
                url=remote.get("url", ""),
                content=remote_content,
                last_modified=remote_modified,
            )

            # 检查知识库中是否已有此文档
            cur = store.conn.cursor()
            cur.execute("SELECT id FROM kb_documents WHERE source_id = ?", (doc_id,))
            row = cur.fetchone()
            old_kb_doc_id = row["id"] if row else None

            # 【H11修复】先加后删：先创建新的知识库文档，成功后再删除旧的，
            # 避免先删后加时新文档创建失败导致数据丢失。
            # 重新添加到知识库
            kb_doc_id = store._kb_repo.add_kb_document(
                title=title,
                doc_type="dingtalk",
                source="dingtalk",
                source_id=doc_id,
                url=remote.get("url", ""),
                content=remote_content,
            )
            chunks = split_text(
                remote_content,
                hard_max=(
                    getattr(getattr(self.config, "rag", None), "chunk_hard_max", None)
                    if self.config is not None else None
                ),
            )
            store._kb_repo.add_kb_chunks(kb_doc_id, chunks)

            # 新文档创建成功后，再删除旧文档
            if old_kb_doc_id is not None:
                store._kb_repo.delete_kb_document(old_kb_doc_id)
                logger.info("已移除 %s 的旧知识库文档 %d", doc_id, old_kb_doc_id)

            # 生成 embedding（带重试，覆盖冷启动/抖动导致的瞬时失败）
            if self.embedding_client and self.embedding_client.is_enabled:
                all_chunks = store._kb_repo.list_kb_chunks(kb_doc_id)
                failed = 0
                for chunk in all_chunks:
                    emb = self.embedding_client.embed_with_retry(chunk["content"])
                    if emb:
                        store._kb_repo.update_chunk_embedding(chunk["id"], emb)
                    else:
                        failed += 1
                if failed:
                    # 【修复#4】空向量 chunk 不会被检索，但文档 status 仍为 indexed，
                    # 此处暴露真实失败数，避免“已索引”假象掩盖 RAG 质量下降。
                    logger.warning("文档 %s 同步完成，但 %d/%d 个 chunk 向量化失败（embedding 为空，将不被检索）",
                                   doc_id, failed, len(all_chunks))

            result["changed"] = True
            result["kb_doc_id"] = kb_doc_id
            result["chunks"] = len(chunks)
            logger.info("文档 %s 同步完成，知识库文档ID=%d，分块数=%d",
                        doc_id, kb_doc_id, len(chunks))

            if self.on_sync:
                self.on_sync(result)

        except (DwsNonRetryableError, DwsError) as e:
            # 检查是否是"当前节点不是钉钉在线文档"错误
            if "当前节点不是钉钉在线文档" in str(e):
                result["error"] = "节点不是钉钉在线文档，已禁用自动同步"
                logger.warning("文档 %s 不是钉钉在线文档，关闭自动同步: %s", doc_id, e)
                # 禁用该文档的自动同步
                try:
                    store._docs_repo.set_doc_auto_sync(doc_id, False)
                    logger.info("已关闭文档 %s 的自动同步", doc_id)
                except Exception as db_err:
                    logger.error("关闭文档 %s 的自动同步失败: %s", doc_id, db_err)
            else:
                result["error"] = str(e)
                logger.error("同步文档 %s 失败（不可重试）: %s", doc_id, e)
        except Exception as e:
            result["error"] = str(e)
            logger.error("同步文档 %s 失败: %s", doc_id, e, exc_info=True)
        return result

    def _run_sync(self) -> list[dict]:
        """执行一次同步轮询。"""
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("文档同步正在进行（同进程内并发调用），跳过本次")
            return []
        try:
            with cross_process_lock("doc-sync", os.path.dirname(self.db_path)) as acquired:
                if not acquired:
                    logger.warning("另一进程正在同步文档，跳过本次（避免重复同步）")
                    return []
                return self._run_sync_inner()
        finally:
            self._sync_lock.release()

    def _run_sync_inner(self) -> list[dict]:
        """实际同步逻辑（已被进程内/跨进程锁保护）。"""
        store = self._store()
        # 获取所有标记为自动同步的钉钉文档
        cur = store.conn.cursor()
        cur.execute("""
            SELECT doc_id FROM dingtalk_docs
            WHERE auto_sync = 1
        """)
        docs = [row["doc_id"] for row in cur.fetchall()]

        if not docs:
            logger.debug("未配置自动同步文档")
            return []

        logger.info("正在同步 %d 个自动同步文档...", len(docs))
        results = []
        for doc_id in docs:
            result = self._sync_single_doc(doc_id)
            results.append(result)
            time.sleep(0.5)  # 避免请求过快

        changed = [r for r in results if r["changed"]]
        errors = [r for r in results if r["error"]]
        logger.info("同步完成: %d 个已变更，%d 个错误", len(changed), len(errors))
        return results

    def _run_loop(self) -> None:
        """后台同步循环。"""
        # 周期统一取整；<=0 视为禁用调度（与 config 注释「0/负数禁用」一致），
        # 避免 range(0) 空转忙循环，或浮点配置触发 range() TypeError 致线程静默退出。
        interval = int(round(self.sync_interval))
        logger.info("文档同步调度器已启动，间隔=%d秒", interval)
        if interval <= 0:
            logger.warning("文档同步间隔<=0，已禁用调度（sync_interval=%r）", self.sync_interval)
            return

        while self._running:
            try:
                self._run_sync()
            except Exception as e:
                logger.error("同步循环错误: %s", e, exc_info=True)

            # 等待下一个周期（1s 粒度，使 stop() 最多延迟 1s 即可退出）
            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("文档同步调度器已停止")

    def start(self) -> None:
        """启动后台同步线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止后台同步线程。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def sync_now(self, doc_id: str | None = None) -> dict | list[dict]:
        """立即同步（可指定单个文档）。"""
        if doc_id:
            return self._sync_single_doc(doc_id)
        return self._run_sync()
