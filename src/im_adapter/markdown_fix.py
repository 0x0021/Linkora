"""Markdown 表格兼容层（钉钉 / 企微等部分平台不渲染 GFM 表格）。

钉钉、企业微信的 markdown 子集只支持标题 / 加粗 / 引用 / 列表 / 链接 / 代码块，
**不支持** ``| 列1 | 列2 |`` 形式的 GFM 表格，渲染后会变成一堆竖线乱码。

本模块对 GFM 表格采用**双策略自适应**重排：

- **小表**（数据行 ≤ 6、列数 ≤ 4、最长单元格显示宽度 ≤ 18）→ 渲染为 box-drawing 边框表格
  （含表头双线分隔 ``╞═╪╡``、CJK 宽度对齐），并包进 ``` 代码块 ``` 利用钉钉等宽渲染对齐。
- **大表 / 长单元格** → 回退为 bullet 列表：首列作粗体条目标题，其余列以「标签：值」字段行
  （``｜`` 分隔）列出，避免代码块里竖线乱码与长 cell 折行崩格式。

飞书（Lark）原生支持 markdown 表格，故按平台能力标志 ``supports_markdown_tables``
决定是否转换（默认 True = 不转换，保持原生渲染）。
"""
from __future__ import annotations

import re
import unicodedata

# 分隔行单元格：仅由 `-` 与对齐 `:` 组成，至少含一个 `-`
_SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")

# 行内转义 `\|` 临时占位符，避免被误判为列分隔
_ESC_PIPE = "\x00PIPE\x00"


def _is_wide(ch: str) -> bool:
    """全角 / 宽字符（CJK、全角标点等）在等宽字体里占 2 列。"""
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _disp_width(s: str) -> int:
    """计算字符串的显示宽度（宽字符计 2）。"""
    return sum(2 if _is_wide(c) else 1 for c in s)


def _pad_right(s: str, width: int) -> str:
    """右侧补空格到指定显示宽度。"""
    return s + " " * max(0, width - _disp_width(s))


def _split_row(line: str) -> list[str]:
    """把一行 `| a | b |` 或 `a | b` 切成单元格并去空白。"""
    line = line.replace("\\|", _ESC_PIPE)
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [c.replace(_ESC_PIPE, "|").strip() for c in stripped.split("|")]
    return cells


def _is_sep_line(line: str) -> bool:
    """判断是否为 GFM 表格分隔行（如 `| --- | :--: | ---: |`）。"""
    if "|" not in line:
        return False
    cells = [c for c in _split_row(line) if c != ""]
    if not cells:
        return False
    return all(_SEP_CELL_RE.match(c) is not None for c in cells)


def _parse_table(block_lines: list[str]) -> list[list[str]] | None:
    """解析一组连续行，若为合法 GFM 表格则返回二维单元格，否则 None。"""
    if len(block_lines) < 2:
        return None
    header = _split_row(block_lines[0])
    if len(header) < 1 or any(c == "" for c in header):
        return None
    if not _is_sep_line(block_lines[1]):
        return None
    ncols = len(header)
    rows: list[list[str]] = [header]
    for raw in block_lines[2:]:
        cells = _split_row(raw)
        if len(cells) != ncols:
            # 列数与表头不一致 → 停止收集（安全，避免误吞普通文本）
            break
        if all(c == "" for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return None
    return rows


def _truncate(s: str, width: int) -> str:
    """按显示宽度截断到 width；超出则尾部加 …（省略号自身占 1 列）。"""
    if _disp_width(s) <= width:
        return s
    budget = width - 1  # 留 1 列给 …
    if budget <= 0:
        return "…"
    out: list[str] = []
    w = 0
    for ch in s:
        cw = 2 if _is_wide(ch) else 1
        if w + cw > budget:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def _render_box_table(rows: list[list[str]], *, max_cell_width: int) -> str:
    """把二维单元格渲染为 box-drawing 边框表格（不含代码块围栏）。

    含表头双线分隔 ``╞═╪╡``、CJK 宽度对齐、超宽单元格截断（``…``）。
    调用方负责包进 ``` 代码块以利用钉钉等宽渲染对齐。
    """
    ncols = len(rows[0])
    truncated = [[_truncate(c, max_cell_width) for c in r] for r in rows]
    widths = [0] * ncols
    for r in truncated:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], _disp_width(c))
    # 列内边距各 1 空格，故边框段长度 = 列宽 + 2
    hseg = lambda w: "─" * (w + 2)
    dseg = lambda w: "═" * (w + 2)

    def row_str(r: list[str]) -> str:
        cells = [" " + _pad_right(c, widths[i]) + " " for i, c in enumerate(r)]
        return "│" + "│".join(cells) + "│"

    parts = [
        "┌" + "┬".join(hseg(w) for w in widths) + "┐",
        row_str(truncated[0]),
        "╞" + "╪".join(dseg(w) for w in widths) + "╡",
    ]
    for r in truncated[1:]:
        parts.append(row_str(r))
    parts.append("└" + "┴".join(hseg(w) for w in widths) + "┘")
    return "\n".join(parts)


