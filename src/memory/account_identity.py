"""多平台账号身份解析 —— 用于会话数据的账号级隔离。

为什么需要：重登录更换账号后，旧 chat_id/open_id 被直接用于新账号的 API 请求，
飞书返回 ``open_id cross app``。根因是会话相关表只有平台级 chat_id、没有账号维度。
本模块在运行时解析「当前登录账号身份」，作为会话数据命名空间（per-account DB）的键。

各平台身份来源：
  - feishu    : ``lark-cli whoami`` → appId（app 命名空间，open_id 作用域）
  - dingtalk  : ``dws profile list`` 解析激活组织 corpId（配置 target_org_corp_id 兜底）
  - wecom     : ~/.config/wecom/mcp_config.enc 内容 sha256（加密文件，不解密任何密钥；
                文件变更=换 bot=键变=自动隔离；缺失时 fallback "wecom"）

设计约束：
  - 解析失败（CLI 未装/未登录/超时）只记 warning 并返回兜底键，**绝不阻断启动**。
  - 进程内缓存（账号不会在进程内突变；re-login 视为重启/重探测事件）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# 各平台 CLI 二进制发现顺序（which 优先，其次已知路径兜底）
_CLI_FALLBACKS = {
    "feishu": ["lark-cli", "/opt/homebrew/bin/lark-cli"],
    "dingtalk": [
        "dws",
        # 允许通过环境变量覆盖 dws 二进制位置（部署到非开发者机器时），
        # 不再硬编码任何本机绝对路径，避免「which 找不到 + 路径不存在」时
        # 钉钉账号恒解析为 dingtalk:unknown、per-account 隔离被破坏。
        os.environ.get("DWS_BIN", ""),
    ],
    "wecom": ["wecom-cli", "/opt/homebrew/bin/wecom-cli"],
}

_WECHAT_CFG_CANDIDATES = [
    os.path.expanduser("~/.config/wecom/mcp_config.enc"),
    os.path.expanduser("~/.config/wecom/bot.enc"),
]

_CACHE: dict[str, str] = {}


def _find_cli(platform: str) -> Optional[str]:
    for cand in _CLI_FALLBACKS.get(platform, []):
        if not cand:
            continue
        if os.path.sep in cand or cand.startswith("~"):
            path = os.path.expanduser(cand)
            if os.path.exists(path) and os.access(path, os.X_OK):
                return path
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def _run(cli: str, args: list[str], timeout: int = 15) -> Optional[str]:
    try:
        r = subprocess.run(
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode != 0:
            logger.debug("[账号身份] %s %s 退出码 %d: %s", cli, args, r.returncode, r.stderr[:200])
            return None
        return r.stdout
    except subprocess.TimeoutExpired:
        logger.warning("[账号身份] %s %s 超时（%ds）", cli, args, timeout)
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("[账号身份] %s %s 执行异常: %s", cli, args, e)
        return None


def _resolve_feishu() -> str:
    cli = _find_cli("feishu")
    if not cli:
        logger.warning("[账号身份] 未找到 lark-cli，飞书账号键回退 feishu:unknown")
        return "feishu:unknown"
    out = _run(cli, ["whoami"])
    if not out:
        return "feishu:unknown"
    try:
        data = json.loads(out)
        app_id = data.get("appId") or data.get("profile")
        if app_id:
            return f"feishu:{app_id}"
    except json.JSONDecodeError:
        logger.debug("[账号身份] lark-cli whoami 非 JSON: %s", out[:200])
    # 退路：从文本里抠 appId/cli_xxx
    import re
    m = re.search(r"(cli_[0-9a-f]+|app[0-9a-f]+)", out)
    if m:
        return f"feishu:{m.group(1)}"
    return "feishu:unknown"


def _resolve_dingtalk(fallback_corp_id: Optional[str] = None) -> str:
    """解析当前激活钉钉组织的真实 corpId。

    ``dws profile list`` 输出是 JSON，真实 corpId 在 ``primaryProfile`` /
    ``currentProfile`` / ``profiles[].corpId`` 字段里（形如 ``ding9888...``）。
    注意：JSON 的键名也叫 ``corpId``，正则若匹配 ``corp[0-9a-zA-Z]+`` 会误命中
    键名本身（字面量 ``corpId``）而非值——所以必须按 JSON 结构化解析，绝不用正则抠。
    """
    cli = _find_cli("dingtalk")
    if cli:
        out = _run(cli, ["profile", "list"])
        if out:
            try:
                data = json.loads(out)
            except json.JSONDecodeError as _exc:
                logger.debug(f"_resolve_dingtalk: swallowed exception: {_exc}")
                data = None
            if isinstance(data, dict):
                # 优先取激活/主 profile 的真实 corpId
                primary = (
                    data.get("primaryProfile")
                    or data.get("currentProfile")
                    or (data.get("profiles") or [{}])[0]
                )
                corp = primary.get("corpId") if isinstance(primary, dict) else None
                if corp:
                    return f"dingtalk:{corp}"
                # 兜底遍历所有 profile
                for p in data.get("profiles", []):
                    if isinstance(p, dict) and p.get("corpId"):
                        return f"dingtalk:{p['corpId']}"
            # 非 JSON 文本输出：真实 corpId 以 ding 开头，勿匹配 corpId 键名
            import re
            m = re.search(r"(ding[0-9a-zA-Z]+)", out)
            if m:
                return f"dingtalk:{m.group(1)}"
    if fallback_corp_id:
        return f"dingtalk:{fallback_corp_id}"
    logger.warning("[账号身份] 未取到钉钉 corpId，回退 dingtalk:unknown")
    return "dingtalk:unknown"


def _resolve_wecom() -> str:
    for path in _WECHAT_CFG_CANDIDATES:
        if os.path.exists(path):
            try:
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                return f"wecom:{h.hexdigest()[:16]}"
            except Exception as e:  # noqa: BLE001
                logger.debug("[账号身份] 读取企微配置哈希失败 %s: %s", path, e)
    logger.warning("[账号身份] 未找到企微配置，回退 wecom")
    return "wecom"


def resolve_account_id(platform: str, fallback_corp_id: Optional[str] = None) -> str:
    """解析某平台「当前登录账号」的稳定身份键。

    Args:
        platform: "feishu" / "dingtalk" / "wecom"
        fallback_corp_id: 钉钉备用 corpId（通常来自配置的 target_org_corp_id）

    Returns:
        ``"<platform>:<account>"`` 形式的命名空间键（解析失败也有稳定兜底键）。
    """
    cache_key = f"{platform}:{fallback_corp_id or ''}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    platform = (platform or "").lower()
    if platform == "feishu":
        aid = _resolve_feishu()
    elif platform == "dingtalk":
        aid = _resolve_dingtalk(fallback_corp_id)
    elif platform == "wecom":
        aid = _resolve_wecom()
    else:
        logger.warning("[账号身份] 未知平台 %s，回退 %s:unknown", platform, platform)
        aid = f"{platform}:unknown"
    _CACHE[cache_key] = aid
    logger.info("[账号身份] %s => %s", platform, aid)
    return aid


def invalidate_cache() -> None:
    """清空缓存（re-login / 配置变更后调用，强制下次重新探测）。"""
    _CACHE.clear()
