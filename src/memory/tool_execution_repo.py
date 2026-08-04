"""工具执行日志仓库。

从 src.memory.sqlite_store 拆出——tool_execution_logs 表的写入与统计。
不持有连接对象（连接必须按线程隔离），仅保存 store 引用，每次访问经
``self.conn`` 属性取当前线程的连接。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class ToolExecutionRepo:
    """工具执行日志仓库。"""

    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> "sqlite3.Connection":
        """返回当前线程独立的 SQLite 连接（委托 store.conn）。"""
        return self._store.conn

    def log(
        self,
        tool_name: str,
        input_args: str,
        output_result: str,
        success: bool,
        duration_ms: float,
        error_message: str = "",
    ) -> None:
        """记录工具调用日志到数据库。"""
        try:
            cur = self.conn.cursor()
            now = datetime.now().isoformat()
            cur.execute(
                """INSERT INTO tool_execution_logs
                   (tool_name, input_args, output_result, success, duration_ms, error_message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool_name,
                    input_args,
                    output_result,
                    int(success),
                    duration_ms,
                    error_message,
                    now,
                ),
            )
            self.conn.commit()
        except Exception as e:
            logger.warning("[ToolLog] 记录工具日志失败: %s", e)

    def stats(self, days: int) -> list[dict]:
        """最近 N 天各工具的调用次数 / 成功率(%) / 平均耗时(ms)，按调用次数降序。

        供工具调用统计面板使用；成功率与耗时均已按展示精度四舍五入到 1 位小数。
        """
        cur = self.conn.cursor()
        cur.execute(
            """SELECT tool_name,
                      COUNT(*) AS total_calls,
                      ROUND(AVG(success) * 100, 1) AS success_rate,
                      ROUND(AVG(duration_ms), 1) AS avg_duration_ms
               FROM tool_execution_logs
               WHERE created_at >= datetime('now', ?, 'localtime')
               GROUP BY tool_name
               ORDER BY total_calls DESC""",
            (f"-{days} days",),
        )
        return [dict(r) for r in cur.fetchall()]

    def health(self) -> list[dict]:
        """全量工具调用健康度：调用次数 / 成功次数 / 平均耗时 / 最近一次调用时间。

        与 ``stats`` 的区别：不限时间窗、返回原始计数（不算成功率），
        供跨平台聚合的工具健康面板自行合并后再算比率。
        """
        cur = self.conn.cursor()
        cur.execute(
            """SELECT tool_name,
                      COUNT(*) as total,
                      SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as ok,
                      AVG(duration_ms) as avg_ms,
                      MAX(created_at) as last_call
               FROM tool_execution_logs
               GROUP BY tool_name
               ORDER BY total DESC"""
        )
        return [dict(r) for r in cur.fetchall()]
