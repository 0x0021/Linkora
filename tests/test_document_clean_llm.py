"""clean_document_for_rag（LLM 语义清洗）专项测试。

核心契约：
- 正则仅作轻量预清洗，**保留 Markdown 结构**（标题/列表/粗体等），与 _clean_text 行为不同；
- 有可用 LLMClient 且启用时，调用 LLM 做语义精修；
- LLM 不可用 / 异常 / 返回空 / 过度缩短 → 回退正则预清洗，绝不抛错阻断流程；
- 超长文档按段落分片清洗，不崩。
"""
from src.tools.utils import clean_document_for_rag


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """最小化 LLMClient 替身：chat() 返回固定文本（或 callable(messages)->str）或抛错。"""

    def __init__(self, content="清洗后的内容", side_effect=None):
        self._content = content
        self._side_effect = side_effect
        self.calls = 0
        self.last_messages = None

    def chat(self, messages, temperature=None, stream=False):
        self.calls += 1
        self.last_messages = messages
        if self._side_effect is not None:
            raise self._side_effect
        text = self._content(messages) if callable(self._content) else self._content
        return _FakeResp(text)


def test_no_client_falls_back_to_regex_and_keeps_structure():
    content = "# 一级标题\n## 二级标题\n- 列表项A\n- 列表项B\n1. 编号项\n**加粗文本**\n<p>HTML段落</p>"
    out = clean_document_for_rag(content, llm_client=None, enable_llm=True)
    # 结构保留：标题/列表/编号/加粗文本都不应被剥掉
    assert "# 一级标题" in out
    assert "## 二级标题" in out
    assert "- 列表项A" in out
    assert "- 列表项B" in out
    assert "1. 编号项" in out
    assert "加粗文本" in out
    # HTML 标签被去除，文本保留
    assert "<p>" not in out and "HTML段落" in out


def test_disabled_llm_never_calls_client():
    content = "# 标题\n正文"
    llm = _FakeLLM()
    out = clean_document_for_rag(content, llm_client=llm, enable_llm=False)
    assert llm.calls == 0
    assert "# 标题" in out  # 正则预清洗仍保留结构


def test_normal_llm_output_returned_and_fence_stripped():
    content = "# 标题\n正文内容"
    llm = _FakeLLM("```markdown\n清洗干净的文本\n```")
    out = clean_document_for_rag(content, llm_client=llm, enable_llm=True)
    assert llm.calls == 1
    assert out == "清洗干净的文本"
    # system prompt 强约束「不总结/不改写/保留结构」
    assert any("不得总结" in m["content"] for m in llm.last_messages)


def test_llm_exception_falls_back_to_regex():
    content = "# 标题\n正文内容 <b>HTML</b>"
    llm = _FakeLLM(side_effect=RuntimeError("model down"))
    out = clean_document_for_rag(content, llm_client=llm, enable_llm=True)
    assert llm.calls == 1  # 确实尝试过
    assert "# 标题" in out  # 回退到正则预清洗


def test_llm_over_short_result_falls_back():
    # 预清洗后较长，但 LLM 返回极短 -> 视为丢失内容，回退
    content = "<p>" + "正文内容" * 40 + "</p>"
    llm = _FakeLLM("短")
    out = clean_document_for_rag(content, llm_client=llm, enable_llm=True)
    assert "正文内容" in out  # 回退结果含原文，而非 "短"


def test_empty_content_returns_empty():
    assert clean_document_for_rag("", llm_client=None) == ""
    assert clean_document_for_rag("   \n  ", llm_client=_FakeLLM()) == ""


def test_long_document_split_and_cleaned_per_paragraph():
    # 每段 <= max_chars，但总长超 max_chars -> 分片逐段清洗
    paras = ["段落{} 内容 {}".format(i, "字" * 20) for i in range(5)]
    content = "\n\n".join(paras)

    # 模拟真实 LLM：返回与原文等长的清洗文本（不触发「过度缩短」回退）
    def _same_len(messages):
        para = messages[-1]["content"].split("\n\n", 1)[1]
        return "x" * len(para)

    llm = _FakeLLM(content=_same_len)
    out = clean_document_for_rag(content, llm_client=llm, enable_llm=True, max_chars=50)
    # 5 段各调用一次 LLM
    assert llm.calls == 5
    # 每段都被清洗（原文 "段落N" 不应出现，已被 LLM 输出替换）
    assert "段落0" not in out
    assert "段落4" not in out


def test_single_paragraph_exceeding_max_chars_kept_as_is():
    # 单段就超过 max_chars：无法安全送入，保留原文（正则预清洗已处理）
    big = "超长段落 " + "数据 " * 200  # 远超 50
    llm = _FakeLLM()
    out = clean_document_for_rag(big, llm_client=llm, enable_llm=True, max_chars=50)
    assert llm.calls == 0  # 单段超限，不调用 LLM
    assert "超长段落" in out
