"""工作时间外未回复消息 → 待办的编排服务。

对外入口 :meth:`OffhoursTodoService.scan_all` 一次完成：扫描 → 去重 → 建待办 →
双边通知 → 状态流转。后台调度器和 Web 手动触发共用同一入口，靠 ``dedupe_key``
与记录表唯一约束保证「重复触发不重复建单」。

平台能力差异：
- 钉钉：``dws todo task create`` 建个人待办；
- 飞书：``lark-cli task +create`` 建个人任务（适配器已补能力）；
- 企微：平台无个人待办 API，降级为「通知型待办」——只给对方发提醒消息，
  记录 ``todo_type='notify_only'``，Web 明确标注，不伪装成真待办。
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from src.config_models.system import UnrepliedTodoConfig
from src.im_adapter.errors import IMAdapterError, IMAdapterUnsupportedTypeError
from src.offhours_todo.scanner import (
    ScanResult,
    UnansweredItem,
    _recheck_conversation_replies,
    collect_platform_self_ids,
    scan_platform,
)
from src.offhours_todo.worktime import classify, parse_message_ts, resolve_tz

if TYPE_CHECKING:
    from src.platform.base import PlatformContext

logger = logging.getLogger(__name__)

_CLI_ERRORS = (
    IMAdapterError, OSError, ValueError, TypeError, KeyError,
    RuntimeError, subprocess.SubprocessError,
)


class OffhoursTodoService:
    """扫描与建单编排器（线程安全：单次扫描持锁，防 web/worker 双跑）。"""

    def __init__(self, cfg: UnrepliedTodoConfig, engine: Any, platforms: dict[str, "PlatformContext"]) -> None:
        self.cfg = cfg
        self.engine = engine
        self.platforms = platforms
        self._lock = threading.Lock()
        self._last_result: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def scan_all(
        self,
        *,
        platform: str = "",
        create_todos: bool | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """扫描平台并处理未回复消息（Linkora 已按平台隔离，跟随当前平台）。

        Args:
            platform: 当前平台 ID（由 Web 请求上下文 / 调度器逐平台传入）；空串时
                扫描全部已配置平台，仅后台调度器使用。
            create_todos: None 时跟随 ``cfg.auto_create``；True/False 强制覆盖，
                供 Web「手动建单」使用。
            dry_run: True 时只扫描与统计，不建单、不发通知、不写记录。

        Returns:
            汇总字典（平台数 / 扫描数 / 新增待处理 / 建单数 / 失败数 / 各平台明细）。
        """
        if not self._lock.acquire(blocking=False):
            logger.debug("[未回复待办] 上一轮扫描仍在进行，跳过本轮")
            return {"skipped": True, "reason": "上一轮扫描尚未结束"}

        summary: dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "platforms": [],
            "scanned": 0,
            "offhours": 0,
            "pending_new": 0,
            "replied_backfilled": 0,
            "todos_created": 0,
            "notify_only": 0,
            "failed": 0,
            "dry_run": dry_run,
        }
        try:
            targets = [
                (pid, ctx) for pid, ctx in self.platforms.items()
                if (not platform or pid == platform) and ctx.store is not None
            ]
            if platform and not targets:
                summary["error"] = f"平台 {platform} 不可用或未启用"
                return summary

            should_create = self.cfg.auto_create if create_todos is None else bool(create_todos)
            all_notices: list[str] = []
            created_keys: list[str] = []

            from src.memory.platform_context import with_platform

            for pid, ctx in targets:
                # 平台 ContextVar 隔离：repo 的账号分库路由 / 写入闸门按当前平台解析
                with with_platform(pid):
                    ctx_result = self._scan_one(
                        pid, ctx, create_todos=should_create, dry_run=dry_run,
                        notices=all_notices, created_keys=created_keys,
                    )
                summary["platforms"].append(ctx_result)
                for key in ("scanned", "offhours", "pending_new", "replied_backfilled",
                            "todos_created", "notify_only", "failed"):
                    summary[key] += ctx_result.get(key, 0)

            # 本人汇总提醒（仅实际建单后发一次，避免逐条轰炸）
            if all_notices and self.cfg.notify_owner and not dry_run:
                if self._notify_owner(all_notices):
                    # 汇总已送达 → 回填这批记录的「已通知本人」标记（否则 Web 上
                    # 永远显示未通知，与事实不符）
                    for pid, ctx in targets:
                        if ctx.store is None:
                            continue
                        try:
                            with with_platform(pid):
                                ctx.store._offhours_todo_repo.mark_owner_notified(created_keys)
                        except (sqlite3.Error, ValueError, AttributeError) as e:
                            logger.debug("[未回复待办] 回填本人通知标记失败 platform=%s: %s", pid, e)
            summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._last_result = summary
            return summary
        finally:
            self._lock.release()

    def _scan_one(
        self,
        platform: str,
        ctx: "PlatformContext",
        *,
        create_todos: bool,
        dry_run: bool,
        notices: list[str],
        created_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        self_ids = collect_platform_self_ids(self.engine, ctx)
        result: ScanResult = scan_platform(
            self.cfg, platform, ctx, self_ids=self_ids,
        )
        out: dict[str, Any] = {
            "platform": platform,
            "scanned": result.scanned,
            "offhours": result.offhours,
            "pending_new": result.pending_new,
            "replied_backfilled": result.replied_backfilled,
            "todos_created": 0,
            "notify_only": 0,
            "failed": 0,
            "errors": result.errors,
        }
        if dry_run or not create_todos or not result.items:
            return out

        # 按会话聚合后逐条建单（scanner 已按 chat_id 分组落库，这里按 limit 截断）
        items = sorted(result.items, key=lambda x: x.timestamp)[: self.cfg.max_per_scan]
        for item in items:
            ok, kind, err = self._create_for_item(item, ctx, self_ids)
            if ok:
                if kind == "notify_only":
                    out["notify_only"] += 1
                else:
                    out["todos_created"] += 1
                notices.append(
                    f"[{platform}] {item.sender_name or item.sender_id or '未知'}："
                    f"{item.content_preview[:40]}"
                )
                if created_keys is not None and kind != "duplicate":
                    created_keys.append(item.dedupe_key)
            else:
                out["failed"] += 1
                logger.warning("[未回复待办] 平台 %s 建单失败 key=%s: %s", platform, item.dedupe_key, err)
        return out

    # ------------------------------------------------------------------
    # 建单 / 通知
    # ------------------------------------------------------------------

    def _build_title(self, item: UnansweredItem) -> str:
        who = item.sender_name or item.sender_id or "未知联系人"
        ts = ""
        try:
            ts = datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
        except ValueError:
            ts = item.timestamp[:16]
        suffix = f"（{ts}）" if ts else ""
        # 钉钉待办无 description 字段，把内容摘要塞进标题，保证待办本身自带上下文
        preview = (item.content_preview or "").strip().replace("\n", " ")[:30]
        return f"待回复 · {who}{suffix}{' · ' + preview if preview else ''}"

    def _resolve_executor(self, platform: str, self_ids: frozenset[str] | set[str]) -> str:
        """解析待办执行人。

        钉钉 ``todo task create --executors`` 只认**纯数字 userId**，传 openDingTalkId
        （``DDn1...``）会被 CLI 静默忽略 → 待办创建成功但没有执行人，不出现在「待我处理」。
        优先级：配置的 owner_user_id（数字）→ 引擎的 current_user_id（数字）→ self_ids 里
        第一个纯数字 ID；非数字 ID 只作最后兜底（飞书 open_id 可用，钉钉不行）。
        """
        configured = str(getattr(self.cfg, "owner_user_id", "") or "").strip()
        if configured:
            return configured
        engine_uid = str(getattr(self.engine, "current_user_id", "") or "").strip()
        if engine_uid and engine_uid.isdigit():
            return engine_uid
        for sid in self_ids:
            if str(sid).isdigit():
                return str(sid)
        # 飞书的 open_id 不是数字但可作执行人；钉钉若有非数字兜底也会记下，
        # 但 true fix 是配置正确的数字 userId。
        next_non_digit = next((str(s) for s in self_ids if str(s)), "")
        if next_non_digit and platform != "dingtalk":
            return next_non_digit
        if next_non_digit:
            logger.warning(
                "[未回复待办] 钉钉执行人未配置数字 userId（当前 %s），待办可能不出现在待我处理；"
                "请在 unreplied_todo.owner_user_id 填数字 userId",
                next_non_digit[:16],
            )
        return next_non_digit

    def _recheck_replied(
        self, item: UnansweredItem, ctx: "PlatformContext",
        self_ids: frozenset[str] | set[str],
    ) -> tuple[bool, str]:
        """建单前单会话复核：本人是否已在这条留言之后回复过。

        返回 ``(True, reason)`` = 确认已回复（不该建单）；``(False, reason)`` =
        确认未回复或无法判定（继续建单）。

        复核不依赖扫描阶段的合并数据（那份数据可能被平台侧补拉截断），而是走
        **单会话**接口单独拉一次——实测该接口返回 ``complete=True``、消息完整。
        """
        if item.platform != "dingtalk":
            return False, "not_dingtalk"
        adapter = ctx.dws
        if adapter is None:
            return False, "no_adapter"
        fn = getattr(adapter, "chat_message_list_conversation", None)
        if not callable(fn):
            return False, "no_conversation_api"
        # 从留言时间的下一分钟开始拉（覆盖留言本身之后的全部回复）
        try:
            base = datetime.fromisoformat(item.timestamp.replace("Z", "+00:00"))
        except ValueError:
            base = None
        since = (base + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S") if base else ""
        if not since:
            return False, "bad_timestamp"
        replied, reason = _recheck_conversation_replies(
            adapter, item.platform, item.chat_id, since, self_ids, resolve_tz(self.cfg.timezone),
        )
        if replied:
            return True, reason or "本人已回复"
        return False, reason

    def _create_for_item(
        self, item: UnansweredItem, ctx: "PlatformContext", self_ids: frozenset[str] | set[str],
        now: datetime | None = None,
    ) -> tuple[bool, str, str]:
        """单条建单 + 双边通知 + 状态落库。返回 (是否成功, 类型, 错误)。

        ``now`` 供测试注入固定时刻（回执闸门依赖「当前时刻」）。
        """
        store = ctx.store
        if store is None:
            return False, "", "平台 store 不可用"
        repo = store._offhours_todo_repo
        if repo.has_todo_record(item.dedupe_key):
            logger.debug("[未回复待办] 去重命中，跳过 key=%s", item.dedupe_key)
            return True, "duplicate", ""

        adapter = ctx.dws
        if adapter is None:
            return False, "", "平台适配器不可用"

        # 🔴 建单前单会话定向复核（2026-11-23 实测事故修复）：
        # 扫描阶段依赖的「全会话时间窗」补拉会被服务端**静默截断**（同一窗口连续三次
        # 返回 118/68/118 条），截断时可能正好漏掉「本人在同一会话里更晚的那条真人回复」，
        # 把「已回复」反转成「未回复」→ 误建待办 + 对外误发「稍后回复」回执。
        # 真实案例：白艳霞 11-22 09:26 的留言本已被坤哥 11-22 11:33「没事」回复过，
        # 却因截断漏掉该回复被误判成未回复。
        # 这里用**单会话**接口复核（实测 complete=True、消息完整），只在**建单前**调用
        # —— 数量极少（每轮个位数），代价可控，且能把误建单挡在最后一关。
        replied, recheck_reason = self._recheck_replied(item, ctx, self_ids)
        if replied:
            logger.info(
                "[未回复待办] 单会话复核发现本人已回复，取消建单 key=%s（%s）",
                item.dedupe_key, recheck_reason,
            )
            try:
                repo.update_unanswered_status(
                    item.dedupe_key, "replied_later", detail="单会话复核：本人已回复",
                )
            except (sqlite3.Error, ValueError) as e:
                logger.debug("[未回复待办] 标记已回复失败 key=%s: %s", item.dedupe_key, e)
            return True, "already_replied", ""

        title = self._build_title(item)
        description = (
            f"来源：{item.platform} / 会话 {item.chat_name or item.chat_id}\n"
            f"发送人：{item.sender_name or item.sender_id}\n"
            f"时间：{item.timestamp}\n"
            f"内容：{item.content_preview[:200]}"
        )
        todo_id = ""
        kind = "platform_todo"
        err = ""

        if item.platform == "wecom":
            # 企微无个人待办 API → 降级为通知型待办（不调 todo 工具）
            kind = "notify_only"
        else:
            executors = self._resolve_executor(item.platform, self_ids)
            try:
                if item.platform == "feishu":
                    # 飞书 task +create 支持独立描述字段（适配器已接线）；基类签名无此参数，
                    # 用 getattr 取运行时方法避免静态签名冲突（实际对象为 FeishuCliAdapter）。
                    result = cast(Any, adapter).todo_task_create(
                        title=title, executors=executors, description=description
                    )
                else:
                    # 钉钉 todo task create 无 description 参数，上下文已并入标题
                    result = adapter.todo_task_create(title=title, executors=executors)
                if isinstance(result, dict):
                    # dws todo task create 返回 {"success": true, "result": {"taskId": ...}}；
                    # 飞书适配器直接返回 {"task_id": ...}。两种结构都要认，否则 todo_id
                    # 记为空，误把「建单成功」记账成没有平台任务 ID。
                    inner = result.get("result")
                    inner = inner if isinstance(inner, dict) else {}
                    todo_id = str(
                        inner.get("task_id") or inner.get("taskId") or inner.get("id")
                        or result.get("task_id") or result.get("taskId")
                        or result.get("id") or result.get("todo_id") or ""
                    )
                    # dws 失败外壳在 run() 已抛异常；这里再兜一层 result 内部的 error
                    if result.get("error") or inner.get("error"):
                        err = str(result.get("error") or inner.get("error"))
            except IMAdapterUnsupportedTypeError:
                kind = "notify_only"
                logger.info("[未回复待办] 平台 %s 待办能力不支持，降级为仅通知", item.platform)
            except _CLI_ERRORS as e:
                err = str(e)

        if err:
            repo.update_unanswered_status(item.dedupe_key, "todo_failed", detail=err[:300])
            return False, kind, err

        # 对方回执（建单后"告诉对方"）——先过时效/时段闸门：
        # 留言太旧、或主人已回到工位时，对方面那句「稍后回复」不合逻辑，不发。
        sender_notified = False
        if self.cfg.notify_sender:
            allowed, skip_reason = self._should_notify_sender(item, now=now)
            if allowed:
                sender_notified = self._notify_sender(item, ctx, adapter)
            else:
                logger.info(
                    "[未回复待办] 跳过回执 key=%s（%s）：留言已陈旧或当前在工作时间，"
                    "只建待办 + 提醒本人",
                    item.dedupe_key, skip_reason,
                )

        # 记录落库（唯一键 = 最终幂等闸门）
        inserted = repo.insert_todo_record(
            platform=item.platform,
            chat_id=item.chat_id,
            msg_id=item.msg_id,
            dedupe_key=item.dedupe_key,
            todo_id=todo_id,
            todo_type=kind,
            title=title,
            status="created",
            sender_notified=sender_notified,
            owner_notified=False,  # 汇总提醒在 scan_all 统一发
        )
        if not inserted:
            # 并发下另一线程已建单：本线程的平台待办可能重复，但记录只保留一条
            logger.warning("[未回复待办] 建单记录去重命中 key=%s（平台侧可能重复）", item.dedupe_key)

        repo.update_unanswered_status(
            item.dedupe_key,
            "notified_only" if kind == "notify_only" else "todo_created",
            detail=f"todo_id={todo_id}" if todo_id else kind,
        )
        return True, kind, ""

    def _should_notify_sender(self, item: UnansweredItem, now: datetime | None = None) -> tuple[bool, str]:
        """回执闸门：这条留言现在还该不该给对方发「稍后回复」。

        回执的语义是「我现在不方便，稍后回」。三种情况下这句话不成立：
        1. 留言已经太旧（超过 ``notice_max_age_hours``）——对方看到的是一句隔天的
           「稍后回复」，既不合逻辑也像骚扰；
        2. 当前已经回到工作时间——主人此刻就在工位上，对对方说「稍后回复」是假话，
           不如安静建单、只提醒本人；
        3. 对方是**群聊**——「稍后回复」是对个人说的承诺，在群里发等于当众承诺，
           且系统日志群（如「工作汇报(日志)」）收到这种回执纯属噪声。
           群聊只建待办 + 提醒本人。

        返回 (是否发回执, 原因码)；原因码用于日志与记录，便于排障时解释
        「为什么这条没有回执」。
        """
        now = now or datetime.now(resolve_tz(self.cfg.timezone))

        if (item.chat_type or "").lower() != "single":
            return False, "group_chat"

        max_age = int(self.cfg.notice_max_age_hours or 0)
        if max_age > 0:
            ts = parse_message_ts(item.timestamp, resolve_tz(self.cfg.timezone))
            if ts is not None:
                age_hours = (now - ts).total_seconds() / 3600.0
                if age_hours > max_age:
                    return False, f"stale_{age_hours:.1f}h"

        if self.cfg.notice_only_when_offhours:
            if not classify(now, self.cfg):
                return False, "now_in_work_hours"

        return True, ""

    def _notify_sender(self, item: UnansweredItem, ctx: "PlatformContext", adapter: Any) -> bool:
        """在原会话给对方回执。钉钉走 conversation_id 稳定 ID 直通，其它平台走 group。"""
        text = self.cfg.sender_notice_text
        try:
            if item.platform == "dingtalk":
                adapter.chat_message_send(
                    conversation_id=item.chat_id, title="", text=text, msg_type="text"
                )
            else:
                adapter.chat_message_send(group=item.chat_id, title="", text=text, msg_type="text")
        except _CLI_ERRORS as e:
            logger.warning("[未回复待办] 对方回执失败 key=%s: %s", item.dedupe_key, e)
            return False
        self._persist_notice_message(item, ctx, text)
        return True

    def _persist_notice_message(
        self, item: UnansweredItem, ctx: "PlatformContext", text: str,
    ) -> None:
        """把刚发出的回执落库为 **assistant / is_bot=1**，避免污染「本人真人回复」判定。

        🔴 为什么必须落库标记（2026-11-23 实测事故）：
        回执是以**本人账号**发出的，钉钉侧看 sendType='user'、senderId=本人 → 轮询拉回
        后在本地库被标成 role='user'/is_bot=0。而「未回复」判定口径是
        「is_bot=0 且 sender_id ∈ 本人 ID」= 本人真人回复 —— 于是这条**自动回执**被
        当成「本人已亲自回复」，把该会话里更早的、真正未回复的留言误判为已回复（漏建单），
        也会骗过建单前的单会话复核。

        这里主动落库并标记 is_bot=True（与 AI 回复走同一条路径，见
        ``src/platform/runtime_dispatch.py``），让轮询拉回同一条时能识别为 AI 代发。
        """
        store = ctx.store
        if store is None:
            return
        try:
            from src.models import Message as MessageModel

            engine = self.engine
            notice = MessageModel(
                msg_id=f"offhours-notice-{item.dedupe_key}",
                chat_id=item.chat_id,
                chat_type=item.chat_type or "single",
                chat_name=item.chat_name or "",
                sender_id=str(getattr(engine, "current_open_dingtalk_id", "") or "") or "ai",
                sender_name=str(getattr(engine, "current_user_name", "") or "AI助手"),
                content=text,
                msg_type="text",
                timestamp=datetime.now(),
                raw={},
                is_bot=True,
            )
            store._message_repo.save_message(notice, role="assistant")
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as e:
            # 收窄异常类型：DB 写失败 / 模型构造参数非法 / 适配器缺方法。
            # 标记失败不应阻断回执主流程，故只 debug 不抛。
            logger.debug("[未回复待办] 回执落库标记失败 key=%s: %s", item.dedupe_key, e)

    def _notify_owner(self, notices: list[str]) -> bool:
        """钉钉私聊给我本人推一条汇总提醒（owner_* 二选一；皆空则跳过）。"""
        main_ctx = self.platforms.get("dingtalk")
        adapter = main_ctx.dws if main_ctx is not None else None
        if adapter is None:
            logger.debug("[未回复待办] 主平台适配器不可用，跳过本人汇总提醒")
            return False
        text = "📋 工作时间外未回复消息已建待办（共 %d 条）\n%s" % (
            len(notices), "\n".join("· " + n for n in notices[:20])
        )
        try:
            if self.cfg.owner_user_id:
                adapter.chat_message_send(title="未回复消息待办", text=text, user=self.cfg.owner_user_id)
            elif self.cfg.owner_open_dingtalk_id:
                adapter.chat_message_send(
                    title="未回复消息待办", text=text,
                    open_dingtalk_id=self.cfg.owner_open_dingtalk_id,
                )
            else:
                logger.debug("[未回复待办] 未配置 owner 身份，跳过本人汇总提醒")
                return False
            return True
        except _CLI_ERRORS as e:
            logger.warning("[未回复待办] 本人汇总提醒失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # Web 侧查询 / 操作
    # ------------------------------------------------------------------

    def get_rules(self) -> dict[str, Any]:
        return self.cfg.model_dump()

    def list_unanswered(
        self, *, status: str = "", platform: str = "", chat_id: str = "",
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """跨已启用平台聚合未回复消息（按消息时间倒序）。"""
        items: list[dict[str, Any]] = []
        total = 0
        for pid, ctx in self.platforms.items():
            if (platform and pid != platform) or ctx.store is None:
                continue
            rows, n = ctx.store._offhours_todo_repo.list_unanswered(
                status=status, platform=pid, chat_id=chat_id,
                limit=limit + offset, offset=0,
            )
            items.extend(rows)
            total += n
        items.sort(key=lambda r: str(r.get("msg_timestamp") or ""), reverse=True)
        # 会话名在 Web 侧按平台补齐（扫描热路径不查，避免拖慢主链路）
        for row in items:
            ctx = self.platforms.get(str(row.get("platform") or ""))
            if ctx is None or ctx.store is None:
                continue
            conv = ctx.store._conversation_repo.get_conversation(str(row.get("chat_id") or ""))
            if conv:
                row["chat_name"] = conv.get("chat_name") or row.get("chat_name") or ""
        return {"items": items[offset: offset + limit], "total": total}

    def list_records(self, *, platform: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        total = 0
        for pid, ctx in self.platforms.items():
            if (platform and pid != platform) or ctx.store is None:
                continue
            rows, n = ctx.store._offhours_todo_repo.list_todo_records(
                platform=pid, limit=limit + offset, offset=0
            )
            items.extend(rows)
            total += n
        items.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return {"items": items[offset: offset + limit], "total": total}

    def get_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        for pid, ctx in self.platforms.items():
            if ctx.store is not None:
                stats[pid] = ctx.store._offhours_todo_repo.get_stats()
        return stats

    def create_for_key(self, dedupe_key: str) -> dict[str, Any]:
        """Web 手动对某条 pending 消息建单（忽略 auto_create 开关）。"""
        for pid, ctx in self.platforms.items():
            if ctx.store is None:
                continue
            repo = ctx.store._offhours_todo_repo
            row = repo.get_unanswered_by_key(dedupe_key)
            if row is None:
                continue
            if row.get("status") in ("todo_created", "notified_only"):
                return {"success": False, "error": "该消息已建过待办"}
            item = UnansweredItem(
                platform=pid,
                chat_id=str(row.get("chat_id") or ""),
                chat_name=str(row.get("chat_name") or ""),
                chat_type=str(row.get("chat_type") or ""),
                msg_id=str(row.get("msg_id") or ""),
                sender_id=str(row.get("sender_id") or ""),
                sender_name=str(row.get("sender_name") or ""),
                content_preview=str(row.get("content_preview") or ""),
                timestamp=str(row.get("msg_timestamp") or ""),
                reason=str(row.get("reason") or ""),
                dedupe_key=dedupe_key,
            )
            ok, kind, err = self._create_for_item(item, ctx, collect_platform_self_ids(self.engine, ctx))
            return {"success": ok, "kind": kind, "error": err}
        return {"success": False, "error": "未找到该消息"}

    def ignore_for_key(self, dedupe_key: str) -> dict[str, Any]:
        """Web 手动忽略某条消息（不再建单）。"""
        for _pid, ctx in self.platforms.items():
            if ctx.store is None:
                continue
            repo = ctx.store._offhours_todo_repo
            if repo.get_unanswered_by_key(dedupe_key) is None:
                continue
            ok = repo.update_unanswered_status(dedupe_key, "ignored", detail="手动忽略")
            return {"success": bool(ok)}
        return {"success": False, "error": "未找到该消息"}

    @property
    def last_result(self) -> dict[str, Any]:
        return self._last_result
