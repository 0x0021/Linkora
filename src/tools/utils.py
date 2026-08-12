from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
import logging

from ..rag_metrics import record_clean, record_chunk, snapshot as _rag_metrics_snapshot

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 不支持 flock
    fcntl = None


def safe_int(value, default: int) -> int:
    """安全解析整数：容忍字符串型数字、None、空串；非法值回退 default。

    LLM 可能传入 '5'、'五'、'3.7'、'3条' 等非纯 int 值，直接 int()/切片
    会抛 ValueError/TypeError 使工具崩溃（仅被工具执行中枢兜底捕获，
    用户侧表现为调用失败）。
    """
    if value is None or value == "":
        return default
    try:
        # 先用 float 兜住 '3.7'，再取整，避免 '3.7'/'3条' 直接 int() 报错
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def safe_float(value, default: float) -> float:
    """安全解析浮点数：容忍字符串型数字、None、空串；非法值回退 default。

    LLM 可能传入 '0.3'、'0.3以上'、None 等非纯 float 值，直接参与
    比较运算(如 score >= min_similarity)会抛 TypeError 使工具崩溃。
    """
    if value is None or value == "":
        return default
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def arg_str(args: dict, key: str, default: str = "") -> str:
    """从工具参数表安全取出字符串：容忍 None 与显式 null，统一 .strip()。

    工具参数解析三件套（arg_str / safe_int / safe_float）统一入口，避免各工具
    混用 `(args.get(k) or "").strip()` / `args.get(k, "")` / 手写 try 三种风格。
    """
    val = args.get(key)
    if val is None:
        return default
    return str(val).strip()


def list_result(raw, limit: int, **extra) -> dict:
    """把 dws 返回的列表规整为标准 {**extra, 'count', 'items'} 结构。

    供 wiki/oa_approval 等多列表工具共用，消除每处手写 `items[:limit]` 的重复；
    非 list 输入安全退回空列表。
    """
    items = raw if isinstance(raw, list) else []
    return {**extra, "count": len(items), "items": items[:limit]}


