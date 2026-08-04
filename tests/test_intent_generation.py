"""AI 意图词生成器测试。

覆盖：
- extract_json 的健壮解析（markdown 围栏 / 前后缀噪声 / 非法输入）
- IntentGenerator 的领域映射 + 关键词展开合并
- 跳过已有意图词（无 force）/ 覆盖（force）
- 过滤未注册类别
- LLM 调用失败/超时的优雅降级（返回 None）
- SkillManager.generate_intents 端到端写回 SKILL.md
"""
from __future__ import annotations


from src.intent import LAYER_DOMAIN
from src.llm.client import LLMResponse
from src.skills.intent_generator import (
    IntentGenerator,
    extract_json,
    _normalize_keywords,
)
from src.skills.loader import Skill


# ── 测试替身 ────────────────────────────────────────────────

class _Cat:
    def __init__(self, cid, keywords):
        self.id = cid
        self.name = cid
        self.definition = cid
        self.layer = LAYER_DOMAIN
        self.evidence_keywords = keywords


class FakeRegistry:
    def __init__(self):
        self._cats = {
            "domain.weather": _Cat("domain.weather", ["天气", "气温", "温度"]),
            "domain.web_search": _Cat("domain.web_search", ["搜索", "查询"]),
        }

    def all(self):
        return list(self._cats.values())

    def get(self, cid):
        return self._cats.get(cid)

    def keywords_for_categories(self, cids):
        out: list[str] = []
        for c in (cids or []):
            cat = self._cats.get(c)
            if cat:
                for k in cat.evidence_keywords:
                    if k not in out:
                        out.append(k)
        return out


class FakeClient:
    """可配置返回内容的假 LLM 客户端。"""

    def __init__(self, content: str | None = None, raise_exc: Exception | None = None):
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list = []

    def chat(self, messages, temperature=None, **kwargs):
        self.calls.append(messages)
        if self.raise_exc is not None:
            raise self.raise_exc
        return LLMResponse(
            content=self.content,
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )


def _make_skill(name="demo", description="演示技能", intent_categories=None, intent_keywords=None, body="# 正文"):
    return Skill(
        name=name,
        description=description,
        body=body,
        intent_categories=intent_categories or [],
        intent_keywords=intent_keywords or [],
    )


# ── extract_json ────────────────────────────────────────────

def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_fenced():
    text = '```json\n{"intent_categories": ["domain.weather"], "intent_keywords": ["今天天气"]}\n```'
    obj = extract_json(text)
    assert obj["intent_categories"] == ["domain.weather"]


def test_extract_json_with_surrounding_text():
    text = '好的，这是结果：\n{"intent_keywords": ["搜索"]}\n以上。'
    assert extract_json(text) == {"intent_keywords": ["搜索"]}


def test_extract_json_invalid_returns_none():
    assert extract_json("完全不是 json 的一堆文字") is None
    assert extract_json("") is None
    assert extract_json(None) is None


# ── _normalize_keywords ─────────────────────────────────────

def test_normalize_keywords_dedup_lowercase():
    out = _normalize_keywords(["天气", "天气", "Weather", 123, "", " 查询 "])
    assert out == ["天气", "weather", "查询"]


# ── IntentGenerator.generate ───────────────────────────────

def test_generate_maps_domain_and_expands():
    client = FakeClient('{"intent_categories": ["domain.weather"], "intent_keywords": ["今天天气", "气象"]}')
    gen = IntentGenerator(client, FakeRegistry())
    res = gen.generate(_make_skill())
    assert res is not None
    # 领域关键词被展开
    assert "天气" in res["intent_keywords"]
    assert "气温" in res["intent_keywords"]
    assert "温度" in res["intent_keywords"]
    # 自由触发词被合并
    assert "今天天气" in res["intent_keywords"]
    assert "气象" in res["intent_keywords"]
    assert res["intent_categories"] == ["domain.weather"]


def test_generate_no_domain_match_keeps_free_keywords():
    client = FakeClient('{"intent_categories": [], "intent_keywords": ["查资料", "找文档"]}')
    gen = IntentGenerator(client, FakeRegistry())
    res = gen.generate(_make_skill())
    assert res["intent_categories"] == []
    assert set(res["intent_keywords"]) == {"查资料", "找文档"}


def test_generate_filters_unregistered_category():
    client = FakeClient('{"intent_categories": ["domain.nope", "domain.weather"], "intent_keywords": ["x"]}')
    gen = IntentGenerator(client, FakeRegistry())
    res = gen.generate(_make_skill())
    assert res["intent_categories"] == ["domain.weather"]


def test_generate_skips_existing_without_force():
    client = FakeClient('{"intent_keywords": ["应被忽略"]}')
    gen = IntentGenerator(client, FakeRegistry())
    skill = _make_skill(intent_keywords=["已有词"])
    assert gen.generate(skill, force=False) is None
    assert client.calls == []  # 根本没调用 LLM


def test_generate_force_overrides_existing():
    client = FakeClient('{"intent_categories": ["domain.weather"], "intent_keywords": ["重新生成"]}')
    gen = IntentGenerator(client, FakeRegistry())
    skill = _make_skill(intent_keywords=["已有词"])
    res = gen.generate(skill, force=True)
    assert res is not None
    assert "重新生成" in res["intent_keywords"]
    assert "天气" in res["intent_keywords"]  # 领域展开依然生效


