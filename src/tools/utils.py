from __future__ import annotations

import contextlib
import os
import re
import tempfile
import logging

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


def split_text(text: str, max_len: int = 500, overlap: int = 50) -> list[str]:
    """将长文本按段落和句子切分为指定长度的 chunks，支持重叠。

    改进（Feature C）：标题行（# 标题 / 第X章 / 1. xxx / 一、xxx）会与紧随其后的
    正文粘连为一个段落，避免标题独占一块、正文被切到下一块导致语义割裂。
    自动清洗 HTML 标签、Markdown 格式等干扰内容，提高检索精度。
    """
    if not text:
        return []

    text = _clean_text(text)
    # —— 预处理：标题行与下一行粘连，保证标题不孤立 ——
    raw_lines = text.split("\n")
    heading_re = re.compile(
        r'^\s*(#{1,6}\s+\S|第[一二三四五六七八九十0-9]+[章篇节卷部]'
        r'|[0-9]+[.、)]\s*\S|[一二三四五六七八九十]+[、.]\s*\S)'
    )
    paras: list[str] = []
    i = 0
    while i < len(raw_lines):
        s = raw_lines[i].strip()
        if not s:
            i += 1
            continue
        if heading_re.match(s) and i + 1 < len(raw_lines):
            nxt = raw_lines[i + 1].strip()
            if nxt:
                paras.append(s + "\n" + nxt)
                i += 2
                continue
        paras.append(s)
        i += 1

    chunks = []
    current = ""
    for para in paras:
        if len(current) + len(para) + 2 <= max_len:
            current += para + "\n\n"
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > max_len:
                sentences = [s.strip() for s in para.replace("。", "。\n").split("\n") if s.strip()]
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) <= max_len:
                        current += sent
                    else:
                        if current:
                            chunks.append(current.strip())
                        current = sent
            else:
                current = para + "\n\n"
    if current:
        chunks.append(current.strip())

    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            curr = chunks[i]
            overlap_text = prev[-overlap:] if len(prev) > overlap else prev
            overlapped.append(overlap_text + curr)
        chunks = overlapped

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
