"""DwsAdapter 文档/联系人/日历/待办 mixin。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging

from src.dws_adapter.core import DwsError

logger = logging.getLogger(__name__)


class DwsAdapterDocMixin(DwsAdapterBase):
    @staticmethod
    def _normalize_doc_search(data: object) -> list[dict]:
        """把新旧两种文档搜索返回统一成旧契约结构。

        旧命令 ``doc search``：顶层 ``documents[]``，字段 ``nodeType`` / ``docUrl``。
        新命令 ``drive +search-docs``：``data.docs[]``，字段 ``type`` / ``url``。
        对外统一为 ``documents[]`` + 旧字段名，调用方（src/tools/doc.py）无需感知。
        """
        if not isinstance(data, dict):
            return []
        raw = data.get("documents")
        if raw is None:
            inner = data.get("data")
            if isinstance(inner, dict):
                raw = inner.get("docs")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for d in raw:
            if not isinstance(d, dict):
                continue
            item = dict(d)
            # 新命令字段 → 旧契约字段（旧命令已有同名字段时不覆盖）
            if not item.get("nodeType"):
                item["nodeType"] = d.get("type", "")
            if not item.get("docUrl"):
                item["docUrl"] = d.get("url", "")
            out.append(item)
        return out

    def doc_search(self, query: str, page_size: int = 10) -> list[dict]:
        """按关键词搜索钉钉文档，返回 ``documents[]``（名称/nodeId/类型/URL）。

        主路径走 ``dws drive +search-docs``（v1.0.63 实测仍可用）；仅当旧版 dws
        不认识该子命令时回退 ``dws doc search``，保证「升级」与「回退」两个方向都能用。

        ⚠️ 版本核实（2026-10-22，dws v1.0.63）：``dws doc search`` **已不再**输出
        deprecated 警告，命令本身可用（v1.0.62-beta.8 时曾标 deprecated，建议改用
        ``drive search`` / ``wiki node search``）。但主路径仍优先 ``+search-docs``
        ——它按「在线文档」语义检索，与「钉盘文件检索」（``drive +search``）区分
        开，与本方法「搜文档标题」的用途更贴合；``doc search`` 保留为旧版回退。
        """
        try:
            data = self.run([
                "drive", "+search-docs",
                "--query", query,
                "--limit", str(page_size),
            ], operation="doc_search", force_no_dry_run=True)
        except DwsError as e:
            msg = str(e).lower()
            # 仅当确属「命令不存在」才回退；权限/网络等真实失败必须原样抛出，
            # 否则会把故障伪装成「搜不到文档」。
            if not any(k in msg for k in (
                    "unknown command", "unknown flag", "unknown shorthand",
                    "no such", "not found")):
                raise
            logger.info("dws 不支持 drive +search-docs，回退 doc search: %s", e)
            data = self.run([
                "doc", "search",
                "--query", query,
                "--page-size", str(page_size),
            ], operation="doc_search", force_no_dry_run=True)
        return self._normalize_doc_search(data)

    def doc_read(self, node_id: str, content_format: str = "markdown") -> dict:
        data = self.run([
            "doc", "read",
            "--node", node_id,
            "--content-format", content_format,
        ], force_no_dry_run=True)
        return data if isinstance(data, dict) else {}

    def contact_user_search(self, keyword: str) -> list[dict]:
        try:
            data = self.run([
                "contact", "user", "search",
                "--query", keyword,
            ], force_no_dry_run=True)
        except DwsError as e:
            if self._is_personal_dingtalk_error(str(e)):
                logger.debug("个人钉钉模式：contact_user_search 不可用，跳过")
                return []
            raise
        result = self._get_result(data)
        if isinstance(result, list):
            return result
        return []

    def calendar_event_list(self, start: str = "", end: str = "") -> list[dict]:
        args = ["calendar", "event", "list"]
        if start:
            args.extend(["--start", start])
        if end:
            args.extend(["--end", end])
        data = self.run(args, force_no_dry_run=True)
        result = self._get_result(data)
        if isinstance(result, dict):
            return result.get("events", [])
        return []

    @staticmethod
    def _calendar_payload(data: object) -> dict:
        """剥掉 shortcut 的多层包裹，取出真正的 data 层。

        dws calendar 的 ``+xxx`` shortcut 返回体**无 result 包装**，形如
        ``{ok, outcome, data:{...}}``；原子命令则是 ``{result:{...}}``。
        两种都要能吃，故先解 result、再解 data，都解不到就原样返回。
        """
        if not isinstance(data, dict):
            return {}
        inner = data.get("result")
        if isinstance(inner, dict):
            data = inner
        payload = data.get("data")
        if isinstance(payload, dict):
            return payload
        return data

    def calendar_conflicts(self, *, in_days: int = 0) -> dict:
        """检测我某天的日程是否有时间冲突。

        封装 `dws calendar +conflicts --in-days N`（0=今天，1=明天…）。
        纯只读：列出当天全部日程后在本地两两比对开始/结束时间。

        返回 ``{"date","event_count","conflict_count","conflicts":[{...}],
        "has_conflict","complete"}``。``conflicts`` 每项含重叠的两个日程
        （title/start/end）。当天无日程时 ``event_count=0``、``conflict_count=0``。
        """
        args = ["calendar", "+conflicts", "--in-days", str(max(0, int(in_days)))]
        data = self.run(args, operation="calendar_conflicts", force_no_dry_run=True)
        payload = self._calendar_payload(data)
        conflicts = payload.get("conflicts")
        return {
            "date": payload.get("date") or "",
            "event_count": int(payload.get("eventCount") or 0),
            "conflict_count": int(payload.get("conflictCount") or 0),
            "conflicts": conflicts if isinstance(conflicts, list) else [],
            "has_conflict": bool(payload.get("hasConflict")),
            "complete": bool(payload.get("complete")),
        }

    def calendar_free_slots(self, *, in_days: int = 0,
                            from_hour: int = 9, to_hour: int = 18) -> dict:
        """查询我某天的空闲时间段（工作时段内的空档）。

        封装 `dws calendar +free-slots --in-days N --from H --to H`。
        纯只读：列出当天全部日程，合并忙碌时段，在工作时段范围内算出所有空闲窗口。

        返回 ``{"date","window","slot_count","all_busy","free_slots":[
        {"start","end","duration_min"}]}``。``all_busy=True`` 表示工作时段被占满。
        """
        args = ["calendar", "+free-slots", "--in-days", str(max(0, int(in_days))),
                "--from", str(max(0, min(int(from_hour), 23))),
                "--to", str(max(1, min(int(to_hour), 24)))]
        data = self.run(args, operation="calendar_free_slots", force_no_dry_run=True)
        payload = self._calendar_payload(data)
        slots = payload.get("freeSlots")
        normalized: list[dict] = []
        if isinstance(slots, list):
            for s in slots:
                if not isinstance(s, dict):
                    continue
                normalized.append({
                    "start": s.get("start") or "",
                    "end": s.get("end") or "",
                    "duration_min": int(s.get("durationMin") or 0),
                })
        return {
            "date": payload.get("date") or "",
            "window": payload.get("window") or f"{from_hour:02d}:00-{to_hour:02d}:00",
            "slot_count": int(payload.get("slotCount") or len(normalized)),
            "all_busy": bool(payload.get("allBusy")),
            "free_slots": normalized,
        }

    def calendar_suggest_time(self, *, participants: list[str], start: str,
                              end: str, duration: int = 0) -> dict:
        """为多人推荐共同空闲的会议时间段。

        封装 `dws calendar +suggest-time --with 姓名列表 --start --end [--duration]`。
        纯只读，不创建任何日程。

        ⚠️ CLI 会把 ``--with`` 里的姓名逐个解析为唯一 userId，**匹配不到或匹配到
        多人时明确报错**（绝不瞎猜）——这正是期望行为，错误信息直接透传给调用方。

        返回 ``{"suggestions":[{"start","end"}], "count", "complete"}``。
        """
        if not participants:
            return {"error": "calendar_suggest_time 需提供 participants（至少一人）"}
        if not start or not end:
            return {"error": "calendar_suggest_time 需提供 start 与 end 时间范围"}
        args = ["calendar", "+suggest-time", "--with", ",".join(participants),
                "--start", start, "--end", end]
        if duration:
            args.extend(["--duration", str(int(duration))])
        data = self.run(args, operation="calendar_suggest_time", force_no_dry_run=True)
        payload = self._calendar_payload(data)
        suggestions = payload.get("suggestions")
        normalized: list[dict] = []
        if isinstance(suggestions, list):
            for s in suggestions:
                if not isinstance(s, dict):
                    continue
                normalized.append({
                    "start": s.get("start") or "",
                    "end": s.get("end") or "",
                })
        return {
            "suggestions": normalized,
            "count": int(payload.get("count") or len(normalized)),
            "complete": bool(payload.get("complete")),
        }

    def todo_task_create(self, title: str, executors: str,
                         due: str = "", priority: str = "") -> dict:
        args = ["todo", "task", "create", "--title", title, "--executors", executors]
        if due:
            args.extend(["--due", due])
        if priority:
            args.extend(["--priority", priority])
        return self.run(args)

    def todo_list_mine(self, *, size: int = 20, page: int = 1,
                       status: str = "", role_types: str = "",
                       priority: str = "") -> dict:
        """列出与我相关的待办（默认执行人角色）。

        封装 `dws todo +get-my-tasks`（shortcut，返回体无 result 包装，形如
        ``{ok, outcome, data:{count, hasMore, page, size, todos[]}}``）。

        实测 v1.0.63 约束：
        - ``--size`` 合法区间 1..20（不是 --limit，越界会被 CLI 拒绝）；
        - ``--page`` 必须 > 0；
        - ``--status`` 只接受 ``true``（已完成）/ ``false``（未完成）；
        - ``--role-types`` 只接受 ``creator``/``executor``/``participant``，
          默认为 ``executor``；
        - ``--priority`` 只接受 ``10``/``20``/``30``/``40``；
        - ``--all`` 会遍历全部分页，但服务端最多 40 页，超出会报
          ``pagination_limit_reached``（待办量大时不可用）——本方法**不传** ``--all``，
          由调用方按 ``hasMore`` 自行翻页。

        每项归一化为 ``{task_id, subject, due_time, priority, status_stage, done}``。
        ``finalStatusStage`` 实测语义（v1.0.63 交叉验证）：**2 = 已完成**。
        交叉验证方式：``+get-my-tasks`` 全量 20 条 stage 均为 2，同时
        ``--status false``（未完成）返回 0 条、``--status true``（已完成）返回
        同批 stage=2 的项；再对其中 3 个 taskId 调 ``todo task get`` 逐个核对
        ``isDone=True``。注意 ``todo task get`` 的 ``finishTime`` 恒为 0
        （审批型待办 bizTag=certify_todo 不填该字段），**不能**用它判完成。
        """
        args = ["todo", "+get-my-tasks", "--size", str(max(1, min(int(size), 20))),
                "--page", str(max(1, int(page)))]
        if status in ("true", "false"):
            args.extend(["--status", status])
        if role_types:
            args.extend(["--role-types", role_types])
        if priority:
            args.extend(["--priority", priority])

        data = self.run(args, operation="todo_list_mine", force_no_dry_run=True)
        result = self._get_result(data)
        if not isinstance(result, dict):
            # shortcut 无 result 包装：顶层就是 {ok, outcome, data}
            result = data if isinstance(data, dict) else {}
        inner = result.get("data")
        payload: dict = inner if isinstance(inner, dict) else result

        todos: list[dict] = []
        raw_items = payload.get("todos") or payload.get("items") or []
        if isinstance(raw_items, list):
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                stage = it.get("finalStatusStage")
                todos.append({
                    "task_id": str(it.get("taskId") or it.get("task_id") or ""),
                    "subject": it.get("subject") or it.get("title") or "",
                    "due_time": it.get("dueTime") or it.get("planFinishTime") or 0,
                    "priority": it.get("priority") or 0,
                    "status_stage": stage if stage is not None else 0,
                    "done": stage == 2,
                })
        return {
            "todos": todos,
            "count": len(todos),
            "has_more": bool(payload.get("hasMore")),
            "page": payload.get("page") or 1,
            "size": payload.get("size") or len(todos),
        }

    def todo_complete(self, task_id: str) -> dict:
        """完成一条待办并读回验证（写操作）。

        封装 `dws todo +complete --task-id <id>`。

        与其他写操作一致：尊重全局 dry_run（不加 force_no_dry_run），
        干跑模式下仅预览不实际完成。
        """
        if not task_id:
            return {"success": False, "error": "task_id 不能为空"}
        args = ["todo", "+complete", "--task-id", str(task_id)]
        data = self.run(args, operation="todo_complete")
        return data if isinstance(data, dict) else {"success": bool(data)}

