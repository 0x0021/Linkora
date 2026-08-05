from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable

from src.config import PollerConfig
from src.dws_adapter import DwsAdapter, DwsPermissionError
from src.memory.sqlite_store import SQLiteStore
from src.models import Message
from src.poller_core_ocr import OcrMixin
from src.poller_core_parse import ParseMixin
from src.poller_core_dedup import DedupMixin
from src.poller_core_access import AccessControlMixin
from src.poller_core_dispatch import DispatchMixin
from src.poller_core_history import HistorySyncMixin
from src.poller_core_discovery import DiscoveryMixin
from src.poller_strategy import PollerStrategyMixin

logger = logging.getLogger(__name__)

# 各平台底层 CLI 工具名（仅用于日志措辞，避免非钉钉平台也显示"DWS"）。

class MessagePoller(PollerStrategyMixin, AccessControlMixin, OcrMixin, ParseMixin, DedupMixin, DispatchMixin, HistorySyncMixin, DiscoveryMixin):
    def __init__(self, config: PollerConfig, dws: DwsAdapter,
                 store: SQLiteStore, current_user_id: str,
                 current_user_name: str,
                 current_user_user_id: str = "",
                 rule_engine=None, platform_id: str = "",
                 load_processed_ids: bool = True, skip_ocr: bool = False):
        self.config = config
        self.dws = dws
        self.store = store
        self.current_user_id = current_user_id
        self.current_user_name = current_user_name
        self.current_user_user_id = current_user_user_id
        self._rule_engine = rule_engine
        self.platform_id = platform_id
        # 历史回填用的独立子进程：跳过图片 OCR / 卡片图下载（仅存文本），并跳过启动期
        # 全量已处理 ID 预载（sync_history 的去重走 DB is_message_processed，不依赖内存集合），
        # 让「全部历史」这类重同步明显变快。
        self._skip_ocr = skip_ocr

        # 尽早设置 src 端平台上下文：init 阶段的 load_blocked_conversations / 飞书
        # _sync_feishu_external_contacts 等都会调 conv_conn(get_current_platform())，
        # 否则 conv_conn 拿到空 platform、回退到空账号命名空间并刷 WARNING。
        # 不显式 reset：__init__ 与后续 run_loop 在不同线程，run_loop 自己会重新设。
        if platform_id:
            from src.memory.platform_context import set_current_platform
            set_current_platform(platform_id)
        self._running = False
        self._last_poll_time: dict[str, datetime] = {}
        # 跨轮次消息去重：记录已经 handle_message 处理过的 msg_id
        # 使用 OrderedDict 实现 LRU 容量淘汰（TTL 由 sqlite_store 的 cleanup_processed_msgs 负责）
        self._processed_msg_ids: OrderedDict[str, bool] = OrderedDict()
        # 无权限访问的会话（避免本次运行内反复重试，重启后自动恢复）
        self._inaccessible_conversations: set[str] = set()
        # 群聊权限错误连续失败计数（用于「连续 N 次才拉黑」容错，避免瞬时抖动误杀活跃群）
        self._perm_fail_streak: dict[str, int] = {}
        # 接口权限警告状态（避免每次轮询都打警告日志）
        self._perm_warned: set[str] = set()
        # 会话元数据接口（chat_conversation_info）不可达的会话（内存集合）。
        # 外部好友/跨组织单聊调该接口会稳定返回权限错误，但消息通道（list-all /
        # list-direct 用消息里的 sender openDingTalkId）仍然正常。记入此集合仅用于
        # 避免每轮重复调用该接口，绝不等同于"不可达"——不进持久化黑名单，好友不受影响。
        self._metadata_unavailable: set[str] = set()
        # 周期对账计数器：每 N 轮用 list-top（安全、不弹窗）+ 直接探测对账一次黑名单，
        # 自动解除已恢复访问的会话（离职后又回来 / 退群又被拉回等状态变化）
        self._poll_count = 0
        # 可观测性指标（供 /api/poller-status 读取）
        self._last_poll_at: datetime | None = None       # 最近一次轮询开始时间
        self._last_error: str | None = None              # 最近一次轮询异常信息
        self._last_error_at: datetime | None = None      # 最近一次异常时间
        self._queue_depth: int = 0                        # 本轮拉取到的待处理消息数
        # P1-E 背压监控指标
        self._dispatch_total = 0          # 累计派发条数
        self._deferred_total = 0          # 累计因限速延迟到下轮的条数
        self._last_cycle_dispatched = 0   # 上一轮实际派发条数
        self._last_cycle_deferred = 0     # 上一轮被延迟条数
        self._first_poll = True           # 首次轮询（重启后冷启动）标记
        self._reconcile_every = getattr(self.config, "blacklist_reconcile_every", 10) or 10
        self._reconcile_probe_idx = 0  # 黑名单探测轮转游标（分批，避免一次性打爆）
        # 置顶/最近会话列表缓存（极少变化，无需每轮打 DWS）
        self._top_convs_cache: list = []
        self._top_convs_cache_ts: float = 0.0
        # 长尾会话按会话限频抓取的时间戳（openConversationId -> 上次抓取 epoch）
        self._last_fetch_time: dict[str, float] = {}

        # 持久化黑名单：启动时从 DB 加载，避免重启后对已离职/非好友/被踢群等
        # 无权限会话反复触发 dws 的 OAuth 弹窗（这是「反复弹」的根因）
        try:
            for b in self.store._blacklist_repo.load_blocked_conversations():
                self._inaccessible_conversations.add(b["chat_id"])
            if self._inaccessible_conversations:
                logger.info("[轮询器] 已从数据库加载 %d 个不遍历黑名单会话",
                            len(self._inaccessible_conversations))
        except Exception as e:
            logger.warning("[轮询器] 加载黑名单失败: %s", e)
        # 启动时对账：解除已恢复访问的会话（自愈）
        try:
            self._reconcile_blocklist()
        except Exception as e:
            logger.warning("[轮询器] 启动时黑名单对账失败（不影响运行）: %s", e)
        # list-all 主通道空轮探针计数器：连续多少轮 list-all 一条新消息都没拉到
        self._list_all_empty_streak: int = 0
        # list-all 主通道上次查询起点时间（首次运行时用配置窗口，之后用最新消息时间戳）
        self._last_list_all_time: datetime | None = None
        # 飞书全量扫描（完整 +chat-list 翻页发现新会话）上次执行时间；
        # 每轮主通道默认走白名单模式，仅在间隔到期时做一次全量扫描。
        self._last_full_scan_time: datetime | None = None
        # 注意：不再设置「全局权限退避」。组织级权限错误（TOKEN_VERIFIED_FAILED 等）
        # 只会导致单个操作失败，按 key 去重后仅警告一次，绝不阻断整轮轮询——
        # 否则会让机器人完全停止收发消息。重登无效的组织配置问题由 AuthMonitor
        # 以 org_not_configured 单独处理，不会触发登录弹窗。
        # 启动时从 DB 加载最近 24h 已处理消息 ID（避免重启后重复处理）。
        # 历史回填子进程传 load_processed_ids=False 跳过：sync_history 去重走 DB 查询，
        # 不依赖此内存集合，预载只是 live 轮询的冷启动优化，对一次性同步纯属开销。
        if load_processed_ids:
            try:
                db_processed = self.store._message_repo.load_recent_processed_msg_ids(hours=168)
                for msg_id in db_processed:
                    self._processed_msg_ids[msg_id] = True
                if db_processed:
                    logger.info("[轮询器] 已从数据库加载 %d 个已处理消息 ID", len(db_processed))
            except Exception as e:
                logger.warning("[轮询器] 从数据库加载已处理消息 ID 失败: %s", e)

        # 目标组织：优先用配置值；配置为空则自动使用当前登录组织。
        # 多组织环境下，非目标组织的会话会在下方全局权限错误分支被持久化跳过，
        # 不再每轮重试，从而避免反复触发跨组织权限验证/弹窗。
        self.target_org_corp_id = self.config.target_org_corp_id or ""
        try:
            cur = self.dws.get_current_org()
            self.current_org = cur
            effective = self.target_org_corp_id or cur.get("corp_id", "")
            if effective:
                logger.info(
                    "[轮询器] 目标组织: %s (corpId=%s)%s",
                    cur.get("corp_name", "?"), effective,
                    " [配置指定]" if self.target_org_corp_id else " [自动=当前登录组织]",
                )
        except Exception as e:
            logger.warning("[轮询器] 解析目标组织失败（不影响运行）: %s", e)
            self.current_org = {"corp_id": "", "corp_name": ""}

        # 图片处理线程池：避免下载/OCR 阻塞主轮询
        self._image_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="image-proc",
        )
        # OCR 进行中的消息 ID -> Future（用于等待 OCR 完成）
        self._ocr_futures: dict[str, Future] = {}
        # OCR 完成结果持久缓存（future 完成后结果保留在此，wait_for_ocr 总能取到）
        self._ocr_results: dict[str, str] = {}
        # OCR 完成图片相对路径缓存（供消息记录页缩略图使用）
        self._ocr_image_paths: dict[str, str] = {}
        self._ocr_lock = threading.Lock()
        # OCR 引擎实例缓存：RapidOCR 初始化需加载模型文件（数秒），
        # 每次图片都重建会带来明显延迟。多线程下用双重检查锁保证只创建一次。
        self._doc_parser = None
        self._ocr_cache_lock = threading.Lock()
        # OCR 调用串行锁：DocumentParser 底层 RapidOCR 引擎非线程安全（共享模型状态），
        # 多线程（_image_executor 线程池）并发调用会偶发崩溃/结果串扰。串行化 OCR 调用
        # 而非每线程重建实例（重建需重载模型数秒），兼顾正确性与吞吐。
        self._ocr_call_lock = threading.Lock()

        # 启动时重新分类现有会话（修复历史数据的 chat_type）
        self._reclassify_existing_conversations(self.platform_id)

        # 飞书：启动时自动发现并注册外部联系人
        self._sync_feishu_external_contacts()

    # ------------------------------------------------------------------
    # 消息派发（快通道 + 周期末统一派发共用）
    # ------------------------------------------------------------------

    def _dispatch_one(self, msg: "Message", handler: Callable[["Message"], None]) -> None:
        """向 handler 派发单条消息，并标记已处理 + 落库（与 run_loop 周期末逻辑一致）。

        抽到独立方法供两处复用：
        1) poll_once 内的 list-all 快通道（抓到即派发，先于会阻塞的 per-conversation 抓取）；
        2) run_loop 周期末对 per-conversation 消息的统一派发。

        语义还原原 run_loop 的 finally：
        - 标记已处理（_mark_msg_processed）仅在 handler 成功后执行，保证去重、避免下一轮重复拉取；
        - 落库（save_message）**无条件**执行——即使 handler 抛错也保留消息原文，
          与原行为一致（handler 只把消息送进防抖队列、立即返回，失败不代表消息丢弃）。
        """
        handler_ok = False
        try:
            handler(msg)
            handler_ok = True
        except Exception as e:
            logger.error("处理 %s 出错：%s", msg.msg_id, e, exc_info=True)
        finally:
            if handler_ok:
                if msg.raw.get("merged"):
                    if msg.msg_id:
                        self._mark_msg_processed(msg.msg_id, msg.chat_id, msg=msg)
                else:
                    if msg.msg_id:
                        self._mark_msg_processed(msg.msg_id, msg.chat_id, msg=msg)
            # 落库无条件执行（与原 run_loop 一致）：handler 失败也保留消息原文
            self.store._message_repo.save_message(msg, "user")

    # ------------------------------------------------------------------
    # 飞书外部联系人自动发现
    # ------------------------------------------------------------------


    def run_loop(self, handler: Callable[[Message], None]) -> None:
        self._running = True
        logger.debug("轮询，间隔 %d 秒", self.config.interval_seconds)
        while self._running:
            try:
                from src.memory.platform_context import set_current_platform, reset_current_platform
                _plat_tok = set_current_platform(self.platform_id)
                try:
                    # 【延迟修复】把 handler 透传给 poll_once：list-all 发现的消息会在
                    # per-conversation 同步抓取（可能挂死的 dws 调用）之前，经 handler
                    # 即时派发（快通道），避免整条派发链被阻塞的 dws 调用冻结。
                    messages = self.poll_once(handler=handler)
                    # P1-E 背压：最旧优先 + 单轮限速，避免重启/突发一次性派发打爆接口
                    is_cold_start = self._first_poll
                    self._queue_depth = len(messages)
                    to_dispatch = self._dispatch_messages(messages, is_cold_start=is_cold_start)
                    self._first_poll = False
                    dispatched_n = len(to_dispatch)
                    deferred_n = len(messages) - dispatched_n
                    self._dispatch_total += dispatched_n
                    self._deferred_total += deferred_n
                    self._last_cycle_dispatched = dispatched_n
                    self._last_cycle_deferred = deferred_n

                    for msg in to_dispatch:
                        # 复用 _dispatch_one（与 list-all 快通道同一逻辑）：派发 + 标记已处理 + 落库
                        self._dispatch_one(msg, handler)
                finally:
                    reset_current_platform(_plat_tok)

            # 标已读已移至 main._send_reply 中「即将发送回复」的时刻执行，
            # 不再在轮询器 handler 结束后立即标记，避免对方看到已读却迟迟收不到回复。

                        
            except KeyboardInterrupt:
                logger.debug("用户中断轮询")
                break
            except Exception as e:
                self._last_error = str(e)[:500]
                self._last_error_at = datetime.now()
                logger.error("轮询出错：%s", e, exc_info=True)

            # 用 1s 粒度轮询等待，使 stop() 最多延迟 1s 即可退出，而非阻塞整个 interval。
            # 周期统一取整并下限定为 1s，避免 interval_seconds<=0 时 tight-loop 忙轮询
            # （配置为 0 不应使 CPU 占满）；浮点配置（如 30.0）也能安全用于比较。
            interval_seconds = max(1, int(round(self.config.interval_seconds or 0)))
            elapsed = 0
            while self._running and elapsed < interval_seconds:
                time.sleep(1)
                elapsed += 1







    def stop(self) -> None:
        self._running = False
        # 关闭线程池，避免线程泄漏（重启/热重载时累积）
        try:
            self._image_executor.shutdown(wait=True)
        except Exception as e:
            logger.warning("[轮询器] 关闭线程池时异常: %s", e)