def _coerce_limit(value, default: int = 20) -> int:
    """把任意入参规整为非负 int 上限（n<1 回退 default），失败回退 default。

    LLM 可能传入 '5'、'五'、'3.7'、负数等非纯 int 值；负数或不可解析时回退 default，
    供 oa_approval/wiki 等列表工具统一使用，消除各模块重复实现。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return n


def _clean_text(text: str) -> str:
    """清洗文本，去除HTML标签、Markdown格式、多余空白等干扰内容。"""
    if not text:
        return ""

    text = text.strip()

    # 去除 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)

    # 去除 Markdown 格式
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    text = re.sub(r'~~([^~]+)~~', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'---+', '', text)
    text = re.sub(r'\*\s+', '', text)
    text = re.sub(r'-\s+', '', text)
    text = re.sub(r'\d+\.\s+', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    # 转换 Markdown 链接为纯文本（保留链接文本和 URL）
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)

    # 保留 URL，不删除
    # URL 在知识库中是有价值的信息，特别是对于导航页、API 文档等

    # 去除多余空白
    text = re.sub(r'[^\S\n]+', ' ', text)
    text = text.strip()

    return text


def clean_document_for_rag(
    content: str,
    *,
    llm_client=None,
    enable_llm: bool = True,
    max_chars: int = 8000,
    source_format: str = "auto",
) -> str:
    """对整篇文档做语义清洗，供 RAG 入库前分块使用。

    策略：
    - 正则仅作「轻量预清洗」（去 HTML 标签、压缩冗余空白），**保留 Markdown 结构**
      （标题、列表、代码块、链接文本），避免 _clean_text 那样把结构也剥掉。
    - 启用且有可用 LLMClient 时，调用 LLM（temperature=0）做语义精修：
      去导航/页眉页脚/广告/模板占位符、修明显格式错误，要求原样输出、不总结不改写。
    - 任何异常/空返回/清洗后过度缩短（< 预清洗 30%）→ 回退正则预清洗结果，绝不阻断流程。

    Args:
        content: 原始文档文本。
        llm_client: LLMClient 实例（src.llm.client.LLMClient）；None 则跳过 LLM。
        enable_llm: 是否启用 LLM 清洗（配置开关）。
        max_chars: 单次 LLM 清洗字符上限；超出按段落分片。
        source_format: 源格式（预留，当前未强制区分）。
    """
    if not content or not content.strip():
        return ""

    precleaned = _llm_preclean(content)

    if not (enable_llm and llm_client is not None):
        return precleaned

    start = time.monotonic()
    try:
        result = _llm_clean(precleaned, llm_client, max_chars=max_chars)
    except Exception as exc:  # 任意异常都回退，保证入库不中断
        logger.warning("LLM 文档清洗失败，回退正则预清洗: %s", exc)
        record_clean(
            fallback=True,
            chars_in=len(content),
            chars_out=len(precleaned),
            duration_ms=(time.monotonic() - start) * 1000,
        )
        if snapshot := _rag_metrics_snapshot():
            logger.info(
                "[RAG 清洗] LLM 清洗回退正则，当前累计回退率=%.1f%%（调用=%d）",
                snapshot["clean_fallback_rate"] * 100, snapshot["clean_calls"],
            )
        return precleaned
    record_clean(
        fallback=False,
        chars_in=len(content),
        chars_out=len(result),
        duration_ms=(time.monotonic() - start) * 1000,
    )
    return result


def _llm_preclean(content: str) -> str:
    """轻量正则预清洗：去 HTML 标签、压缩冗余空白，但保留 Markdown 结构与段落边界。

    与 _clean_text 不同，这里**不**剥离标题(# )、列表(- / 1. )、粗体等标记，
    因为这些结构对后续语义分块与检索有价值；同时保留段落间的空行（\\n\\n），
    便于后续超长文档按段落分片送入 LLM。
    """
    text = content.strip()
    # 去除 HTML 标签（保留标签间文本）
    text = re.sub(r"<[^>]+>", " ", text)
    # 常见 HTML 实体解码
    text = (text.replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&").replace("&quot;", '"'))
    # 按段落切分，段内压缩空白/去空行，段间保留 \n\n
    paragraphs = re.split(r"\n{2,}", text)
    cleaned: list[str] = []
    for para in paragraphs:
        lines = [re.sub(r"[ \t]+", " ", ln.rstrip()) for ln in para.split("\n")]
        lines = [ln for ln in lines if ln.strip()]
        if lines:
            cleaned.append("\n".join(lines))
    result = "\n\n".join(cleaned).strip()
    # 兜底：防止出现多于两个连续换行
    return re.sub(r"\n{3,}", "\n\n", result)


def _strip_fence(text: str) -> str:
    """剥离 LLM 可能返回的 markdown 代码围栏。"""
    t = text.strip()
    m = re.match(r"^```[^\n]*\n(.*)\n```$", t, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    if t.startswith("```") and t.endswith("```"):
        return t[3:-3].strip()
    return t


def _llm_clean(precleaned: str, llm_client, *, max_chars: int) -> str:
    """调用 LLM 对预清洗文本做语义精修。超长按段落分片。"""
    if len(precleaned) <= max_chars:
        result = _llm_clean_once(precleaned, llm_client)
    else:
        result = _llm_clean_long(precleaned, llm_client, max_chars=max_chars)

    result = _strip_fence(result).strip()
    if not result:
        raise ValueError("LLM 返回空结果")
    # 安全阀：清洗后过度缩短视为失败/过度改写，回退正则
    if len(result) < 0.3 * len(precleaned):
        raise ValueError(
            "LLM 清洗后长度过度缩短 (%d -> %d)，疑似丢失内容，回退"
            % (len(precleaned), len(result))
        )
    return result


def _llm_clean_once(text: str, llm_client) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "你是文档预处理助手，为后续 RAG 检索清洗文本。"
                "规则：1) 去除与正文无关的噪声（导航栏、页眉页脚、版权/广告、"
                "模板占位符如「请填写」「TODO」、重复横幅、多余空行）；"
                "2) 修正明显格式错误与乱码；"
                "3) 完整保留所有语义内容、标题层级、列表、代码块、表格与关键术语；"
                "4) 不得总结、不得改写、不得删减实质信息、不得添加任何解释或前言；"
                "5) 直接输出清洗后的文本本身。"
            ),
        },
        {
            "role": "user",
            "content": "请清洗以下文档内容：\n\n" + text,
        },
    ]
    resp = llm_client.chat(messages, temperature=0, stream=False)
    if isinstance(resp, dict):
        return resp.get("content", "") or ""
    return getattr(resp, "content", "") or ""


def _llm_clean_long(text: str, llm_client, *, max_chars: int) -> str:
    """超长文档：按段落切片，逐片调用 LLM 清洗；超单段上限的段保留原文。"""
    paragraphs = re.split(r"\n{2,}", text)
    out: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            try:
                out.append(_llm_clean_once(para, llm_client).strip())
            except Exception as exc:
                logger.warning("LLM 长文档分片清洗失败，回退该段原文: %s", exc)
                out.append(para)
        else:
            out.append(para)
    return "\n\n".join(out)


# ============================================================================
# 语义分块（RAG chunking）
# ----------------------------------------------------------------------------
# 设计目标：固定长度只作「软目标 / 上限参考」，不再硬截断于第 N 个字符；
# 分块优先遵循语义边界，避免把一句话从中间劈开。
#
# 边界优先级（从高到低）：
#   1) 段落 / 空行分隔（结构最强）
#   2) 句子结束符 ：。！？!?…  以及 ASCII 句号+空格（". "，避开 github.com 这类 URL）
#   3) 子句分隔符 ：，,；;：:、—–
#   4) 空白       ：拉丁长 token / URL 兜底
#   5) 字符级     ：CJK 无更细边界时的最后兜底（巨型 URL/哈希等病态输入）
# ============================================================================
# 语义边界正则（捕获分隔符，使其附在前一片段末尾，拼接时可无损还原）
_SENT_SPLIT = re.compile(r'([。！？!?…]|\.\s)')
_CLAUSE_SPLIT = re.compile(r'([，,；;：:、—–])')
_WS_SPLIT = re.compile(r'(\s+)')


def _atoms(text: str, pat: re.Pattern) -> list[str]:
    """按边界正则把文本切成原子片段，分隔符附在前一片段末尾（保留语义标点）。"""
    if not text:
        return []
    buf = ""
    atoms: list[str] = []
    for tok in pat.split(text):
        buf += tok
        if (pat.fullmatch(tok) or tok == "") and buf:
            atoms.append(buf)
            buf = ""
    if buf:
        atoms.append(buf)
    return [a for a in atoms if a]


def _pack_atoms(atoms: list[str], limit: int) -> list[str]:
    """把原子片段贪婪拼装成若干 ≤ limit 的块（仅在原子之间断开，原子内部不动）。"""
    out: list[str] = []
    cur = ""
    for a in atoms:
        if not cur:
            cur = a
        elif len(cur) + len(a) <= limit:
            cur += a
        else:
            out.append(cur)
            cur = a
    if cur:
        out.append(cur)
    return out


def _split_recursive(text: str, pats: tuple[re.Pattern, ...], hard_max: int) -> list[str]:
    """逐层细化切分：先按 pats[0] 切，超长片段再按 pats[1:] 切，直至每块 ≤ hard_max。

    若所有层级都用尽仍超长（如巨型无标点长串 / URL），退化为字符级兜底。
    每个片段内部永不在语义边界之外被切断。
    """
    if not pats:
        # 字符级兜底：CJK 可任意断；拉丁长 token 同理（已无更细边界）
        return [text[i:i + hard_max] for i in range(0, len(text), hard_max)]
    pat = pats[0]
    pieces = [a for a in _atoms(text, pat) if a]
    if all(len(p) <= hard_max for p in pieces):
        return pieces
    out: list[str] = []
    for p in pieces:
        if len(p) <= hard_max:
            out.append(p)
        else:
            out.extend(_split_recursive(p, pats[1:], hard_max))
    return out


def split_text(
    text: str,
    max_len: int = 500,
    overlap: int = 50,
    hard_max: int | None = None,
) -> list[str]:
    """语义分块：按 结构→句子→子句 等语义边界切分，固定长度仅作软目标/上限参考。

    相对旧实现的关键变化：
    - **长度不再硬限制**：``max_len`` 是「软目标 / 上限参考」。块尽量贴近该长度，
      但**不切断语义单元**——一个句子 / 子句 / 段落即使略超 ``max_len`` 也整段保留，
      不再于第 N 个字符处硬截断（避免「中间截断」丢失上下文）。
    - **遵循语义**：优先在 段落空白 → 句子结束 → 子句分隔 → 空白 的边界断开。
    - **硬上限（安全天花板）** ``hard_max``：仅用于拦截病态输入（如超长无标点段落、
      巨型 URL / 哈希）。达到 ``hard_max`` 时仍优先在最佳语义边界断开，仅当无更细
      边界才字符级兜底。默认 ``max(max_len * 2, 800)``。
      *建议*：将该值设在所用 embedding 模型有效字符容量之下，可彻底杜绝模型侧截断。
    - 标题行（# 标题 / 第X章 / 1. xxx / 一、xxx）仍与紧随其后的正文粘连，避免孤立。

    >>> split_text("第一段。第二段。", max_len=5)
    ['第一段。', '第二段。']
    """
    if not text:
        record_chunk(count=0)
        return []

    hard_max = hard_max or max(max_len * 2, 800)
    SEP = "\n\n"

    text = _clean_text(text)

    # —— 预处理：标题行与下一行粘连，保证标题不孤立 ——
    raw_lines = text.split("\n")
    heading_re = re.compile(
        r'^\s*(#{1,6}\s+\S|第[一二三四五六七八九十0-9]+[章篇节卷部]'
        r'|[0-9]+[.、)]\s*\S|[一二三四五六七八九十]+[、.]\s*\S)'
    )
    blocks: list[str] = []
    i = 0
    while i < len(raw_lines):
        s = raw_lines[i].strip()
        if not s:
            i += 1
            continue
        if heading_re.match(s) and i + 1 < len(raw_lines):
            nxt = raw_lines[i + 1].strip()
            if nxt:
                blocks.append(s + "\n" + nxt)
                i += 2
                continue
        blocks.append(s)
        i += 1

    # —— 每个 block 先切成语义子单元（句子级起步，超长单元再逐层细化）——
    units: list[str] = []
    for blk in blocks:
        units.extend(_split_recursive(blk, (_SENT_SPLIT, _CLAUSE_SPLIT, _WS_SPLIT), hard_max))

    # —— 按软目标贪婪拼装：达到 max_len 即换块，但绝不切断一个语义单元 ——
    # cur_len 以「当前块 + 一个待加分隔符」计（与旧实现 flush 时机一致）。
    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    for u in units:
        if not current:
            current = [u]
            cur_len = len(u) + len(SEP)
        elif cur_len + len(SEP) + len(u) <= max_len:
            current.append(u)
            cur_len += len(SEP) + len(u)
        else:
            chunks.append(SEP.join(current))
            current = [u]
            cur_len = len(u) + len(SEP)
    if current:
        chunks.append(SEP.join(current))

    # —— 重叠：取上一块尾部作为上下文前缀，且整体不超 hard_max ——
    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for idx in range(1, len(chunks)):
            prev = chunks[idx - 1]
            max_ov = hard_max - len(SEP) - len(chunks[idx])
            if max_ov <= 0:
                overlapped.append(chunks[idx])
                continue
            tail = prev[-max_ov:] if len(prev) > max_ov else prev
            overlapped.append(tail + SEP + chunks[idx])
        chunks = overlapped

    record_chunk(count=len(chunks))
    return chunks


@contextlib.contextmanager
def cross_process_lock(lock_name: str, workdir: str | None = None):
    """跨进程互斥锁（基于 fcntl.flock），防止多 bot 实例并发执行同一耗时任务。

    典型用途：数据库备份、钉钉文档同步——这些任务若被两个进程同时执行，
    会造成重复备份/互相清理、文档重复同步、双倍 embedding 配额浪费。

    语义（建议配合调用方进程内 threading.Lock 串行化，避免同进程自我冲突）：
    - 获取成功（yield True）：执行临界区工作；退出时自动释放锁。
    - 已被其他进程持有（yield False）：调用方应跳过本次执行，不做任何工作。
    - 不支持 flock 的平台（如 Windows）退化为无跨进程保护（yield True），
      不影响单实例正常运行。
    """
    if fcntl is None:
        yield True
        return
    lock_dir = workdir or tempfile.gettempdir()
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"dingtalk-{lock_name}.lock")
    lock_file = open(lock_path, "w")
    acquired = False
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield True
    except OSError:
        # 其他进程已持有锁：不阻塞，交由调用方跳过本次执行。
        yield False
    finally:
        if acquired:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError as _exc:
                # 释放失败不影响主流程（进程退出时 OS 自动回收），记一条即可
                logger.warning("cross_process_lock: 释放锁失败: %s", _exc)
        try:
            lock_file.close()
        except OSError:
            pass
