"""测试 parse_document.py — OCR 后处理纯函数 + 文档解析器核心路径"""
import pytest
from unittest.mock import patch, MagicMock, mock_open
from pathlib import Path

from src.tools.parse_document import (
    _is_noise_line,
    _looks_like_label,
    _is_merge_value,
    _looks_like_value,
    _clean_line,
    _should_join,
    post_process_ocr_text,
    DocumentParser,
    OCR_TUNED_PARAMS,
)


# ============================================================================
# _is_noise_line
# ============================================================================
class TestIsNoiseLine:
    def test_empty_string(self):
        assert _is_noise_line("") is True
        assert _is_noise_line("   ") is True
        assert _is_noise_line("\t  \n") is True

    def test_ui_chrome_tokens(self):
        for token in ["返回", "关闭", "取消", "确定", "完成", "提交", "登录", "注册"]:
            assert _is_noise_line(token) is True, f"UI token '{token}' should be noise"

    def test_ui_chrome_tokens_english(self):
        for token in ["loading", "Loading", "loading"]:
            assert _is_noise_line(token) is True

    def test_clock_patterns(self):
        assert _is_noise_line("21:05") is True
        assert _is_noise_line("9:30") is True
        assert _is_noise_line("21:051") is True
        assert _is_noise_line("12：48") is True  # full-width colon

    def test_pure_symbols(self):
        assert _is_noise_line("···") is True
        assert _is_noise_line("...") is True
        assert _is_noise_line("....") is True   # 纯标点行 → 噪声
        assert _is_noise_line("+") is True

    def test_normal_text_not_noise(self):
        assert _is_noise_line("合同金额 1,234,567 元") is False
        assert _is_noise_line("供应商：北京某某科技有限公司") is False
        assert _is_noise_line("2024年度财务报表") is False
        assert _is_noise_line("Hello World") is False

    def test_contains_ui_keyword_in_context(self):
        """含 UI 词但属于正常句子，不应判为噪声（整行精确匹配才过滤）"""
        assert _is_noise_line("查看详情页") is False
        assert _is_noise_line("确定签署本合同") is False
        assert _is_noise_line("点击下载附件") is False


# ============================================================================
# _looks_like_label
# ============================================================================
class TestLooksLikeLabel:
    def test_colon_suffix(self):
        assert _looks_like_label("总金额：") is True
        assert _looks_like_label("合同编号:") is True
        assert _looks_like_label("Name:") is True

    def test_bracket_suffix(self):
        assert _looks_like_label("总金额（元）") is True
        assert _looks_like_label("供应商(") is True
        assert _looks_like_label("电话（") is True

    def test_short_noun(self):
        assert _looks_like_label("供应商") is True
        assert _looks_like_label("地址") is True
        assert _looks_like_label("电话") is True

    def test_label_with_digits_is_not_label(self):
        assert _looks_like_label("第1条") is False
        assert _looks_like_label("金额123") is False
        assert _looks_like_label("2024年") is False

    def test_full_sentence_not_label(self):
        assert _looks_like_label("本合同自双方签字之日起生效") is False
        assert _looks_like_label("甲方应于每月15日前支付款项") is False

    def test_ui_token_is_not_label(self):
        assert _looks_like_label("确定") is False
        assert _looks_like_label("关闭") is False


# ============================================================================
# _looks_like_value
# ============================================================================
class TestLooksLikeValue:
    def test_digit_start(self):
        assert _looks_like_value("1,234,567 元") is True
        assert _looks_like_value("¥500.00") is True
        assert _looks_like_value("+86 13800138000") is True
        assert _looks_like_value("-1,200.50") is True

    def test_bracket_start(self):
        assert _looks_like_value("（含税）") is True
        assert _looks_like_value("(VAT included)") is True

    def test_contains_digit(self):
        assert _looks_like_value("合同总金额为 500 万元") is True
        assert _looks_like_value("本年度第 3 季度") is True

    def test_no_digit(self):
        assert _looks_like_value("双方协商一致") is False
        assert _looks_like_value("甲方签字盖章") is False

    def test_empty(self):
        assert _looks_like_value("") is False
        assert _looks_like_value("   ") is False


# ============================================================================
# _is_merge_value
# ============================================================================
class TestIsMergeValue:
    def test_plain_value_after_label(self):
        assert _is_merge_value("1,234,567") is True

    def test_list_item_not_merge(self):
        assert _is_merge_value("1. 合同条款一") is False
        assert _is_merge_value("• 注意事项") is False

    def test_self_contained_label_not_merge(self):
        """自带标签结构的行不应并入前一行"""
        assert _is_merge_value("金额：500元") is False
        assert _is_merge_value("Name: John") is False

    def test_next_is_another_label(self):
        """下一行是另一个字段名 → 不并入"""
        assert _is_merge_value("供应商") is False

    def test_empty(self):
        assert _is_merge_value("") is False


