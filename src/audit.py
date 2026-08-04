"""结构化审计日志（文件型 JSONL + 内存日志缓冲）。

审计是安全态势的闭环：高危写操作必须留痕。本模块提供统一的审计落点，
供以下三处调用：
- 工具真实执行（src/tools/base.py:_run_tool）
- 配置原子写回（web/api.py:_write_config）
- 审批转交（src/approval/service.py）

设计要点：
- 每条审计记录以 JSONL 追加到 data/audit.log（可重定位：开发态 cwd/data，打包态用户数据目录）。
- 同时经 ``logger.info("[audit] ...")`` 进入既有内存日志缓冲（Web 端可见）。
- 路径可通过 ``LINKORA_AUDIT_LOG`` 环境变量或 ``set_audit_log_path()`` 覆盖，便于测试。
- best-effort：写文件失败仅记日志，绝不向主流程抛异常。
- 仅落安全字段（事件/动作/状态/会话/目标标识），不写原始参数/令牌等敏感负载，
  敏感信息脱敏交由既有日志格式器处理。

状态管理：使用 _AuditState 类封装路径状态，避免 global 关键字。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_path

logger = logging.getLogger("linkora.audit")

_DEFAULT_PATH = data_path("audit.log")
_env_path = os.environ.get("LINKORA_AUDIT_LOG")


class _AuditState:
    """审计日志状态（替代 global _audit_path）。"""

    def __init__(self) -> None:
        self._path = Path(_env_path) if _env_path else _DEFAULT_PATH
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        with self._lock:
            return self._path

    def set_path(self, p: Path | None) -> None:
        with self._lock:
            self._path = p if p is not None else _DEFAULT_PATH


_audit_state = _AuditState()


def set_audit_log_path(path: str | Path | None) -> None:
    """覆盖审计日志路径（测试用）。传 None 恢复默认 data/audit.log。"""
    _audit_state.set_path(Path(path) if path else None)


def get_audit_log_path() -> Path:
    """返回当前审计日志路径（测试断言用）。"""
    return _audit_state.path


def audit(
    event_type: str,
    action: str,
    status: str,
    *,
    actor: str = "",
    session_key: str | None = None,
    target: str = "",
    detail: str = "",
    meta: dict | None = None,
) -> None:
    """记录一条审计事件（best-effort，永不抛异常）。

    Args:
        event_type: 业务域，如 tool_execution / config_write / approval_transfer。
        action: 具体动作，如 transfer_approval / update_config。
        status: 结果，如 success / failure / error / blocked。
        actor: 触发者（人/系统），如用户姓名、web。
        session_key: 会话标识（chat_id/sender_id），用于关联。
        target: 操作对象标识（工具名/配置路径/审批实例 ID）。
        detail: 简短人类可读说明（不含敏感原文）。
        meta: 额外结构化字段（可选）。
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "action": action,
        "status": status,
        "actor": actor,
        "session_key": session_key,
        "target": target,
        "detail": detail,
    }
    if meta:
        record["meta"] = meta

    # 进入内存日志缓冲（Web 端可见），并复用既有脱敏格式器
    tail = ""
    if session_key:
        tail += f" session={session_key}"
    if target:
        tail += f" target={target}"
    logger.info("[audit] %s | %s | %s%s", action, status, event_type, tail)

    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        with _audit_state._lock:
            path = _audit_state._path
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - 审计失败绝不能拖垮主流程
        logger.warning("[audit] 写审计日志文件失败(best-effort): %s", e)