def test_generate_llm_failure_returns_none():
    client = FakeClient(raise_exc=RuntimeError("timeout"))
    gen = IntentGenerator(client, FakeRegistry())
    assert gen.generate(_make_skill()) is None


def test_generate_empty_content_returns_none():
    client = FakeClient(content="")
    gen = IntentGenerator(client, FakeRegistry())
    assert gen.generate(_make_skill()) is None


def test_generate_non_json_returns_none():
    client = FakeClient(content="模型跑题了，没给 JSON")
    gen = IntentGenerator(client, FakeRegistry())
    assert gen.generate(_make_skill()) is None


# ── SkillManager 端到端 ─────────────────────────────────────

def test_manager_generate_intents_writes_back(tmp_path, monkeypatch):
    from src.skills.manager import SkillManager
    import src.skills.loader as loader

    skills_dir = tmp_path / "data" / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 天气助手\n---\n# 天气\n查天气\n",
        encoding="utf-8",
    )
    # _SKILL_DIRS 在模块加载时固化，测试需临时 patch（monkeypatch 自动还原，避免污染后续用例）
    monkeypatch.setattr(loader, "_SKILL_DIRS", [str(tmp_path / "data" / "skills")])

    mgr = SkillManager(tmp_path)
    mgr.reload()
    assert mgr.get("demo") is not None

    client = FakeClient(
        '{"intent_categories": ["domain.weather"], "intent_keywords": ["今天天气", "气象"]}'
    )
    result = mgr.generate_intents(
        client=client, registry=FakeRegistry(), force=True, persist=True
    )
    assert result["total"] == 1
    assert result["generated"] == 1

    # 写回校验
    content = (skills_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "intent_keywords" in content
    assert "天气" in content          # 领域展开词已写入
    assert "今天天气" in content      # 自由词已写入
    # 内存对象同步
    assert "天气" in mgr.get("demo").intent_keywords


def test_manager_generate_intents_skips_existing_by_default(tmp_path, monkeypatch):
    from src.skills.manager import SkillManager
    import src.skills.loader as loader

    skills_dir = tmp_path / "data" / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 天气助手\nintent_keywords:\n  - 已有词\n---\n正文\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_SKILL_DIRS", [str(tmp_path / "data" / "skills")])

    mgr = SkillManager(tmp_path)
    mgr.reload()
    client = FakeClient('{"intent_keywords": ["应被忽略"]}')
    result = mgr.generate_intents(client=client, registry=FakeRegistry(), force=False)
    assert result["skipped"] == 1
    assert result["generated"] == 0
    assert client.calls == []


# ── IntentGenerator.generate_with_trace ───────────────────

def test_generate_with_trace_success_has_messages_and_result():
    client = FakeClient('{"intent_categories": ["domain.weather"], "intent_keywords": ["今天天气"]}')
    gen = IntentGenerator(client, FakeRegistry())
    out = gen.generate_with_trace(_make_skill())
    assert out["result"] is not None
    trace = out["trace"]
    assert trace["skipped"] is False
    assert trace["error"] is None
    assert len(trace["messages"]) == 2
    assert trace["messages"][0]["role"] == "system"
    assert trace["messages"][1]["role"] == "user"
    assert "天气" in trace["result"]["intent_keywords"]
    assert trace["raw_response"] == client.content


def test_generate_with_trace_skipped_when_existing():
    client = FakeClient('{"intent_keywords": ["x"]}')
    gen = IntentGenerator(client, FakeRegistry())
    out = gen.generate_with_trace(_make_skill(intent_keywords=["已有"]), force=False)
    assert out["result"] is None
    assert out["trace"]["skipped"] is True
    assert out["trace"]["error"] is not None


def test_generate_with_trace_error_on_llm_failure():
    client = FakeClient(raise_exc=RuntimeError("boom"))
    gen = IntentGenerator(client, FakeRegistry())
    out = gen.generate_with_trace(_make_skill(), force=True)
    assert out["result"] is None
    assert out["trace"]["error"] is not None
    assert "boom" in out["trace"]["error"]


# ── SkillManager.generate_intents_trace ───────────────────

def test_manager_generate_intents_trace_writes_back(tmp_path, monkeypatch):
    from src.skills.manager import SkillManager
    import src.skills.loader as loader

    skills_dir = tmp_path / "data" / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 天气助手\n---\n# 天气\n查天气\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_SKILL_DIRS", [str(tmp_path / "data" / "skills")])
    mgr = SkillManager(tmp_path)
    mgr.reload()

    client = FakeClient(
        '{"intent_categories": ["domain.weather"], "intent_keywords": ["今天天气"]}'
    )
    res = mgr.generate_intents_trace(client=client, name="demo", force=True, persist=True)
    assert res["found"] is True
    assert res["written"] is True
    assert res["result"]["intent_categories"] == ["domain.weather"]
    # trace 字段齐全
    assert res["trace"]["messages"]
    assert res["trace"]["raw_response"] == client.content
    # SKILL.md 已写入 domain 展开词
    content = (skills_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "intent_keywords" in content
    assert "天气" in content
