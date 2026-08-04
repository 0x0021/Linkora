"""图片本地存储路径工具（分平台+账号+会话隔离）。

设计目标
--------
把所有下载点（OCR、自发送图、飞书卡片内嵌图）的目录组织统一到同一套规范下，
避免「不同平台/账号/会话的图混在同一中文 chat_name 目录」导致的：

  1) 同名污染：钉钉「陈海艳」和飞书「陈海艳」是不同人，旧结构会把图全堆在
     ``data/tmp_images/陈海艳/`` 里，跨平台/人无法区分。
  2) 路径里出现 ``/``、``+``、``=`` 等字符（钉钉 chat_id 含 base64 字符）需安全转义。
  3) 迁移老数据时需可定位「这条消息对应的本地图片在哪」。

新结构
------
::

    data/tmp_images/<platform>/<account_id>/<chat_id>/<filename>

- ``platform``     ：``feishu`` / ``dingtalk`` / ``wecom``，与 DB 文件前缀一致。
- ``account_id``   ：当前登录账号的身份键，``account_identity.resolve_account_id``
                    解析出的 ``feishu:<open_id>`` / ``dingtalk:<corpId>`` 形式。
                    落库时取 ``:`` 前的 platform + ``:`` 后的 ID 拼盘，迁移脚本
                    支持显式覆盖。
- ``chat_id``      ：平台侧会话稳定 ID（飞书 ``oc_xxx``、钉钉 ``cidxxx``/``DDxxx``），
                    不再用 chat_name（含中文、可能重名、可能改名）。
- ``filename``     ：保留原有 ``ocr_<msg_id>.png`` / ``card_<key>.png`` 命名，
                    仍按出现顺序生成。

路径里所有「段」都经 :func:`safe_path_component` 转义：把不在
``[A-Za-z0-9_-]`` 也不在 CJK 区的字符替换为 ``_``，并截断到 80 字符，
确保在 Linux/macOS 文件系统、URL path、JSON 里都安全。

向后兼容
--------
旧 image_path 形如 ``<chat_name>/<file>``，没有平台/账号/会话 ID 段。
迁移脚本 :mod:`scripts.migrate_image_paths` 会按 chat_id 反查 + 当前 profile
重写为新结构，落库后所有 API / 前端 / ``/api/image/<rel>`` 路径都按新结构走。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# 段内允许的字符：A-Z a-z 0-9 _ - .（点，文件后缀需要） + CJK 统一汉字（U+4E00-U+9FFF）。
# 钉钉 chat_id 是 base64，含 `+` `/` `=`；飞书 chat_id 是 hex，无特殊字符。
# 把这些「不允许」字符全部替换为下划线，避免破坏目录结构或触发 shell 转义。
# 注意：单段内允许 `.`（后缀要用），但段边界与 `..` 由调用方用 Path 控制，禁止传入含 `/` 的段。
_SAFE_KEEP_RE = re.compile(r"[^A-Za-z0-9_\-.\u4e00-\u9fff]")

# 段长度上限：单段文件名在多数 FS（HFS+ 单段 ≤255，ext4 ≤255）下安全，
# 也避免 DB 字段被超长路径撑爆。
_MAX_SEGMENT_LEN = 80


def safe_path_component(s: object, *, fallback: str = "_") -> str:
    """把任意字符串转为可安全用作路径段的 ASCII/CJK 串。

    - ``None`` / 空 / 全非法字符 → ``fallback``
    - 非法字符（含 ``/``、``\\``、``+``、``=``、``%``、空格、中文标点）→ ``_``
    - 截断到 80 字符（防超长）
    - 保留汉字（``陈海艳`` → ``陈海艳``），便于运维 ``ls`` 时一眼看懂

    Examples
    --------
    >>> safe_path_component("陈海艳")
    '陈海艳'
    >>> safe_path_component("cidB75S9QNvDmZabnBuIdwWoVzEPClGZiWHpZfyYdmL5zQ=")
    'cidB75S9QNvDmZabnBuIdwWoVzEPClGZiWHpZfyYdmL5zQ_'
    >>> safe_path_component("oc_13c85f9a027902117d9063f2dc04f138")
    'oc_13c85f9a027902117d9063f2dc04f138'
    >>> safe_path_component(None)
    '_'
    """
    if s is None:
        return fallback
    text = str(s).strip()
    if not text:
        return fallback
    # 1) 把 ASCII 非法字符替换为 _；2) 截断长度
    out = _SAFE_KEEP_RE.sub("_", text)
    out = out[:_MAX_SEGMENT_LEN].rstrip("_")
    # 拒绝纯点 / 空段：避免 caller 用 ``safe_path_component("..")`` 拿到 ``..`` 触发
    # 路径穿越（虽然 image_subdir 用 Path 拼，本身不会穿，但多一道防线）
    if not out or out in (".", ".."):
        return fallback
    return out


def account_id_dir(account_id: str) -> str:
    """把 ``feishu:ou_xxx`` / ``dingtalk:corp123`` 这类 account_id 转成单段目录名。

    规则：取 ``:`` 后的真实 ID（最稳的部分），若没有 ``:`` 则整段当 ID。
    再经 :func:`safe_path_component` 二次保险。

    Examples
    --------
    >>> account_id_dir("feishu:ou_abc_123")
    'ou_abc_123'
    >>> account_id_dir("dingtalk:corp123")
    'corp123'
    >>> account_id_dir("corp123")
    'corp123'
    """
    if not account_id:
        return "_"
    head, sep, tail = str(account_id).partition(":")
    raw = tail if sep and tail else head
    return safe_path_component(raw)


def image_subdir(
    image_temp_dir: str | Path,
    platform: str,
    account_id: str,
    chat_id: str,
) -> Path:
    """返回某会话的图片子目录（绝对路径，已 resolve parent）。

    路径形如 ``<image_temp_dir>/<platform>/<account_id>/<chat_id>``。
    父目录不必存在——调用方按需 ``mkdir(parents=True, exist_ok=True)``。
    """
    base = Path(image_temp_dir).expanduser()
    return base / safe_path_component(platform) / account_id_dir(account_id) / safe_path_component(chat_id)


def image_rel_path(
    image_temp_dir: str | Path,
    platform: str,
    account_id: str,
    chat_id: str,
    filename: str,
) -> str:
    """返回 DB 里要存的「相对路径」字符串（posix 形式，正斜杠，相对 ``image_temp_dir``）。

    与 :func:`image_subdir` 区别：单数 + filename，且**始终返回相对 ``image_temp_dir`` 的路径**
    （``<platform>/<account_id>/<chat_id>/<filename>``），无论传入的 ``image_temp_dir`` 是绝对
    还是相对路径。前端用 ``/api/image/<rel>`` 取图时直接拼，后端 ``serve_image`` 用
    ``base / rel`` 还原绝对路径。

    ``filename`` 由调用方预生成（形如 ``ocr_<msg_id>.png`` / ``card_<key>.bin``），
    内部已用 safe_path_component 把 ``+``/``/``/``=`` 等字符清掉，本函数不再二次转义，
    避免 ``.png`` 后缀里的点被替换成下划线。
    """
    base = Path(image_temp_dir).expanduser()
    abs_path = (
        base
        / safe_path_component(platform)
        / account_id_dir(account_id)
        / safe_path_component(chat_id)
        / (filename or "image.bin")
    )
    # 始终输出相对 base 的 POSIX 正斜杠路径，避免 Windows 下反斜杠被前端当转义、
    # 也避免把 image_temp_dir 的绝对前缀写进 DB（否则 is_new_image_path 首段判定失效）。
    return abs_path.relative_to(base).as_posix()


def parse_image_rel_path(rel: str) -> Optional[dict]:
    """把 ``<platform>/<account_id>/<chat_id>/<filename>`` 拆成结构化字段。

    用于迁移脚本判断「这是不是新结构」：
    - 4 段且第 1 段是已知 platform → 新结构
    - 2 段（``<name>/<file>``）→ 旧结构，走迁移

    解析失败返回 ``None``（调用方按原值保留）。
    """
    if not rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 4:
        return None
    if parts[0] not in ("feishu", "dingtalk", "wecom"):
        return None
    return {
        "platform": parts[0],
        "account_id_dir": parts[1],
        "chat_id_dir": parts[2],
        "filename": "/".join(parts[3:]),
    }


def is_new_image_path(rel: str) -> bool:
    """判断 image_path 字符串是否已经是新结构（迁移前/后判定）。"""
    return parse_image_rel_path(rel) is not None
