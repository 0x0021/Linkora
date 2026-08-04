"""技能管理器：发现、加载、缓存所有技能，提供查询接口。

支持热加载：启动后自动监控 skills 目录变更，无需重启即可加载新安装的技能。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from src.skills.loader import Skill, SkillLoader

logger = logging.getLogger(__name__)

# 默认热加载轮询间隔（秒）
_DEFAULT_POLL_INTERVAL = 15


class SkillManager:
    """管理项目根目录下所有技能的生命周期。

    功能：
    - discover(): 扫描 data/skills 和 .agents/skills
    - reload():   重新加载全部技能（线程安全）
    - start_watcher() / stop_watcher(): 热加载文件监控（后台轮询）
    - 提供按名称查找、全量列表等查询接口
    """

    def __init__(self, project_root: str | Path, poll_interval: float = _DEFAULT_POLL_INTERVAL):
        self._root = Path(project_root).resolve()
        self._loader = SkillLoader(self._root)
        self._skills: dict[str, Skill] = {}
        self._loaded = False
        self._lock = threading.RLock()
        # 热加载状态
        self._poll_interval = poll_interval
        self._watcher_thread: threading.Thread | None = None
        self._watcher_stop = threading.Event()
        # 变更检测指纹：记录上次扫描时各目录的最新 mtime
        self._last_fingerprint: dict[str, float] = {}

    # ── 加载 ──────────────────────────────────────────────────

    def discover(self) -> list[str]:
        """扫描并返回所有技能目录路径。"""
        return self._loader.discover()

    def reload(self) -> int:
        """重新扫描并加载全部技能。返回成功加载的技能数（去重后）。线程安全。

        同时更新变更检测指纹，供热加载轮询使用。
        """
        with self._lock:
            self._skills.clear()

            for skill_dir in self.discover():
                skill = self._loader.load(skill_dir)
                if skill is None:
                    continue
                if skill.name in self._skills:
                    logger.warning(
                        "技能名冲突 %s: %s 被 %s 覆盖",
                        skill.name, self._skills[skill.name].source_path, skill.source_path,
                    )
                self._skills[skill.name] = skill
                logger.info("已加载技能: %s (%s)", skill.name, skill.description[:50])

            self._loaded = True
            count = len(self._skills)
            # 更新指纹
            self._update_fingerprint()
            logger.info("技能加载完成: %d 个技能", count)
            return count

    @property
    def loaded(self) -> bool:
        return self._loaded

    # ── 热加载监控 ────────────────────────────────────────────

    def start_watcher(self) -> None:
        """启动后台热加载监控线程（守护线程，随主进程退出）。

        以 _poll_interval 为间隔扫描 skills 目录，
        检测到新增/删除/修改的技能目录时自动调用 reload()。
        已有线程在运行时不会重复启动。
        """
        if self._watcher_thread and self._watcher_thread.is_alive():
            logger.warning("[SkillWatcher] 已在运行，跳过重复启动")
            return

        self._watcher_stop.clear()
        t = threading.Thread(
            target=self._watch_loop,
            name="SkillHotReload",
            daemon=True,
        )
        t.start()
        self._watcher_thread = t
        logger.info("[SkillWatcher] 热加载已启动，轮询间隔 %.0fs", self._poll_interval)

    def stop_watcher(self) -> None:
        """停止热加载监控线程。"""
        self._watcher_stop.set()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=5)
        logger.info("[SkillWatcher] 热加载已停止")

    def _update_fingerprint(self) -> None:
        """更新各技能目录的 mtime 指纹。"""
        self._last_fingerprint.clear()
        for skill_dir in self.discover():
            try:
                # 用目录内最新文件的 max mtime 作为指纹
                p = Path(skill_dir)
                latest = max(
                    (f.stat().st_mtime for f in p.rglob("*") if f.is_file()),
                    default=p.stat().st_mtime,
                )
                self._last_fingerprint[skill_dir] = latest
            except OSError as _exc:
                logger.debug(f"_update_fingerprint: swallowed exception: {_exc}")
                pass

    def _has_changes(self) -> bool:
        """检查 skills 目录是否有变更（与上次 fingerprint 比较）。"""
        current: dict[str, float] = {}
        for skill_dir in self.discover():
            try:
                p = Path(skill_dir)
                latest = max(
                    (f.stat().st_mtime for f in p.rglob("*") if f.is_file()),
                    default=p.stat().st_mtime,
                )
                current[skill_dir] = latest
            except OSError as _exc:
                logger.debug(f"_has_changes: swallowed exception: {_exc}")
                current[skill_dir] = 0.0

        # 目录集合变化或任一目录 mtime 变化
        if set(current.keys()) != set(self._last_fingerprint.keys()):
            return True
        for d, t in current.items():
            if abs(self._last_fingerprint.get(d, 0) - t) > 0.5:  # 0.5s 容差防抖
                return True
        return False

    def _watch_loop(self) -> None:
        """后台轮询循环。首次先建立基线指纹，之后每轮检测变更。"""
        # 首次建立基线
        with self._lock:
            self._update_fingerprint()

        while not self._watcher_stop.is_set():
            try:
                self._watcher_stop.wait(timeout=self._poll_interval)
                if self._watcher_stop.is_set():
                    break
                if self._has_changes():
                    logger.info("[SkillWatcher] 检测到 skills 目录变更，自动 reload")
                    count = self.reload()
                    logger.info("[SkillWatcher] 热加载完成: %d 个技能", count)
            except Exception as e:
                logger.error("[SkillWatcher] 轮询异常: %s", e, exc_info=True)

    # ── 查询 ──────────────────────────────────────────────────

    def get(self, name: str) -> Skill | None:
        """按名称查找技能。"""
        with self._lock:
            return self._skills.get(name)

    def list_all(self) -> list[Skill]:
        """返回全部已加载技能（快照）。"""
        with self._lock:
            return list(self._skills.values())

    def list_names(self) -> list[str]:
        """返回全部技能名。"""
        with self._lock:
            return list(self._skills.keys())

    def get_disabled_skill_owned_tools(self,
                                        tool_domain_map: dict[str, list[str]] | None = None
                                        ) -> set[str]:
        """返回所有已停用技能「声明覆盖」的工具名集合。

        用于 agent._select_tools 在 smart 模式下排除 disabled skill 的工具，
        使 Web 技能管理页的 enabled 开关真正生效（而不仅是跳过 skill 激活）。

        匹配逻辑：skill 的 intent_categories 与工具的 intent_categories 有交集
        → 视为该 skill「拥有」该工具。多个 skill 共享同一工具时，只要其中
        任意一个 skill 仍启用，该工具就不被排除（避免误杀）。

        Args:
            tool_domain_map: 工具名→域类别列表的映射；None 时返回空集。
        """
        if not tool_domain_map:
            return set()

        # 先收集所有「仍有启用 skill 声称」的域类别——这些域的工具不受保护
        protected_domains: set[str] = set()
        all_disabled_domains: dict[str, list[str]] = {}  # domain -> [skill_names]

        with self._lock:
            for skill in self._skills.values():
                if not skill.intent_categories:
                    continue
                domains = set(skill.intent_categories)
                if skill.enabled:
                    protected_domains |= domains
                else:
                    for d in domains:
                        all_disabled_domains.setdefault(d, []).append(skill.name)

        # 工具属于 disabled skill 的域 且 不在任何启用 skill 的域 → 排除
        blocked: set[str] = set()
        disabled_domain_set = set(all_disabled_domains.keys())
        for tool_name, domains in tool_domain_map.items():
            tool_domains_set = set(domains)
            if not tool_domains_set:
                continue
            # 只当工具的域全部落在 disabled 域中、且无任何启用 skill 保底时才屏蔽
            if tool_domains_set <= disabled_domain_set and not (tool_domains_set & protected_domains):
                blocked.add(tool_name)
                logger.debug("[SkillManager] skill已停用,屏蔽工具 %s (域=%s)",
                             tool_name, sorted(tool_domains_set))

        return blocked

    # ── AI 意图词生成 ────────────────────────────────────────

    def generate_intents(
        self,
        client,
        registry=None,
        names: list[str] | None = None,
        force: bool = False,
        throttle=None,
        persist: bool = True,
    ) -> dict:
        """为技能批量生成 AI 意图词（分析 SKILL.md，调用 LLM 产出意图词并写回）。

        参数：
          - client: LLMClient 实例
          - registry: IntentRegistry（默认进程内 default_registry）
          - names: 仅生成指定技能名；None=全部
          - force: 是否覆盖已有意图词（否则仅填充空意图词的技能）
          - throttle: 可选 LLM 节流器（如 BackgroundLLMThrottle）
          - persist: 是否写回 SKILL.md（False 仅更新内存对象）

        返回：{"total", "generated", "skipped", "failed", "details": [...]}
        """
        from src.skills.intent_generator import IntentGenerator
        from src.intent import default_registry
        from src.semantic import invalidate_skills

        if registry is None:
            registry = default_registry

        generator = IntentGenerator(client, registry, throttle=throttle)

        with self._lock:
            targets = [s for s in self._skills.values() if (names is None or s.name in names)]

        details: list[dict] = []
        generated = skipped = failed = 0
        for skill in targets:
            try:
                result = generator.generate(skill, force=force)
                if result is None:
                    # 跳过（已有意图词且非 force）或 LLM 失败/超时
                    if not force and (skill.intent_categories or skill.intent_keywords):
                        status = "skipped_existing"
                        skipped += 1
                    else:
                        status = "failed"
                        failed += 1
                    details.append({"name": skill.name, "status": status})
                    continue

                if persist:
                    ok = self._loader.save_intent(
                        skill, result["intent_categories"], result["intent_keywords"]
                    )
                    if not ok:
                        failed += 1
                        details.append({"name": skill.name, "status": "write_failed"})
                        continue
                else:
                    skill.intent_keywords = list(result["intent_keywords"])
                    if result["intent_categories"]:
                        skill.intent_categories = list(result["intent_categories"])

                generated += 1
                details.append({
                    "name": skill.name,
                    "status": "generated",
                    "intent_categories": result["intent_categories"],
                    "intent_keywords": result["intent_keywords"],
                })
            except Exception as e:
                logger.error("[IntentGen] 生成异常 %s: %s", skill.name, e, exc_info=True)
                failed += 1
                details.append({"name": skill.name, "status": "error", "error": str(e)})

        # 意图词变化 → 语义向量缓存失效，使语义路由重算
        if generated > 0:
            try:
                invalidate_skills()
            except Exception as e:
                logger.warning("invalidate_skills 失败，语义缓存可能过期: %s", e)

        return {
            "total": len(targets),
            "generated": generated,
            "skipped": skipped,
            "failed": failed,
            "details": details,
        }

    def generate_intents_trace(
        self,
        client,
        name: str,
        force: bool = False,
        throttle=None,
        persist: bool = True,
    ) -> dict:
        """为单个技能生成 AI 意图词，并返回完整交互过程（供前端可视化）。

        与 generate_intents 的区别：聚焦单技能，附带 LLM 交互 trace
        （发送 messages / 原始返回 / 解析结果 / 写回状态），不消耗批量限速器。

        返回：{"skill", "found", "trace", "written", "result"}。
        """
        from src.skills.intent_generator import IntentGenerator
        from src.intent import default_registry
        from src.semantic import invalidate_skills

        generator = IntentGenerator(client, default_registry, throttle=throttle)

        with self._lock:
            skill = self._skills.get(name)

        if skill is None:
            return {
                "skill": name,
                "found": False,
                "trace": {"skill": name, "skipped": False, "messages": [],
                          "raw_response": None, "result": None,
                          "error": "技能不存在"},
                "written": False,
                "result": None,
            }

        out = generator.generate_with_trace(skill, force=force)
        result = out["result"]
        trace = out["trace"]

        written = False
        if result is not None:
            if persist:
                written = bool(self._loader.save_intent(
                    skill, result["intent_categories"], result["intent_keywords"]
                ))
            else:
                skill.intent_keywords = list(result["intent_keywords"])
                if result["intent_categories"]:
                    skill.intent_categories = list(result["intent_categories"])
            # 意图词变化 → 语义向量缓存失效，使语义路由重算
            try:
                invalidate_skills()
            except Exception as e:
                logger.warning("invalidate_skills 失败，语义缓存可能过期: %s", e)

        return {
            "skill": name,
            "found": True,
            "trace": trace,
            "written": written,
            "result": result,
        }

    # ── System prompt 注入 ───────────────────────────────────

    def skills_prompt_section(self) -> str:
        """生成供 system prompt 注入的「可用技能」片段。

        仅生成目录式简介（一行一个技能），不包含完整 SKILL.md 正文。
        完整正文在技能被激活时通过 activate() 注入。
        """
        with self._lock:
            if not self._skills:
                return ""

            lines = ["\n\n【可用技能（Skills）】",
                     "直接提及技能名即可激活（例如「用 web-search 帮我搜 XX」）。"]

            for skill in self._skills.values():
                lines.append(skill.prompt_section())

            lines.append(
                "技能激活方式：直接对 AI 说「用 {技能名} 帮我做 XX」或「启动 web-search」。\n"
                "激活后该技能的专业指令会注入到对话上下文。"
            )
            return "\n".join(lines)

    def activate_prompt(self, name: str) -> str | None:
        """返回某个技能被激活时注入到对话的 prompt 文本。"""
        with self._lock:
            skill = self._skills.get(name)
            if not skill:
                return None
            return (
                f"\n\n【已激活技能：{skill.name}】\n"
                f"以下是该技能的专业指令，请严格遵循：\n\n"
                f"{skill.body}"
            )
