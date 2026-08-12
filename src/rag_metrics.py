"""RAG 清洗 / 分块流水线轻量指标采集（纯增量，不改动任何清洗逻辑）。

为什么需要它
------------
语义分块 + LLM 清洗刚上线，目前只有函数级单测，**没法判断「LLM 清洗到底值不值 /
有没有回归」**。本模块提供一组线程安全的内存计数器，挂在 ``clean_document_for_rag``
与 ``split_text`` 内部打点，用于回答：

- LLM 清洗调用了多少次？其中多少成功、多少回退到正则预清洗（含过度缩短）？
- 回退率是否在爬升？（回退率异常升高 = LLM 不可用或过度改写，质量在掉）
- 清洗前后字符量变化（``chars_out / chars_in`` 比值，过低提示过度缩短风险）？
- 单次清洗平均耗时？分块平均块数？（评估 embedding / 检索成本）

设计原则
--------
- **零行为影响**：只计数、只读数，绝不改变清洗 / 分块的输出。
- **线程安全**：所有计数器由 ``threading.Lock`` 保护，可被 poller / web / 后台线程共用。
- **无外部依赖**：仅用标准库，避免引入 metrics 第三方库造成的依赖漂移。
- **可观测入口**：``snapshot()`` 取当前累计快照（dict），``log_summary()`` 打一条 INFO
  汇总日志。部署侧可在管理接口或定时任务里调用 ``snapshot()`` 暴露给监控系统。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class _RagMetrics:
    """RAG 流水线指标采集器（单例，模块级复用同一份计数）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """清空所有累计计数。测试或部署侧主动重置时调用。"""
        with self._lock:
            self.clean_calls = 0
            self.clean_llm_ok = 0
            self.clean_fallback_regex = 0
            self.clean_chars_in = 0
            self.clean_chars_out = 0
            self.clean_duration_ms = 0
            self.chunk_calls = 0
            self.chunk_total = 0

    def record_clean(
        self,
        *,
        fallback: bool,
        chars_in: int,
        chars_out: int,
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次文档清洗。

        Args:
            fallback: True=回退到正则预清洗（LLM 不可用 / 异常 / 过度缩短）；
                      False=LLM 语义清洗成功。
            chars_in: 原始文档字符数。
            chars_out: 清洗后字符数（fallback 时为预清洗结果长度）。
            duration_ms: 本次清洗耗时（毫秒）。
        """
        with self._lock:
            self.clean_calls += 1
            if fallback:
                self.clean_fallback_regex += 1
            else:
                self.clean_llm_ok += 1
            self.clean_chars_in += max(0, chars_in)
            self.clean_chars_out += max(0, chars_out)
            self.clean_duration_ms += max(0.0, duration_ms)

    def record_chunk(self, *, count: int) -> None:
        """记录一次分块调用的产出块数。"""
        with self._lock:
            self.chunk_calls += 1
            self.chunk_total += max(0, count)

    def snapshot(self) -> dict:
        """返回当前累计指标快照。

        含派生指标：``clean_fallback_rate``（回退率）、``clean_avg_chars_out``
        （平均出字符）、``chunk_avg``（平均分块数）。
        """
        with self._lock:
            calls = self.clean_calls
            chunks = self.chunk_calls
            return {
                "clean_calls": calls,
                "clean_llm_ok": self.clean_llm_ok,
                "clean_fallback_regex": self.clean_fallback_regex,
                "clean_chars_in": self.clean_chars_in,
                "clean_chars_out": self.clean_chars_out,
                "clean_duration_ms": round(self.clean_duration_ms, 1),
                "clean_fallback_rate": round(self.clean_fallback_regex / calls, 3) if calls else 0.0,
                "clean_avg_chars_out": round(self.clean_chars_out / calls, 1) if calls else 0.0,
                "chunk_calls": chunks,
                "chunk_total": self.chunk_total,
                "chunk_avg": round(self.chunk_total / chunks, 1) if chunks else 0.0,
            }


# 模块级单例与便捷函数（避免调用处持有实例，保持打点零侵入）。
_metrics = _RagMetrics()
reset = _metrics.reset
record_clean = _metrics.record_clean
record_chunk = _metrics.record_chunk
snapshot = _metrics.snapshot


def log_summary() -> None:
    """打一条 INFO 级指标汇总日志，便于从运行日志直接观察 RAG 流水线健康度。"""
    s = snapshot()
    logger.info(
        "[RAG 指标] 清洗调用=%d(LLM成功=%d/回退=%d,回退率=%.1f%%,平均出字符=%s) "
        "字符进出=%d->%d 耗时=%.0fms | 分块调用=%d 总块数=%d 平均=%.1f",
        s["clean_calls"], s["clean_llm_ok"], s["clean_fallback_regex"],
        s["clean_fallback_rate"] * 100, s["clean_avg_chars_out"],
        s["clean_chars_in"], s["clean_chars_out"], s["clean_duration_ms"],
        s["chunk_calls"], s["chunk_total"], s["chunk_avg"],
    )
