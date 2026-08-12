"""AI 驱动的技能意图词生成器。

读取技能的 SKILL.md（name / description / body），结合 IntentRegistry 中已有的
`domain.*` 领域类别目录，调用 LLM 生成：
  - intent_categories：匹配到的 domain.* 类别 id 列表（只从给定清单中选）
  - intent_keywords：用户可能说出的自由触发词（中文/英文短语，覆盖口语与同义改写）

随后把匹配 domain 的关键词展开，与自由触发词合并为**统一**的 `intent_keywords`，
使子串路由与语义路由都能直接受益（无需改动 `effective_intent_keywords` 既有逻辑）。

设计要点：
- 生成失败 / 超时 / 返回非 JSON → 返回 None，调用方应保留既有意图词（优雅降级）。
- 若不填 `force`，技能已声明 intent_categories / intent_keywords 时直接跳过（尊重人工配置）。
- 可选传入 throttle（如 BackgroundLLMThrottle）避免免费额度被打爆。
"""
from __future__ import annotations

import logging
from typing import Optional, cast

from src.intent import IntentRegistry, LAYER_DOMAIN, default_registry
from src.llm.client import LLMClient, LLMResponse
from src.utils.llm_json import extract_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个意图分析助手。你会收到一个"技能(Skill)"的元信息（名称、描述、正文摘要），
以及一个"可选领域类别清单"（每个类别有 id、中文名、定义）。
请分析该技能的核心能力与典型用户请求，输出 JSON：

{
  "intent_categories": ["匹配到的领域类别 id（只从给定清单里选，可多选，无匹配则为空数组）"],
  "intent_keywords": ["用户可能会说出的触发短语/关键词（中文与英文均可，3-12 字为宜，覆盖口语与同义改写，最多 25 个）"]
}

