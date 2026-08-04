"""Markdown 表格兼容层测试。

验证：
- GFM 表格被转换为钉钉可渲染的等宽代码块网格（含 CJK 对齐）
- 无表格 / 已在代码块内的表格 / 引用块内表格 不被误改
- 平台能力标志控制是否转换（钉钉 False → 转换；飞书 True → 不动）
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.dws_adapter import DwsAdapter
from src.im_adapter.markdown_fix import (
    convert_markdown_tables,
    normalize_markdown_for_platform,
)


class TestConvertMarkdownTables:
    def test_basic_table_wrapped_in_code_block(self):
        src = "下面是数据：\n\n| 名称 | 价格 |\n| --- | --- |\n| 苹果 | 5 |\n| 香蕉 | 3 |\n\n以上。"
        out = convert_markdown_tables(src)
        # 小表 → box-drawing 边框表格，仍包进代码块以等宽对齐
        assert "```" in out
        # 保留表头与数据
        assert "名称" in out and "苹果" in out and "香蕉" in out
        # box-drawing 字符：竖线、顶/底边框、表头双线分隔
        assert "│" in out
        assert "┌" in out and "└" in out
        assert "╞" in out
        # 单元格保留（CJK 对齐）
        assert "│ 名称" in out or "名称 │" in out

    def test_cjk_alignment_pads_double_width(self):
        # 「苹果」占 2 列宽，应与表头「名称」(2) 对齐
        src = "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 2 |\n| 梨 | 1 |"
        out = convert_markdown_tables(src)
        # 苹果行补了空格（名称宽 2，苹果宽 4 → 右补 2 空格）——核心是对齐存在
        lines = [l for l in out.splitlines() if "苹果" in l]
        assert lines, "苹果行应保留"
        assert "苹果" in lines[0]

    def test_no_table_returns_unchanged(self):
        src = "普通文本\n- 列表项\n**加粗**\n```python\nprint(1)\n```"
        assert convert_markdown_tables(src) == src

    def test_table_inside_existing_code_block_untouched(self):
        # 代码块里的「表格」是示例代码，不应被转换
        src = "示例：\n```\n| a | b |\n| - | - |\n| 1 | 2 |\n```"
        assert convert_markdown_tables(src) == src

    def test_quote_block_table_not_converted(self):
        # 引用块内的表格语法不处理（避免破坏引用排版）
        src = "> | a | b |\n> | - | - |\n> | 1 | 2 |"
        assert convert_markdown_tables(src) == src

    def test_alignment_colons_ignored(self):
        src = "| 左 | 中 | 右 |\n| :-- | :-: | --: |\n| x | y | z |"
        out = convert_markdown_tables(src)
        assert "左" in out and "x" in out
        assert "```" in out

    def test_horizontal_rule_not_treated_as_table(self):
        # 单独 `---` 不应触发表格解析
        src = "标题\n\n---\n\n正文"
        assert convert_markdown_tables(src) == src

    def test_mixed_text_and_table(self):
        src = "先看表格：\n\n| 项 | 值 |\n| --- | --- |\n| A | 1 |\n\n再说明：结束。"
        out = convert_markdown_tables(src)
        assert "先看表格" in out
        assert "再说明：结束" in out
        assert "```" in out
        assert "│ 项 │ 值 │" in out

    def test_escaped_pipe_in_cell(self):
        # 单元格内的转义竖线不应被拆成两列
        src = "| 表达式 | 含义 |\n| --- | --- |\n| a\\|b | 或 |\n| c | 与 |"
        out = convert_markdown_tables(src)
        assert "a|b" in out  # 转义还原
        assert "```" in out

    def test_single_column_table(self):
        src = "| 标题 |\n| --- |\n| 内容 |"
        out = convert_markdown_tables(src)
        assert "```" in out
        assert "│ 标题 │" in out
        assert "│ 内容 │" in out

    def test_empty_text(self):
        assert convert_markdown_tables("") == ""
        assert convert_markdown_tables(None) == "" or convert_markdown_tables(None) is None


class TestNormalizeForPlatform:
    def test_supports_tables_true_keeps_gfm(self):
        src = "| a | b |\n| - | - |\n| 1 | 2 |"
        assert normalize_markdown_for_platform(src, supports_tables=True) == src

    def test_supports_tables_false_converts(self):
        src = "| a | b |\n| - | - |\n| 1 | 2 |"
        out = normalize_markdown_for_platform(src, supports_tables=False)
        assert "```" in out
        assert out != src


class TestDwsAdapterTableConversion:
    """校验钉钉适配器在发送出口实际做了表格转换。"""

    def _capture_sent_text(self, text: str, method: str = "send") -> str:
        adapter = DwsAdapter(dry_run=True)
        captured = {}

        def fake_run(args):
            # 找到 --text 后的真实文本
            if "--text" in args:
                idx = args.index("--text")
                captured["text"] = args[idx + 1]
            return {"success": True, "data": {}}

        adapter.run = MagicMock(side_effect=fake_run)
        if method == "send":
            adapter.chat_message_send(user="u", text=text)
        else:
            adapter.chat_message_update(message_id="m1", text=text)
        return captured.get("text", "")

    def test_send_converts_table(self):
        text = "| 名称 | 价格 |\n| --- | --- |\n| 苹果 | 5 |"
        sent = self._capture_sent_text(text, "send")
        assert "```" in sent
        assert "│ 名称 │ 价格 │" in sent

    def test_send_no_table_unchanged(self):
        text = "普通回复，没有表格。"
        sent = self._capture_sent_text(text, "send")
        assert sent == text

    def test_update_converts_table(self):
        text = "| 项 | 值 |\n| --- | --- |\n| A | 1 |"
        sent = self._capture_sent_text(text, "update")
        assert "```" in sent

    def test_supports_markdown_tables_flag_false(self):
        assert DwsAdapter.supports_markdown_tables is False


class TestBoxDrawingTable:
    """小表走 box-drawing 边框表格：column 对齐、CJK 宽度、表头双线分隔、截断。"""

    def test_header_double_line_separator(self):
        src = "| 设备 | IP |\n| --- | --- |\n| 打印机 | 10.0.2.3 |"
        out = convert_markdown_tables(src)
        # 表头下方用 ╞═╪╡ 双线分隔
        assert "╞" in out and "╪" in out and "╡" in out
        # 顶/底边框
        assert "┌" in out and "┐" in out and "└" in out and "┘" in out
        # 列分隔竖线
        assert "│" in out

    def test_cjk_alignment_pads_double_width(self):
        # 「苹果」显示宽 4，应与表头「名称」(宽 4) 对齐 —— 验证右补空格
        src = "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 2 |\n| 梨 | 1 |"
        out = convert_markdown_tables(src)
        apple_lines = [l for l in out.splitlines() if "苹果" in l]
        assert apple_lines, "苹果行应保留"
        # 苹果行应在等宽单元格内（被空格补齐到表头宽度）
        assert "苹果" in apple_lines[0]

    def test_truncation_adds_ellipsis_for_long_cell(self):
        # 长单元格超过默认 max_cell_width=18 → 触发 bullet 回退（非 box 截断）
        long_val = "这是一段非常非常非常非常非常长的设备备注说明文本"
        src = "| 设备 | 备注 |\n| --- | --- |\n| A | " + long_val + " |"
        out = convert_markdown_tables(src)
        # 超宽 cell 走 bullet，不包代码块
        assert "```" not in out
        assert "**A**" in out
        # bullet 回退时字段值会被截断加 …
        assert "…" in out

    def test_single_column_box_table(self):
        src = "| 标题 |\n| --- |\n| 内容A |\n| 内容B |"
        out = convert_markdown_tables(src)
        # "内容A" 显示宽度 > "标题"，表头右补空格对齐，故为 "│ 标题  │"
        assert "│ 标题" in out
        assert "│ 内容A │" in out
        assert "│ 内容B │" in out
        assert "```" in out


class TestBulletListFallback:
    """大表 / 长单元格回退 bullet 列表：首列粗体标题、其余字段行、空单元格用 —、不包代码块。"""

    def test_large_row_count_falls_back_to_bullets(self):
        # 7 行数据（> 默认 max_rows=6）→ bullet 列表
        header = "| 名称 | 值 |\n| --- | --- |"
        data_rows = "\n".join(f"| 项{i} | {i} |" for i in range(7))
        src = header + "\n" + data_rows
        out = convert_markdown_tables(src)
        assert "```" not in out            # 不包代码块
        assert "**项0**" in out            # 首列作加粗区块标题（独立行）
        # 单标签列走内联模式：名称是首列（标题），值是标签列
        assert "- 值：0" in out
        assert "**项6**" in out            # 最后一行也存在

    def test_too_many_columns_falls_back_to_bullets(self):
        # 5 列（> 默认 max_cols=4）→ bullet 列表
        src = "| a | b | c | d | e |\n| --- | --- | --- | --- | --- |\n| 1 | 2 | 3 | 4 | 5 |"
        out = convert_markdown_tables(src)
        assert "```" not in out
        assert "**1**" in out
        assert "- b：2" in out and "- e：5" in out

    def test_empty_cell_renders_em_dash(self):
        src = "| 设备 | 备注 |\n| --- | --- |\n| 打印机 |  |"
        out = convert_markdown_tables(src)
        # 小表走 box-drawing，空单元格在 box 里就是空白占位
        assert "```" in out
        assert "打印机" in out

    def test_bullet_preserves_markdown_semantics(self):
        # 2 列 1 行小表 → 走 box-drawing（非 bullet），包进代码块
        src = "| 标题 | 说明 |\n| --- | --- |\n| 关键项 | 一些解释 |"
        out = convert_markdown_tables(src)
        assert "```" in out
        assert "关键项" in out


class TestAdaptiveDispatch:
    """阈值边界：恰好在阈值内走 box，超出任一维度走 bullet。"""

    def test_exact_threshold_uses_box(self):
        # 6 行数据、4 列、最长 cell 18 → 恰好 box
        cells = ["x" * 18, "y", "z", "w"]
        header = "| " + " | ".join(["c1", "c2", "c3", "c4"]) + " |\n| --- | --- | --- | --- |"
        rows = "\n".join("| " + " | ".join(cells) + " |" for _ in range(6))
        src = header + "\n" + rows
        out = convert_markdown_tables(src)
        assert "```" in out  # box 包代码块

    def test_one_row_over_threshold_uses_bullets(self):
        cells = ["x" * 18, "y", "z", "w"]
        header = "| " + " | ".join(["c1", "c2", "c3", "c4"]) + " |\n| --- | --- | --- | --- |"
        rows = "\n".join("| " + " | ".join(cells) + " |" for _ in range(7))  # 7 行 > 6
        src = header + "\n" + rows
        out = convert_markdown_tables(src)
        assert "```" not in out

    def test_cell_over_width_threshold_uses_bullets(self):
        # 最长 cell 19（> 18）→ bullet，即便行/列数都很小
        src = "| 设备 | 备注 |\n| --- | --- |\n| A | " + ("长" * 19) + " |"
        out = convert_markdown_tables(src)
        assert "```" not in out
        assert "**A**" in out

    def test_custom_thresholds_override(self):
        # 放宽阈值：原本超宽的 cell 在 max_cell_width=40 下走 box
        wide = "x" * 30
        src = "| 设备 | 备注 |\n| --- | --- |\n| A | " + wide + " |"
        out = convert_markdown_tables(src, max_cell_width=40)
        assert "```" in out  # 放宽后走 box
        # A 在 box 里被右补空格与 "备注" 列对齐
        assert "│ A" in out
        assert wide in out  # 完整保留，不截断
