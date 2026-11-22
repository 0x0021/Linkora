"""工作时间外未回复消息 → 个人待办（off-hours todo）。

- ``worktime``：工作时间 / 工作日 / 节假日判定（纯函数）。
- ``scanner``：按平台 + 会话扫描时间窗消息，筛出「未读或无真人回复」且落在工作时间外的消息。
- ``service``：编排去重、建待办（钉钉/飞书，企微降级通知）、双边通知、状态流转。
"""

from src.offhours_todo.service import OffhoursTodoService

__all__ = ["OffhoursTodoService"]