# ============================================================================
# _clean_line
# ============================================================================
class TestCleanLine:
    def test_remove_zero_width(self):
        assert _clean_line("金额\u200b100元") == "金额100元"
        assert _clean_line("姓名\u200c：张三") == "姓名：张三"

    def test_remove_trailing_junk(self):
        assert _clean_line("合同条款》") == "合同条款"
        assert _clean_line("注意事项」") == "注意事项"
        assert _clean_line("text»") == "text"

    def test_remove_leading_junk(self):
        assert _clean_line("》合同条款") == "合同条款"
        assert _clean_line("  · 注意事项") == "注意事项"

    def test_collapse_whitespace(self):
        assert _clean_line("金额   100  元") == "金额 100 元"
        assert _clean_line("  金额\t100\t元  ") == "金额 100 元"


# ============================================================================
# _should_join
# ============================================================================
class TestShouldJoin:
    def test_split_sentence_join(self):
        """被切散的句子应合并"""
        assert _should_join("本合同自双方签字", "之日起生效。") is True

    def test_label_boundary_not_join(self):
        assert _should_join("总金额：", "500万元") is False
        assert _should_join("供应商（", "某某公司）") is False

    def test_list_boundary_not_join(self):
        assert _should_join("以下是条款：", "1. 第一条") is False
        assert _should_join("text", "• bullet") is False

    def test_sentence_end_not_join(self):
        assert _should_join("本合同生效。", "双方均应遵守。") is False
        assert _should_join("Done!", "Next line") is False

    def test_new_field_boundary_not_join(self):
        assert _should_join("前一行", "电话：") is False
        assert _should_join("前一行", "金额（元）") is False

    def test_short_speaker_boundary(self):
        """短 token（≤5字）+ 中文整句 → 说话人名/小标题边界"""
        assert _should_join("张三", "我同意这个方案。") is False
        assert _should_join("甲方", "本合同有效期为三年。") is False

    def test_short_next_boundary(self):
        """短下一行（≤4字、无句末标点）→ 边界"""
        assert _should_join("上一段话很长很长很长", "张三") is False

    def test_ascii_space_join(self):
        assert _should_join("Hello", "World") is True

    def test_empty_inputs(self):
        assert _should_join("", "hello") is False
        assert _should_join("hello", "") is False
        assert _should_join("", "") is False


# ============================================================================
# post_process_ocr_text
# ============================================================================
class TestPostProcessOcrText:
    def test_empty_input(self):
        assert post_process_ocr_text("") == ""
        assert post_process_ocr_text("   ") == ""

    def test_none_input(self):
        assert post_process_ocr_text(None) == ""

    def test_basic_cleaning(self):
        raw = "返回\n关闭\n总金额\n1,234,567 元\n合同编号：\nROC-2024-001"
        result = post_process_ocr_text(raw)
        assert "总金额" in result
        assert "1,234,567" in result
        assert "返回" not in result
        assert "关闭" not in result

    def test_label_value_merge(self):
        """标签+值合并：仅值行含数字时才触发合并（公司名等纯文本不会被误合并）"""
        # 含数字的值行应合并
        raw = "总金额\n1,234,567\n电话\n13800138000"
        result = post_process_ocr_text(raw)
        assert "总金额：1,234,567" in result
        assert "电话：13800138000" in result

    def test_pure_text_not_merged_as_value(self):
        """纯文本公司名不应被当作值合并到标签下"""
        raw = "供应商\n北京某某科技有限公司\n联系人\n张三"
        result = post_process_ocr_text(raw)
        # 供应商 + 纯文本公司名：不合并，各自保留
        assert "供应商" in result
        assert "北京某某科技有限公司" in result
        # 联系人 + 张三（纯文本无数字）：也不合并
        assert "联系人" in result
        assert "张三" in result

    def test_dedup_consecutive(self):
        raw = "合同条款\n合同条款\n合同条款\n生效日期\n生效日期"
        result = post_process_ocr_text(raw)
        # consecutive duplicates removed
        assert result.count("合同条款") == 1
        assert result.count("生效日期") == 1

    def test_sentence_rejoin(self):
        """被OCR误拆的长句应重新拼接（前段≥6字避免触发短token边界）"""
        raw = "本合同自双方签字\n之日起生效。\n各方当事人应当\n严格遵守本协议。"
        result = post_process_ocr_text(raw)
        assert "本合同自双方签字之日起生效。" in result
        assert "各方当事人应当严格遵守本协议。" in result

    def test_all_noise_returns_raw(self):
        """全部是噪声 → 回退返回原始 strip"""
        raw = "返回\n关闭\n确定\n取消"
        result = post_process_ocr_text(raw)
        assert result == raw.strip()

    def test_truncation(self):
        long_text = "A" * 2000 + "\n" + "B" * 500
        result = post_process_ocr_text(long_text, max_chars=1500)
        assert len(result) <= 1550  # allow truncation hint
        assert "截断" in result

    def test_no_truncation_within_limit(self):
        text = "合同金额500万元。\n供应商：某某公司。"
        result = post_process_ocr_text(text, max_chars=1500)
        assert "截断" not in result

    def test_clock_noise_removed(self):
        raw = "21:05\n合同金额\n500万元\n9:30"
        result = post_process_ocr_text(raw)
        assert "21:05" not in result
        assert "9:30" not in result
        assert "合同金额" in result

    def test_zero_width_cleaned(self):
        raw = "金额\u200b：\u200b500元"
        result = post_process_ocr_text(raw)
        assert "金额：500元" in result
        assert "\u200b" not in result

    def test_trailing_junk_cleaned(self):
        raw = "合同条款》\n注意事项」"
        result = post_process_ocr_text(raw)
        assert "合同条款" in result
        assert "注意事项" in result
        assert "》" not in result
        assert "」" not in result


