"""全局共享常量（跨模块单一真源）。

集中存放需要在多个层（src / web / config 模型）间共享、且历史上曾出现「多份副本
各自漂移」的常量，例如平台白名单。任何新增常量请确认是否应归口此处，避免再次
散落为三处互不感知的副本。
"""
from __future__ import annotations

from typing import Final

# 系统支持的全部 IM 平台。
# 与 src/config_models.py 中 ``PlatformConfig.adapter_type:
# Literal["dingtalk", "feishu", "wecom"]`` 保持一致；新增平台时两处必须同步更新，
# 否则会破坏「新增平台一处漏改」一致性断言（tests/test_platform_whitelist.py）。
SUPPORTED_PLATFORMS: Final[frozenset[str]] = frozenset({"dingtalk", "feishu", "wecom"})

# ---- 摘要取材必须排除的「元消息」------------------------------------------
# 这些**不是真实对话内容**，而是本系统自己写回 messages 表的中间产物：
#   1) role='system'：系统落库的摘要/提示消息；
#   2) content 以「【对话摘要】」开头：历史版本把生成的摘要回写进会话正文；
#   3) content 以「📋 …对话摘要…」开头：主动触达 digest 汇总推送后，推送副本被
#      当作普通消息回灌到主人会话里——里面装着**其他所有会话**的人和事。
# 一旦被当对话正文再次摘要，摘要就会把不相干的会话内容复述进来，且每轮把上轮
# 污染再放大一次。故「SQL 取材」与「LLM 拼接」两侧都要过滤（单一真源在此）。
SUMMARY_NOISE_CONTENT_PREFIXES: Final[tuple[str, ...]] = ("【对话摘要",)
SUMMARY_NOISE_ROLE: Final[str] = "system"


def is_summary_noise_message(content: str, role: str) -> bool:
    """判断一条消息是否为「摘要元消息」（不应再次参与摘要取材）。

    Args:
        content: 消息正文。
        role: 消息角色（``user`` / ``assistant`` / ``system``）。
    """
    if role == SUMMARY_NOISE_ROLE:
        return True
    text = (content or "").lstrip()
    if text.startswith(SUMMARY_NOISE_CONTENT_PREFIXES):
        return True
    # 主动触达 digest 的回灌副本，如「📋 近 24 小时对话摘要（共 8 段）」
    return text.startswith("📋") and "对话摘要" in text[:20]
