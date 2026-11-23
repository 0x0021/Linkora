"""消息正文里的「媒体引用标记」清洗工具。

钉钉（DWS）把媒体资源引用塞在消息 ``content`` 里，形态有多种：

    [图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSwUj8GKV7o8vkK0PbPqr35AA)
    [视频消息](mediaId=@lQb...) fileName=x.mp4 url: ...
    [语音消息](mediaId=@lR_...) 注意：如需下载使用dws chat message download-media命令下载
    [文件](mediaId=...) fileId: bva6xxx 注意：如需下载使用dws drive download命令下载

这些串是**给 CLI / 下游工具读的元信息**，不是人写的字。把它们原样转发出去
（尤其再经 ``[:40]`` 之类的截断）会造成两个真实事故：

1. **mediaId 被截断成无效碎片**。mediaId 长达 46 字符，截断后是无效 ID，
   用它下载必然失败（钉钉返回 ``NO_PERMISSION`` / ``invalid``）。
2. **接收方把文本里的 mediaId 当成自己的资源去下载**。典型场景：Linkora
   自己用主人账号发出一条「待办通知」，正文里引用了对方的 mediaId；这条
   通知被轮询器回灌时，会被误判成「图片消息」并触发下载 —— 但那段 mediaId
   属于**另一个会话的别人的消息**，用本会话的 msgId 去换下载地址必然
   ``NO_PERMISSION``，且每轮轮询重复失败刷屏（2026-11-23 线上事故）。

故提供本模块，把媒体引用标记整段剥离，只留人话。

⚠️ 使用边界：**只用于「转述 / 摘要 / 转发」消息正文的场景**（如待办通知
文本、Web 列表摘要）。**不可**用于需要解析媒体 ID 去做下载的原始 content
（轮询器的 ``_extract_media_id`` 依赖原始 content 里的 mediaId 才能下载
原图），提前剥离会导致 OCR 拿不到图。
"""
from __future__ import annotations

import re

# 媒体引用标记的「资源 ID 段」：mediaId / fileId / file_id，值为不含空白与
# 闭合括号的任意非空串（钉钉的 ID 里可能含 ``-`` ``_`` ``=`` ``$`` ``@``）。
_RESOURCE_ID = r"[^\s)\]&]+"

# 整段剥离用的模式（按顺序应用）：
#  1. 媒体类型标记 + 其后的资源 ID：``[图片消息](mediaId=xxx)`` / ``[文件](mediaId=xxx)``
#  2. 裸资源 ID：``mediaId=xxx``（无标记包裹时）
#  3. DWS 塞的「工具提示」整句：``注意：如需下载...命令下载``
#  4. 裸文件名参数：``fileName=x.mp4``（URL 参数形态）
#
# ⚠️ 第 1 条要**整段**替换（不是只删 ID）：媒体 ID 本身没有可读性，整段换成
# 占位符才能表达「这里原本有张图」；只删 ID 会留下 ``[图片消息]()`` 这种空壳。
_MARKED_MEDIA_REF = re.compile(
    r"\[(?:图片消息|视频消息|语音消息|文件|图片)\]\s*\(\s*"
    rf"(?:mediaId|fileId|file_id)\s*=\s*{_RESOURCE_ID}\s*\)"
)

_MEDIA_REF_PATTERNS: tuple[str, ...] = (
    rf"\b(?:mediaId|file_?[Ii]d)\s*[:=]\s*{_RESOURCE_ID}",
    r"注意：如需下载.*?命令下载",
    r"注意：如需下载.*",
    r"\bfile_?[Nn]ame\s*[:=]\s*[^\s)\]&]+",
    r"\burl\s*[:=]\s*[^\s)\]&]+",
)

# 媒体类型标记残留（前面整段已剥，但可能出现无 ID 的裸标记，如 ``[图片消息]``）
_BARE_MARKER = re.compile(r"\[(?:图片消息|视频消息|语音消息|文件|图片)\]")

# 清理后可能残留的空括号 / 空白
#
# ⚠️ 只清「空括号」和成对括号里的空白，**不能**裸删所有方括号 ——
# 待办通知正文里有 ``[dingtalk] 张旭：`` 这种平台前缀，裸删方括号会把它
# 变成 ``dingtalk 张旭：``（丢了分组语义）。
_DANGLING = re.compile(r"\(\s*\)|\[\s*\]|\(\s+|\s+\)|\[\s+|\s+\]")

# 清洗后原位置留下的占位符（保留「这里原本有张图」的信息，而不是留个空洞）
_PLACEHOLDER = "[媒体]"

# 占位符保护哨兵：清理「残留空括号」前先把占位符换走，
# 否则 ``[媒体]`` 自身的方括号会被 _DANGLING 吃掉 → 变成「媒」。
_PLACEHOLDER_SENTINEL = "\x00MEDIA_PLACEHOLDER\x00"


def strip_media_refs(text: str, *, placeholder: str = _PLACEHOLDER) -> str:
    """把正文里的媒体引用标记整段剥离，只留人话。

    Args:
        text: 原始消息正文。
        placeholder: 剥离后原位置留下的占位符（默认 ``[媒体]``）；传空串则不留占位。

    Returns:
        清洗后的文本；输入为空时返回空串。

    幂等：对已清洗过的文本再跑一次不会改变结果。
    """
    if not text:
        return ""
    out = text
    # 带 ID 的媒体标记 → 整段换成哨兵占位符（保留「这里原本有张图」的信息）
    if placeholder:
        out = _MARKED_MEDIA_REF.sub(_PLACEHOLDER_SENTINEL, out)
    else:
        out = _MARKED_MEDIA_REF.sub(" ", out)
    # 其余噪声（裸 ID / 工具提示 / 文件名参数）→ 空格
    for pat in _MEDIA_REF_PATTERNS:
        out = re.sub(pat, " ", out)
    # 裸标记（如只有 ``[图片消息]`` 而无 ID）→ 换成占位符
    # ⚠️ 占位符先用哨兵保护：``[媒体]`` 自身的方括号会被下面的
    # _DANGLING（清理残留空括号）吃掉 → 只剩「媒」。
    if placeholder:
        out = _BARE_MARKER.sub(_PLACEHOLDER_SENTINEL, out)
    else:
        out = _BARE_MARKER.sub(" ", out)
    # 收拢残留空括号与空白
    out = _DANGLING.sub(" ", out).strip()
    if placeholder:
        # 占位符前后可能留下多余空格，压掉一次
        out = re.sub(r"\s{2,}", " ", out)
        out = out.replace(_PLACEHOLDER_SENTINEL, placeholder)
    return out.strip()


def has_media_ref(text: str) -> bool:
    """判断正文里是否含媒体引用标记（mediaId= / [图片消息] 等）。"""
    if not text:
        return False
    return bool(re.search(rf"\b(?:mediaId|file_?[Ii]d)\s*[:=]\s*{_RESOURCE_ID}", text)) or \
        bool(_BARE_MARKER.search(text))
