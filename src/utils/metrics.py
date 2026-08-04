"""轻量级 LLM 指标聚合器（线程安全）。

用于 #5 仪表盘：跟踪
- llm_calls: 主模型调用次数
- fallback_used: 是否走过 fallback（>=1 表示）
- tool_calls: tool_call 次数
- total_latency_ms: 本次请求总延迟
- input_tokens / output_tokens: token 估算（用 _estimate_tokens 复用）
- rate_limited: 触发过 429
- errors: 失败次数

按分钟窗口滚动保留近 60 分钟数据，供 Web 端图表查询。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMSample:
    ts: float
    platform_id: str
    llm_calls: int = 0
    fallback_used: int = 0
    tool_calls: int = 0
    total_latency_ms: int = 0
    rate_limited: int = 0
    errors: int = 0
    input_tokens_est: int = 0
    output_tokens_est: int = 0
    request_id: str = ""


class MetricsAggregator:
    """单实例全局聚合器（多平台共享）。"""
    _instance: Optional["MetricsAggregator"] = None
    _lock = threading.Lock()

    def __init__(self, window_seconds: int = 3600, max_samples: int = 20000):
        self._samples: deque[LLMSample] = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._window = window_seconds
        # 实时累计（用于快速查询近 1min / 5min 窗口）
        self._realtime_lock = threading.Lock()
        self._realtime = {
            "llm_calls": 0,
            "fallback_used": 0,
            "tool_calls": 0,
            "errors": 0,
            "rate_limited": 0,
            "latency_total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "last_request_ts": 0.0,
        }
        self._since_start = {
            "llm_calls": 0,
            "fallback_used": 0,
            "tool_calls": 0,
            "errors": 0,
            "rate_limited": 0,
            "latency_total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "start_ts": time.time(),
        }

    @classmethod
    def instance(cls) -> "MetricsAggregator":
        """单例获取（线程安全）。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def record(self, sample: LLMSample) -> None:
        """记录一条样本。"""
        now = time.time()
        sample.ts = now
        with self._lock:
            self._samples.append(sample)
        with self._realtime_lock:
            self._realtime["llm_calls"] += sample.llm_calls
            self._realtime["fallback_used"] += sample.fallback_used
            self._realtime["tool_calls"] += sample.tool_calls
            self._realtime["errors"] += sample.errors
            self._realtime["rate_limited"] += sample.rate_limited
            self._realtime["latency_total"] += sample.total_latency_ms
            self._realtime["input_tokens"] += sample.input_tokens_est
            self._realtime["output_tokens"] += sample.output_tokens_est
            self._realtime["last_request_ts"] = now
        for k in ("llm_calls", "fallback_used", "tool_calls", "errors",
                  "rate_limited", "latency_total", "input_tokens", "output_tokens"):
            self._since_start[k] = self._since_start.get(k, 0) + getattr(sample, k, 0)

    def summary(self, window_seconds: int = 300) -> dict:
        """汇总最近 window_seconds 内的指标。"""
        cutoff = time.time() - window_seconds
        with self._lock:
            relevant = [s for s in self._samples if s.ts >= cutoff]
        if not relevant:
            return {
                "window_seconds": window_seconds,
                "sample_count": 0,
                "llm_calls": 0,
                "fallback_used": 0,
                "tool_calls": 0,
                "errors": 0,
                "rate_limited": 0,
                "latency_avg_ms": 0,
                "latency_p95_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fallback_rate": 0.0,
                "error_rate": 0.0,
            }
        llm_calls = sum(s.llm_calls for s in relevant)
        fallback_used = sum(s.fallback_used for s in relevant)
        tool_calls = sum(s.tool_calls for s in relevant)
        errors = sum(s.errors for s in relevant)
        rate_limited = sum(s.rate_limited for s in relevant)
        latencies = sorted([s.total_latency_ms for s in relevant if s.total_latency_ms > 0])
        latency_avg = int(sum(latencies) / len(latencies)) if latencies else 0
        latency_p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
        input_tokens = sum(s.input_tokens_est for s in relevant)
        output_tokens = sum(s.output_tokens_est for s in relevant)
        return {
            "window_seconds": window_seconds,
            "sample_count": len(relevant),
            "llm_calls": llm_calls,
            "fallback_used": fallback_used,
            "tool_calls": tool_calls,
            "errors": errors,
            "rate_limited": rate_limited,
            "latency_avg_ms": latency_avg,
            "latency_p95_ms": latency_p95,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "fallback_rate": round(fallback_used / llm_calls, 3) if llm_calls else 0.0,
            "error_rate": round(errors / len(relevant), 3) if relevant else 0.0,
            "rate_limit_rate": round(rate_limited / llm_calls, 3) if llm_calls else 0.0,
        }

    def lifetime(self) -> dict:
        """返回启动至今累计指标。"""
        return {
            "start_ts": self._since_start.get("start_ts", 0),
            "uptime_seconds": int(time.time() - self._since_start.get("start_ts", time.time())),
            "llm_calls": self._since_start.get("llm_calls", 0),
            "fallback_used": self._since_start.get("fallback_used", 0),
            "tool_calls": self._since_start.get("tool_calls", 0),
            "errors": self._since_start.get("errors", 0),
            "rate_limited": self._since_start.get("rate_limited", 0),
            "input_tokens": self._since_start.get("input_tokens", 0),
            "output_tokens": self._since_start.get("output_tokens", 0),
        }

    def recent_requests(self, n: int = 50) -> list[dict]:
        """返回最近 n 条样本（用于前端瀑布图）。"""
        with self._lock:
            recent = list(self._samples)[-n:]
        return [
            {
                "ts": s.ts,
                "platform_id": s.platform_id,
                "llm_calls": s.llm_calls,
                "fallback_used": s.fallback_used,
                "tool_calls": s.tool_calls,
                "total_latency_ms": s.total_latency_ms,
                "rate_limited": s.rate_limited,
                "errors": s.errors,
                "input_tokens_est": s.input_tokens_est,
                "output_tokens_est": s.output_tokens_est,
                "request_id": s.request_id,
            }
            for s in recent
        ]
