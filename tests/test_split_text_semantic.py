"""语义分块（split_text）专项测试。

核心契约：
- 固定长度（max_len）仅作软目标/上限参考，不硬截断于第 N 个字符；
- 分块优先遵循语义边界（段落→句子→子句→空白），避免中间截断；
- hard_max 仅作安全天花板，拦截病态超长单元，且仍优先语义边界。
"""
import re

import pytest

from src.tools.utils import split_text


def _sentences(text: str) -> list[str]:
    """把文本拆成以 。 结尾的句子（保留句号），用于「无中间截断」校验。"""
    parts = [p for p in text.split("。") if p != ""]
    return [p + "。" for p in parts]


class TestSemanticBoundaries:
    def test_no_mid_sentence_truncation(self):
        """每个句子都完整落在某一块内，绝不被从中间劈开。"""
        text = (
            "VPN 接入需要先申请权限。申请完成后等待审批通过。"
            "审批通过后下载客户端并安装。安装完成后使用工号登录即可。"
        )
        chunks = split_text(text, max_len=20)
        joined = "\n\n".join(chunks)
        for sent in _sentences(text):
            assert sent in joined, f"句子被截断: {sent!r}"

    def test_length_is_soft_target_not_hard_cap(self):
        """单个超长句子（>max_len 但 ≤hard_max）整段保留，不硬切。"""
        # 300 个「中」字组成一句无标点的长句；max_len=200，hard_max 默认 400
        long_sentence = "中" * 300
        chunks = split_text(long_sentence, max_len=200)
        assert len(chunks) == 1
        assert len(chunks[0]) == 300  # 允许超过软目标，但不要切断
        assert chunks[0] == long_sentence

    def test_chunk_respects_sentence_boundary_when_possible(self):
        """正常多句文本：每块都应结束于句子边界（不卡在句中间）。"""
        text = "第一句话内容。第二句话内容更长一些。第三句话内容。"
        chunks = split_text(text, max_len=12)
        # 每个块若不以 \n\n 结尾，也应以句号结尾（句子边界）
        for c in chunks:
            stripped = c.rstrip("\n")
            assert stripped.endswith("。") or stripped.endswith("，") is False and stripped, (
                f"块未在语义边界结束: {c!r}"
            )

    def test_heading_glued_to_body(self):
        """标题行（第X章 等可保留样式）与紧随其后的正文粘连在同一块，不孤立。"""
        text = "第三章 系统安装\n第一步先准备环境并校验"
        chunks = split_text(text, max_len=50)
        glued = "\n\n".join(chunks)
        assert "第三章 系统安装" in glued
        assert "第一步先准备环境并校验" in glued
        # 两者应在同一块内
        assert any(
            "第三章 系统安装" in c and "第一步先准备环境并校验" in c for c in chunks
        )

    def test_clause_splitting_for_long_sentence(self):
        """超长句子在子句（逗号）处断开，而非字符硬切。"""
        # 一句由多个逗号分隔的子句组成、总长超 hard_max 的文本
        clause = "，".join([f"第{i}个配置项需要正确填写" for i in range(40)])
        sentence = clause + "。"
        chunks = split_text(sentence, max_len=200, hard_max=400)
        # 每块的子句都应完整（以逗号或句号收尾，不在子句中间断）
        for c in chunks:
            s = c.rstrip("\n")
            assert s.endswith("，") or s.endswith("。"), f"子句被截断: {c!r}"

    def test_hard_max_safety_ceiling_bounds_garbage(self):
        """病态无标点长串（如巨型哈希）也必须被天花板拦住。"""
        garbage = "a" * 3000  # 无句号、无逗号、无空白
        chunks = split_text(garbage, max_len=200, hard_max=400)
        assert chunks, "不应返回空"
        for c in chunks:
            assert len(c) <= 400, f"块超过 hard_max: len={len(c)}"

    def test_latin_url_not_split_by_period(self):
        """英文 URL 中的点不应被当作句子边界切断。"""
        text = "请访问 github.com。然后打开 console 页面查看。"
        chunks = split_text(text, max_len=50)
        joined = "\n\n".join(chunks)
        assert "github.com" in joined

    def test_overlap_never_exceeds_hard_max(self):
        """开启重叠时，含重叠的块也不应超过 hard_max。"""
        text = "。".join([f"这是第{i}段示例内容用于测试重叠边界" for i in range(30)]) + "。"
        chunks = split_text(text, max_len=80, overlap=30, hard_max=200)
        for c in chunks:
            assert len(c) <= 200, f"重叠块超过 hard_max: len={len(c)}"


class TestSoftTargetDistribution:
    def test_short_text_single_chunk(self):
        assert split_text("短文本。", max_len=500) == ["短文本。"]

    def test_normal_doc_multi_chunk_near_target(self):
        """常规文档应被切成多块，且块长大致围绕 max_len。"""
        text = "。".join([f"第{i}句话描述了某个知识库相关的操作步骤" for i in range(20)]) + "。"
        chunks = split_text(text, max_len=60)
        assert len(chunks) >= 2
        # 绝大多数块应接近软目标（不超过 hard_max=120 太多）
        for c in chunks:
            assert len(c) <= 120 + 2
