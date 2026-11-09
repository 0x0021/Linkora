"""入站消息安全防护策略（``safety.guard`` 配置段）。

本模块**只依赖 pydantic**，不导入 ``src.config_models`` / ``src.shared_state``，
避免 ``config_models.system -> guard.config -> config_models`` 形成循环导入。
运行期配置由 ``src.guard.service.get_guard_config()`` 从共享配置单例读取。

设计原则（与项目既有铁律一致）：

- **不阻断正常业务消息**：默认 ``block_high_risk=False``、``mode=enforce`` 时
  只做「清洗 + 边界隔离」，消息照常进入 LLM，业务流程零变更。
- **可降级为纯观测**：``mode=observe`` 时只记录事件用于 Web 展示，正文一字不改。
- **频率控制默认宽松**：阈值按「单个发送者每分钟」计，正常聊天不会触发。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class GuardConfig(BaseModel):
    """入站消息提示词注入防护策略。

    所有字段都有安全默认值，``config.yaml`` 未写本段时行为等价于「只记录 +
    清洗结构化攻击载荷、不拦截整条消息」。
    """

    # ── 总开关与模式 ──
    enabled: bool = True
    # enforce：记录 + 清洗 + 边界隔离；observe：只记录，正文零改动（可用于灰度观察）
    mode: Literal["enforce", "observe"] = "enforce"
    # 是否整条拦截 high 风险消息（默认 False —— 不改变现有业务流程）
    block_high_risk: bool = False

    # ── 检测项开关（全部是结构性信号，无自然语言语义判断）──
    detect_role_spoofing: bool = True
    detect_boundary_escape: bool = True
    detect_control_chars: bool = True
    detect_oversize: bool = True

    # ── 处置 ──
    sanitize_payload: bool = True  # 剥离协议标记 / 不可见控制字符
    wrap_untrusted_inbound: bool = True  # 对命中消息加不可信数据边界标记
    redact_output_secrets: bool = True  # 回复正文密钥脱敏

    # ── 阈值 ──
    max_message_chars: int = 8000  # 超长消息截断阈值（仅对命中风险的消息生效）
    max_repeat_ratio: float = 0.85  # 单字符重复占比超过该值判为异常（防填充攻击）
    min_repeat_length: int = 200  # 短消息不参与重复率判定

    # ── 审计 / 事件留存 ──
    audit_enabled: bool = True  # 是否写 JSONL 事件文件
    log_all_messages: bool = False  # True 时连无风险消息也记（默认只记命中，避免日志膨胀）
    max_events: int = 5000  # 事件文件保留上限（滚动截断）
    content_preview_chars: int = 200  # 事件里保留的原始内容预览长度

    # ── 频率控制（防注入刷屏打爆 LLM / 日志）──
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_max_messages: int = 60
    rate_limit_block: bool = True  # 超限时跳过本条消息的 LLM 处理


DEFAULT_GUARD_CONFIG = GuardConfig()

__all__ = ["GuardConfig", "DEFAULT_GUARD_CONFIG"]