要求：
- intent_categories 只能从给定清单的 id 中选，不要编造 id。
- intent_keywords 应是"用户消息里可能出现的词"，而不是技能内部术语；要利于路由命中。
- 只输出 JSON，不要任何解释，也不要用 markdown 代码块包裹。"""


# extract_json 已提升为全项目共享工具（src/utils/llm_json），此处保留同名再导出，
# 兼容既有 `from src.skills.intent_generator import extract_json` 的调用与测试。
__all__ = ["extract_json"]


def _normalize_keywords(kws) -> list[str]:
    """小写去重，过滤空值与非字符串。"""
    out: list[str] = []
    seen: set[str] = set()
    for k in (kws or []):
        if not isinstance(k, str):
            continue
        k = k.strip().lower()
        if not k:
            continue
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


class IntentGenerator:
    """基于 LLM 的技能意图词生成器。"""

    def __init__(
        self,
        client: LLMClient,
        registry: Optional[IntentRegistry] = None,
        throttle=None,
    ):
        self.client = client
        self.registry = registry or default_registry
        self.throttle = throttle

    # ── 提示词构建 ──────────────────────────────────────────

    def _domain_catalog(self) -> str:
        lines = []
        for c in self.registry.all():
            if getattr(c, "layer", None) == LAYER_DOMAIN:
                lines.append(f"- {c.id} | {c.name} | {c.definition}")
        return "\n".join(lines)

    def build_messages(self, skill) -> list[dict]:
        catalog = self._domain_catalog()
        body_summary = (skill.body or "")[:2000]
        user = (
            f"技能名称: {skill.name}\n"
            f"技能描述: {skill.description}\n"
            f"技能正文摘要:\n{body_summary}\n\n"
            f"可选领域类别清单（id | 中文名 | 定义）:\n{catalog}\n\n"
            "请按系统指令输出 JSON。"
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    # ── 核心生成 ────────────────────────────────────────────

    def _generate_core(self, skill, force: bool, return_trace: bool):
        """统一生成内核。

        force/return_trace 组合：
        - return_trace=False → 兼容旧接口 generate()，返回 dict 或 None。
        - return_trace=True  → 返回 {"result": dict|None, "trace": {...}}，
          trace 含发送 messages / LLM 原始返回 / 解析结果 / 跳过或失败原因，
          供前端把「AI 生成过程」可视化。
        """
        trace: dict = {
            "skill": skill.name,
            "skipped": False,
            "messages": [],
            "raw_response": None,
            "result": None,
            "error": None,
        }

        if not force and (skill.intent_categories or skill.intent_keywords):
            trace["skipped"] = True
            trace["error"] = "技能已声明意图词，未启用 force，跳过（尊重人工配置）"
            return {"result": None, "trace": trace} if return_trace else None

        if self.throttle is not None:
            if not self.throttle.acquire():
                logger.warning("[IntentGen] 节流拒绝，跳过技能 %s", skill.name)
                trace["error"] = "被后台 LLM 节流器拒绝（限流保护）"
                return {"result": None, "trace": trace} if return_trace else None

        messages = self.build_messages(skill)
        trace["messages"] = messages

        try:
            resp: LLMResponse = self.client.chat(messages, temperature=0.1)
        except Exception as e:  # 网络/限流/超时
            logger.warning("[IntentGen] 调用 LLM 失败（技能 %s）: %s", skill.name, e)
            trace["error"] = f"LLM 调用失败: {e}"
            return {"result": None, "trace": trace} if return_trace else None

        if not resp or not resp.content:
            trace["error"] = "LLM 返回空内容"
            return {"result": None, "trace": trace} if return_trace else None

        trace["raw_response"] = resp.content

        data = extract_json(resp.content)
        if not isinstance(data, dict):
            logger.warning(
                "[IntentGen] LLM 返回非 JSON（技能 %s）: %r",
                skill.name, (resp.content or "")[:120],
            )
            trace["error"] = f"LLM 返回非 JSON: {(resp.content or '')[:120]}"
            return {"result": None, "trace": trace} if return_trace else None

        # 过滤掉未注册的类别 id（避免路由盲区）
        raw_cats = data.get("intent_categories", []) or []
        cats: list[str] = []
        for c in raw_cats:
            cid = str(c).strip()
            if self.registry.get(cid) is not None and cid not in cats:
                cats.append(cid)
            else:
                logger.debug("[IntentGen] 忽略未注册/重复类别 %s", cid)

        free_kws = _normalize_keywords(data.get("intent_keywords", []))

        # 展开 domain 关键词 + 自由触发词 = 统一 intent_keywords
        domain_kws = self.registry.keywords_for_categories(cats)
        merged = list(domain_kws)
        for k in free_kws:
            if k not in merged:
                merged.append(k)

        result = {
            "intent_categories": cats,
            "intent_keywords": merged,
        }
        trace["result"] = result
        return {"result": result, "trace": trace} if return_trace else result

    def generate(self, skill, force: bool = False) -> Optional[dict]:
        """为单个技能生成意图词（兼容旧接口）。

        返回 {"intent_categories": [...], "intent_keywords": [...]} 或 None。
        - 技能已有意图声明且 force=False → 跳过返回 None（尊重人工配置）。
        - 调用 LLM；失败/超时/返回非 JSON → 返回 None（调用方回退到既有值）。
        """
        return self._generate_core(skill, force=force, return_trace=False)

    def generate_with_trace(self, skill, force: bool = False) -> dict:
        """为单个技能生成意图词，并返回完整交互过程（供前端可视化）。

        返回 {"result": dict|None, "trace": {...}}。trace 字段：
          - skill: 技能名
          - skipped: 是否因已有意图词而跳过
          - messages: 发送给 LLM 的 messages（system + user）
          - raw_response: LLM 原始返回文本
          - result: 解析后的 {intent_categories, intent_keywords}
          - error: 失败/跳过原因（成功时为 None）
        """
        return cast(dict, self._generate_core(skill, force=force, return_trace=True))