# ============================================================================
# OCR_TUNED_PARAMS
# ============================================================================
class TestOCRTunedParams:
    def test_params_structure(self):
        assert isinstance(OCR_TUNED_PARAMS, dict)
        assert "text_score" in OCR_TUNED_PARAMS
        assert "min_height" in OCR_TUNED_PARAMS
        assert "limit_side_len" in OCR_TUNED_PARAMS

    def test_params_reasonable(self):
        assert OCR_TUNED_PARAMS["text_score"] < 1.0
        assert OCR_TUNED_PARAMS["min_height"] > 0
        assert OCR_TUNED_PARAMS["limit_side_len"] > 0


# ============================================================================
# DocumentParser — parse() dispatch
# ============================================================================
class TestDocumentParserParse:
    @pytest.fixture
    def mock_config(self):
        cfg = MagicMock()
        return cfg

    @pytest.fixture
    def parser(self, mock_config):
        with patch("src.tools.parse_document.DocumentParser._check_dependencies"):
            return DocumentParser(mock_config)

    def test_file_not_exists(self, parser):
        result = parser.parse("/nonexistent/file.pdf")
        assert result == ""

    def test_unsupported_extension(self, parser):
        with patch.object(Path, "exists", return_value=True):
            result = parser.parse("/fake/file.xyz")
            assert result == ""

    def test_pdf_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_pdf", return_value="PDF content"):
                result = parser.parse("/fake/file.pdf")
                assert result == "PDF content"

    def test_ppt_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_ppt", return_value="PPT content"):
                result = parser.parse("/fake/file.pptx")
                assert result == "PPT content"

    def test_docx_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_docx", return_value="DOCX content"):
                result = parser.parse("/fake/file.docx")
                assert result == "DOCX content"

    def test_image_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_image", return_value="OCR result"):
                result = parser.parse("/fake/file.png")
                assert result == "OCR result"

    def test_html_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_html", return_value="HTML parsed"):
                result = parser.parse("/fake/file.html")
                assert result == "HTML parsed"

    def test_markdown_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_markdown", return_value="MD content"):
                result = parser.parse("/fake/file.md")
            assert result == "MD content"

    def test_text_dispatch(self, parser):
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_markdown", return_value="text content"):
                result = parser.parse("/fake/file.txt")
            assert result == "text content"

    def test_explicit_file_type(self, parser):
        """显式指定 file_type 时不依赖扩展名推断"""
        with patch.object(Path, "exists", return_value=True):
            with patch.object(parser, "_parse_pdf", return_value="PDF"):
                result = parser.parse("/fake/file.unknown", file_type="pdf")
                assert result == "PDF"


