"""技能数据模型与 SKILL.md 解析器。

技能文件（SKILL.md）格式：
    ---
    name: web-search
    description: 网络搜索与网页内容获取。
    allowed-tools: WebFetch, Bash
    intent_keywords:
      - 搜索
      - 查询
    weight: 0.7
    ---

    # 技能正文

技能的发现路径（优先级从高到低）：
    1. {project_root}/data/skills/{name}/SKILL.md  （主路径）
    2. {project_root}/.agents/skills/{name}/SKILL.md（兼容旧路径）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.paths import data_path, get_app_root

logger = logging.getLogger(__name__)

# ── 技能发现目录 ──────────────────────────────────────────────
# 1) 用户数据目录下的 skills（可写，Web 安装/克隆落这里）— 可重定位
# 2) 捆绑资源下的内置 skills（冻结态 _MEIPASS/data/skills，开发态仓库 data/skills）
# 3) .agents/skills（cwd 相对，兼容旧路径）
_SKILL_DIRS = [
    str(data_path("skills")),
    str(get_app_root() / "data" / "skills"),
    ".agents/skills",
]
_SKILL_FILE = "SKILL.md"


@dataclass
class Skill:
    """单个技能的完整描述。"""

    name: str
    description: str
    body: str                          # SKILL.md 正文（Markdown）
    allowed_tools: list[str] = field(default_factory=list)
    source_path: str = ""              # SKILL.md 的绝对路径
    intent_keywords: list[str] = field(default_factory=list)  # 意图关键词（用于智能路由，向后兼容/兜底）
    intent_categories: list[str] = field(default_factory=list)  # 声明的意图类别（Phase 1 单一真源）
    weight: float = 0.5                # 路由权重（0.0-1.0），越高越优先
    enabled: bool = True               # 技能开关（false 时 router 跳过该技能）
    fallback_tools: list[str] = field(default_factory=list)  # 故障时回退的内置工具名
    config: dict | None = None         # config.yaml 解析后的配置字典
    has_config: bool = False           # 是否存在 config.yaml
    composable: bool = False           # 是否允许与其他 composable 技能组合激活（Phase 3）
    platforms: list[str] = field(default_factory=list)  # 适用平台列表（空=通用）

    def prompt_section(self) -> str:
        """生成注入到 system prompt 的技能简介片段。"""
        return (
            f"- **{self.name}**: {self.description}"
        )

    @property
    def effective_intent_keywords(self) -> list[str]:
        """路由用的有效意图关键词（Phase 1 单一真源解析点）。

        - 声明了 intent_categories → 经 IntentRegistry 解析为对应域类别证据词；
        - 否则回退到字面/自动推导的 intent_keywords（向后兼容）。
        """
        if self.intent_categories:
            try:
                from src.intent import default_registry
                return default_registry.keywords_for_categories(self.intent_categories)
            except Exception as _exc:
                logger.warning(f"effective_intent_keywords: swallowed exception: {_exc}")
                return list(self.intent_keywords)
        return list(self.intent_keywords)

    @property
    def semantic_text(self) -> str:
        """用于语义向量化的文本（Phase 2 语义路由）。

        组合 name + description + effective_intent_keywords，
        使「同义改写 / 错别字 / 口语」也能与技能语义对齐。
        """
        kws = self.effective_intent_keywords
        return " ".join([self.name, self.description, " ".join(kws)]).strip()


class SkillLoader:
    """从项目根目录扫描并加载所有 SKILL.md。"""

    def __init__(self, project_root: str | Path):
        self._root = Path(project_root).resolve()

    def discover(self) -> list[str]:
        """返回所有技能目录的绝对路径列表（去重）。"""
        seen: set[str] = set()
        dirs: list[str] = []

        for rel in _SKILL_DIRS:
            base = self._root / rel
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                if entry.is_dir() or entry.is_symlink():
                    resolved = str(entry.resolve())
                    if resolved not in seen:
                        seen.add(resolved)
                        dirs.append(resolved)
        return dirs

    def load(self, skill_dir: str) -> Skill | None:
        """加载单个技能目录下的 SKILL.md。

        返回 Skill 对象，解析失败返回 None。
        """
        skill_md = Path(skill_dir) / _SKILL_FILE
        if not skill_md.is_file():
            logger.warning("技能目录 %s 缺少 SKILL.md", skill_dir)
            return None

        try:
            raw = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("无法读取 %s: %s", skill_md, e)
            return None

        frontmatter, body = self._split_frontmatter(raw)

        try:
            meta = yaml.safe_load(frontmatter) if frontmatter else {}
        except yaml.YAMLError as e:
            logger.warning("%s YAML 解析失败: %s", skill_md, e)
            return None

        if not isinstance(meta, dict):
            logger.warning("%s frontmatter 不是字典", skill_md)
            return None

        name = meta.get("name", "").strip()
        if not name:
            logger.warning("%s 缺少 name 字段", skill_md)
            return None

        raw_tools = meta.get("allowed-tools", "")
        if isinstance(raw_tools, str):
            allowed_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]
        elif isinstance(raw_tools, list):
            allowed_tools = [str(t).strip() for t in raw_tools if str(t).strip()]
        else:
            allowed_tools = []

        # 解析意图关键词（外部 skill 可能不提供，需自动推导）
        raw_kw = meta.get("intent_keywords", [])
        if isinstance(raw_kw, list):
            intent_keywords = [str(k).strip() for k in raw_kw if str(k).strip()]
        elif isinstance(raw_kw, str):
            intent_keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]
        else:
            intent_keywords = []

        # 解析意图类别（Phase 1 单一真源）：技能声明服务哪些 domain.* 意图类别，
        # 路由时经 IntentRegistry 解析出关键词，避免与工具重复维护场景词。
        raw_cats = meta.get("intent_categories", [])
        if isinstance(raw_cats, list):
            intent_categories = [str(c).strip() for c in raw_cats if str(c).strip()]
        elif isinstance(raw_cats, str):
            intent_categories = [c.strip() for c in raw_cats.split(",") if c.strip()]
        else:
            intent_categories = []

        # 若无显式 intent_keywords 且未声明 intent_categories，从 description + tags 自动提取
        # （已声明 intent_categories 的技能不再自动推导，避免噪声词污染路由）
        if not intent_keywords and not intent_categories:
            intent_keywords = SkillLoader._derive_keywords(meta, body)

        # 解析权重
        try:
            weight = float(meta.get("weight", 0.5))
            weight = max(0.0, min(1.0, weight))
        except (TypeError, ValueError) as _exc:
            logger.debug(f"load: swallowed exception: {_exc}")
            weight = 0.5

        # 解析启用状态
        enabled = True
        if "enabled" in meta:
            enabled = bool(meta["enabled"])

        # 解析 composable（Phase 3 组合激活开关）：只有显式声明 composable: true 的技能
        # 才允许在与主激活技能 score 接近平局时被组合激活，避免噪声技能被连带激活。
        composable = bool(meta.get("composable", False))

        # 解析 fallback_tools（技能故障时回退的内置工具）
        raw_fb = meta.get("fallback_tools", [])
        if isinstance(raw_fb, list):
            fallback_tools = [str(t).strip() for t in raw_fb if str(t).strip()]
        elif isinstance(raw_fb, str):
            fallback_tools = [t.strip() for t in raw_fb.split(",") if t.strip()]
        else:
            fallback_tools = []

        # 解析 platforms（通用技能不设或为空，平台专属设 [dingtalk] 等）
        raw_platforms = meta.get("platforms", [])
        if isinstance(raw_platforms, list):
            platforms = [str(p).strip() for p in raw_platforms if str(p).strip()]
        elif isinstance(raw_platforms, str):
            platforms = [p.strip() for p in raw_platforms.split(",") if p.strip()]
        else:
            platforms = []

        return Skill(
            name=name,
            description=meta.get("description", "").strip(),
            body=body.strip(),
            allowed_tools=allowed_tools,
            source_path=str(skill_md),
            intent_keywords=intent_keywords,
            intent_categories=intent_categories,
            weight=weight,
            enabled=enabled,
            fallback_tools=fallback_tools,
            composable=composable,
            platforms=platforms,
            config=self._load_config(skill_dir),
            has_config=(Path(skill_dir) / "config.yaml").is_file(),
        )

    def save_intent(
        self,
        skill: "Skill",
        intent_categories: list[str],
        intent_keywords: list[str],
    ) -> bool:
        """将生成的意图词回写到 SKILL.md frontmatter，并同步更新内存中的 skill 对象。

        返回是否成功写入。复用与 update_skill_meta 一致的 frontmatter 重写范式。
        安全策略：若 AI 未匹配到任何 domain 类别（intent_categories 为空），
        则保留技能原有的 intent_categories，避免清空人工配置导致路由退化。
        """
        skill_md_path = Path(skill.source_path)
        if not skill_md_path.exists():
            logger.warning("[IntentGen] SKILL.md 不存在: %s", skill_md_path)
            return False
        try:
            content = skill_md_path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                logger.warning("[IntentGen] SKILL.md 缺少 frontmatter: %s", skill_md_path)
                return False
            second_delim = content.find("---", 3)
            if second_delim == -1:
                logger.warning("[IntentGen] SKILL.md frontmatter 格式异常: %s", skill_md_path)
                return False
            frontmatter_str = content[3:second_delim]
            body = content[second_delim + 3:]

            fm = yaml.safe_load(frontmatter_str) or {}
            if not isinstance(fm, dict):
                return False

            # 保护人工配置的类别：AI 未产出类别时不清空
            final_cats = list(intent_categories) if intent_categories else list(skill.intent_categories)
            fm["intent_keywords"] = list(intent_keywords)
            if final_cats:
                fm["intent_categories"] = final_cats

            new_frontmatter = yaml.dump(
                fm, allow_unicode=True, default_flow_style=False, sort_keys=False
            ).strip()
            new_content = f"---\n{new_frontmatter}\n---{body}"
            skill_md_path.write_text(new_content, encoding="utf-8")

            # 同步内存对象（无需立即 reload 即可生效）
            skill.intent_keywords = list(intent_keywords)
            if final_cats:
                skill.intent_categories = list(final_cats)
            logger.info(
                "[IntentGen] 已写回意图词: %s (%d 关键词, %d 类别)",
                skill.name, len(intent_keywords), len(final_cats),
            )
            return True
        except Exception as e:
            logger.error("[IntentGen] 写回意图词失败 %s: %s", skill.name, e, exc_info=True)
            return False

    @staticmethod
    def _load_config(skill_dir: str) -> dict | None:
        """加载技能目录下的 config.yaml（可选）。"""
        config_file = Path(skill_dir) / "config.yaml"
        if not config_file.is_file():
            return None
        try:
            raw = config_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("无法解析 %s: %s", config_file, e)
            return None

    @staticmethod
    def _derive_keywords(meta: dict, body: str) -> list[str]:
        """从 description + tags + body 标题自动提取意图关键词。

        策略：
        1. 从 description 提取 2-4 字的中文词组和英文单词
        2. 从 tags 直接取
        3. 从 body 前几行的 ## 标题提取
        4. 去重、过滤停用词、限制 15 个
        """
        import re

        stop_words = {
            "可以", "使用", "支持", "进行", "一个", "通过", "用于", "这个",
            "the", "a", "an", "is", "are", "for", "with", "and", "or",
            "this", "that", "to", "of", "in", "on", "it",
        }

        keywords: list[str] = []

        # 1. 从 description 提取关键词
        desc = meta.get("description", "")
        # 先按标点/空格切分，避免跨句滑动窗口产生碎片
        desc_segments = re.split(r"[,，;；。！？\s]+", desc)
        for seg in desc_segments:
            seg = seg.strip()
            if not seg:
                continue
            # 对每个纯中文段落，提取 2-4 字重叠滑动窗口，确保 "搜索" "引擎" 等子串被捕获
            cn_only = "".join(ch for ch in seg if "\u4e00" <= ch <= "\u9fff")
            for i in range(len(cn_only)):
                for length in [2, 3, 4]:
                    chunk = cn_only[i:i+length]
                    if len(chunk) == length and chunk not in stop_words and chunk not in keywords:
                        keywords.append(chunk)
            # 也保留原段落中的混合文本（含英文/数字的 token）
            for token in re.findall(r"[^\s,，;；。！？]+", seg):
                token = token.strip()
                if len(token) >= 2 and not re.search(r"[\u4e00-\u9fff]", token):
                    if token.lower() not in stop_words and token not in keywords:
                        keywords.append(token)

        # 从 description 提取英文单词（≥3 字母）
        en_words = re.findall(r"[a-zA-Z]{3,}", desc)
        for w in en_words:
            wl = w.lower()
            if wl not in stop_words and wl not in keywords:
                keywords.append(wl)

        # 2. 从 tags
        tags = meta.get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                t = str(t).strip().lower()
                if t and t not in keywords and len(t) >= 2:
                    keywords.append(t)

        # 3. 从 body 前几行的 ## 标题提取
        body_lines = body.strip().split("\n")
        for line in body_lines[:10]:
            line = line.strip()
            if line.startswith("##"):
                title = line.lstrip("#").strip()
                cn_title = re.findall(r"[\u4e00-\u9fff]{2,4}", title)
                for chunk in cn_title:
                    if chunk not in stop_words and chunk not in keywords:
                        keywords.append(chunk)

        # 去重并按长度排序（2 字词优先，匹配用户输入概率更高），限制 25 个
        cn_kw = [k for k in keywords if re.search(r"[\u4e00-\u9fff]", k)]
        en_kw = [k for k in keywords if k not in cn_kw]
        cn_kw.sort(key=lambda x: len(x))  # 短词优先
        return (cn_kw + en_kw)[:25]

    @staticmethod
    def _split_frontmatter(raw: str) -> tuple[str, str]:
        """将 SKILL.md 拆分为 YAML frontmatter 和正文。"""
        lines = raw.split("\n")
        if not lines or lines[0].strip() != "---":
            return "", raw

        end = 0
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break

        if end == 0:
            return "", raw

        frontmatter = "\n".join(lines[1:end])
        body = "\n".join(lines[end + 1:])
        return frontmatter, body
