"""Metrics module — read-only observability queries over existing repos.

Provides:
- MetricsCollector: per-store stateless query object (tool stats, routing
  accuracy, blacklist trends, token tracking).
- report_logger: periodic structured JSON log emitter (Prometheus/Grafana ready).

All queries are read-only; no repo interfaces are modified.
"""

from src.metrics.collector import MetricsCollector
from src.metrics.report_logger import (
    MetricsReportLogger,
    start_metrics_reporter,
)

__all__ = [
    "MetricsCollector",
    "MetricsReportLogger",
    "start_metrics_reporter",
]