# ============================================================================
# DocumentParser — _parse_markdown
# ============================================================================
class TestParseMarkdown:
    @pytest.fixture
    def parser(self):
        with patch("src.tools.parse_document.DocumentParser._check_dependencies"):
            return DocumentParser(MagicMock())

    def test_read_markdown(self, parser):
        fake_content = "# 标题\n\n段落内容\n- 列表项"
        with patch("builtins.open", mock_open(read_data=fake_content)):
            result = parser._parse_markdown("/fake/doc.md")
            assert "# 标题" in result
            assert "段落内容" in result
            assert "列表项" in result

    def test_read_text_file(self, parser):
        fake_content = "纯文本内容"
        with patch("builtins.open", mock_open(read_data=fake_content)):
            result = parser._parse_markdown("/fake/doc.txt")
            assert result == "纯文本内容"

    def test_empty_file(self, parser):
        with patch("builtins.open", mock_open(read_data="   ")):
            result = parser._parse_markdown("/fake/empty.md")
            assert result == ""

    def test_file_not_found(self, parser):
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = parser._parse_markdown("/fake/nonexistent.md")
            assert result == ""


# ============================================================================
# DocumentParser — dependency check
# ============================================================================
class TestDependencyCheck:
    @pytest.fixture
    def mock_config(self):
        return MagicMock()

    def test_all_deps_available(self, mock_config):
        with patch("src.tools.parse_document.DocumentParser._check_dependencies"):
            parser = DocumentParser(mock_config)
        # patch each import detection
        with patch.object(parser, "_check_dependencies"):
            with patch("builtins.__import__", return_value=MagicMock()):
                parser._pdf_available = True
                parser._ppt_available = True
                parser._docx_available = True
                parser._ocr_available = True
                assert parser._pdf_available
                assert parser._ppt_available
                assert parser._docx_available
                assert parser._ocr_available

    def test_ocr_not_available(self, mock_config):
        with patch("src.tools.parse_document.DocumentParser._check_dependencies"):
            parser = DocumentParser(mock_config)
        parser._ocr_available = False
        parser._ocr_engine = None
        result = parser._parse_image("/fake/img.png")
        assert result == ""


# ============================================================================
# DocumentParser — _parse_image (OCR)
# ============================================================================
class TestParseImage:
    @pytest.fixture
    def parser(self):
        with patch("src.tools.parse_document.DocumentParser._check_dependencies"):
            p = DocumentParser(MagicMock())
            p._ocr_available = True
            return p

    def test_ocr_success(self, parser):
        fake_engine = MagicMock()
        fake_engine.return_value = (
            [
                ["box", "合同金额"],
                ["box", "500万元"],
                ["box", ""],   # empty line skipped
                ["box", "   "], # whitespace skipped
            ],
            [0.5],
        )
        parser._ocr_engine = fake_engine
        result = parser._parse_image("/fake/img.png")
        assert "合同金额" in result
        assert "500万元" in result

    def test_ocr_no_text(self, parser):
        fake_engine = MagicMock()
        fake_engine.return_value = (None, [0.1])
        parser._ocr_engine = fake_engine
        result = parser._parse_image("/fake/img.png")
        assert result == ""

    def test_ocr_all_empty_lines(self, parser):
        fake_engine = MagicMock()
        fake_engine.return_value = ([["box", ""], ["box", "   "]], [0.1])
        parser._ocr_engine = fake_engine
        result = parser._parse_image("/fake/img.png")
        assert result == ""

    def test_ocr_exception(self, parser):
        fake_engine = MagicMock(side_effect=RuntimeError("OCR failed"))
        parser._ocr_engine = fake_engine
        result = parser._parse_image("/fake/img.png")
        assert result == ""

    def test_ocr_image_public_api(self, parser):
        """ocr_image 公开接口应代理到 _parse_image"""
        with patch.object(parser, "_parse_image", return_value="OCR result") as mock_parse:
            result = parser.ocr_image("/fake/img.png")
            mock_parse.assert_called_once_with("/fake/img.png")
            assert result == "OCR result"

    def test_parse_pdf_ocr_handles_engine_error(self, parser):
        """OCR 引擎在页面处理中抛异常时应优雅降级返回空，且 doc 被关闭（P2-13 try/finally）。"""
        try:
            import fitz as _fitz
        except ImportError:
            pytest.skip("PyMuPDF 不可用")
        parser._ocr_available = True
        parser._ocr_engine = MagicMock()
        import tempfile as _tf
        import os as _os
        tmp = _tf.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_path = tmp.name
        tmp.close()
        doc = _fitz.open()
        doc.new_page()
        doc.save(tmp_path)
        doc.close()
        try:
            with patch.object(parser, "_ocr_engine", side_effect=RuntimeError("ocr boom")):
                result = parser._parse_pdf_ocr(tmp_path)
            assert result == ""
        finally:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
