"""LLM 结构化输出（JSON）稳健解析工具 —— 全项目唯一真源。

背景：让 LLM「只输出 JSON」在实际厂商模型上从来不可靠，常见污染形态有

1. markdown 围栏包裹：```json\\n{...}\\n```
2. 前后缀寒暄：「好的，以下是评分结果：{...} 希望对你有帮助」
3. 思考链标签：<think>...</think>{...}（部分推理模型未走 reasoning_content 通道）
4. 空内容：推理模型把正文全塞进 reasoning_content，content 为空串

裸 `json.loads(text)` 遇到 1/2/3/4 会一律抛
``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``，把「模型措辞不规范」
误报成「程序崩溃」。本模块提供 `extract_json()` 统一兜底，调用方只需判 None。

使用约定：**任何解析 LLM JSON 输出的地方都应调用本模块，禁止裸 `json.loads`。**
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 推理模型偶尔把思考链以标签形式混在 content 里，解析前整段剔除
_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
# 未闭合的思考标签（被 max_tokens 截断时出现）：从标签处截掉后半段
_THINK_OPEN_RE = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """剔除 content 里混入的 <think>/<thinking>/<reasoning> 思考链片段。"""
    if not text:
        return ""
    s = _THINK_RE.sub("", text)
    m = _THINK_OPEN_RE.search(s)
    if m:
        s = s[: m.start()]
    return s.strip()


def extract_json(text: str | None) -> Any | None:
    """从 LLM 返回中稳健提取首个 JSON 对象/数组。

    处理顺序：去思考链 → 去 markdown 围栏 → 整体 json.loads → 逐字符 raw_decode
    扫描取首个 dict/list（可跨越任意前缀噪声）。

    返回 None 表示无法解析（调用方负责降级，不要让异常冒泡）。
    """
    if not text:
        return None
    s = strip_reasoning(text)
    if not s:
        return None
    # 去 markdown 围栏（```json ... ``` / ``` ... ```）
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # 整体解析（最常见的干净情形，快速路径）
    try:
        obj = json.loads(s)
        if isinstance(obj, (dict, list)):
            return obj
    except json.JSONDecodeError:
        pass
    # 逐段 raw_decode 取首个 JSON（吃掉任意前缀噪声：围栏残留、寒暄、序号等）
    dec = json.JSONDecoder()
    idx, n = 0, len(s)
    while idx < n:
        if s[idx] not in "{[":
            idx += 1
            continue
        try:
            obj, _ = dec.raw_decode(s, idx)
            if isinstance(obj, (dict, list)):
                return obj
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("json parse fallback: %s", e)
        idx += 1
    return None


def extract_last_json(text: str | None) -> Any | None:
    """从混合文本中提取**最后一个**合法 JSON 对象/数组。

    与 ``extract_json`` 语义互补：extract_json 取首个（LLM 输出场景，第一个
    JSON 即目标）；本函数取最后一个——适用于 CLI stdout 等「先有进度条/安装
    提示噪声、后接 JSON 响应，或末尾还跟非 JSON 内容」的场景（如 lark-cli /
    dws 子进程输出），返回最后一次成功解析的 dict/list。

    返回 None 表示无法解析（调用方负责降级）。
    """
    if not text:
        return None
    s = strip_reasoning(text)
    if not s:
        return None
    dec = json.JSONDecoder()
    last_data: Any | None = None
    idx, n = 0, len(s)
    while idx < n:
        if s[idx] not in "{[":
            idx += 1
            continue
        try:
            obj, end = dec.raw_decode(s, idx)
            if isinstance(obj, (dict, list)):
                last_data = obj
                idx += end  # 跳过已解析的完整 JSON，避免嵌套子对象被重复识别
                continue
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("json parse fallback: %s", e)
        idx += 1
    return last_data