def _render_bullet_list(rows: list[list[str]], *, max_cell_width: int) -> str:
    """大表 / 长单元格回退：首列作加粗区块标题，其余列拆为独立子列表项。

    格式：每个数据行输出为 ``**标题**`` 独立行 + ``- 标签：值`` 子列表，
    区段之间以空行分隔。钉钉下各区块边界清晰、标题醒目。
    仅 1 个标签列时仍走内联 ``｜``（避免过度展开）。
    不包代码块——保留 markdown 列表 / 加粗语义，让钉钉正常渲染。
    """
    header = rows[0]
    label_cols = header[1:]
    items: list[str] = []
    inline_mode = len(label_cols) <= 1  # 单标签列 → 紧凑内联
    for r in rows[1:]:
        title = r[0] if r else ""
        if items:
            items.append("")  # 区段间空行，视觉边界清晰
        items.append(f"**{title}**")
        if label_cols:
            if inline_mode:
                # 单字段紧凑：设备：9F 研发打印机
                lab = label_cols[0]
                val = r[1] if len(r) > 1 else ""
                if not val:
                    val = "—"
                else:
                    val = _truncate(val, max_cell_width)
                items.append(f"- {lab}：{val}")
            else:
                # 多字段展开：每条独占子列表行，区段清晰
                for idx, lab in enumerate(label_cols):
                    val = r[idx + 1] if idx + 1 < len(r) else ""
                    if not val:
                        val = "—"
                    else:
                        val = _truncate(val, max_cell_width)
                    items.append(f"- {lab}：{val}")
    return "\n".join(items)


def _convert_segment(
    text: str,
    *,
    max_rows: int = 6,
    max_cols: int = 4,
    max_cell_width: int = 18,
) -> str:
    """转换一段「不含代码块」的文本中的所有 GFM 表格（双策略自适应）。"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 表格起点：当前行像表头（含 |）、且下一行是分隔行
        if (
            "|" in line
            and not line.lstrip().startswith(">")
            and i + 1 < n
            and _is_sep_line(lines[i + 1])
        ):
            block = [line, lines[i + 1]]
            j = i + 2
            while j < n and "|" in lines[j] and not lines[j].lstrip().startswith(">"):
                block.append(lines[j])
                j += 1
            parsed = _parse_table(block)
            if parsed is not None:
                n_data = len(parsed) - 1
                ncols = len(parsed[0])
                max_cell = max(_disp_width(c) for r in parsed for c in r)
                small = (
                    n_data <= max_rows
                    and ncols <= max_cols
                    and max_cell <= max_cell_width
                )
                # 表格前若非空文本，插入空行，避免钉钉把前文与列表/代码块粘在一起
                if out and out[-1].strip():
                    out.append("")
                if small:
                    out.append("```")
                    out.append(_render_box_table(parsed, max_cell_width=max_cell_width))
                    out.append("```")
                else:
                    out.append(_render_bullet_list(parsed, max_cell_width=max_cell_width))
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def convert_markdown_tables(
    text: str,
    *,
    max_rows: int = 6,
    max_cols: int = 4,
    max_cell_width: int = 18,
) -> str:
    """把文本中所有 GFM 表格转为钉钉可渲染的格式（双策略自适应）。

    - 小表 → box-drawing 边框表格（包进 ``` 代码块，等宽对齐）。
    - 大表 / 长单元格 → bullet 列表（保留 markdown 语义）。
    - 已位于 ``` 代码块内的内容原样保留（避免双重包裹 / 破坏示例代码）。
    - 文本无表格时原样返回（no-op），不影响其它 markdown。
    """
    if not text or "|" not in text:
        return text
    # 按 fenced code block 切分，仅处理块外文本
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    result: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            result.append(part)          # 代码块：原样保留
        else:
            result.append(
                _convert_segment(
                    part,
                    max_rows=max_rows,
                    max_cols=max_cols,
                    max_cell_width=max_cell_width,
                )
            )
    return "".join(result)


def normalize_markdown_for_platform(text: str, *, supports_tables: bool) -> str:
    """按平台能力规整 markdown。

    ``supports_tables=True``（如飞书）→ 原样返回；
    ``supports_tables=False``（如钉钉 / 企微）→ 转换表格为等宽代码块。
    """
    if supports_tables:
        return text
    return convert_markdown_tables(text)
