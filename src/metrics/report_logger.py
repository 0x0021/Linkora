"""Periodic structured-metrics reporter.

Emits JSON-formatted log lines once per interval for each platform.
Designed for Prometheus/Grafana ingestion via log collectors (Fluentd, Logstash, etc.).

Usage (from main.py or shared_state):
    from src.metrics.report_logger import MetricsReportLogger

    logger = MetricsReportLogger(
        stores={"dingtalk": store_dingtalk, "feishu": store_feishu},
        interval_seconds=3600,
    )
    logger.start()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

from src.metrics.collector import MetricsCollector

logger = logging.getLogger("metrics.report")


class MetricsReportLogger:
    """Background daemon thread that periodically emits structured metrics logs."""

    def __init__(
        self,
        stores: dict[str, "SQLiteStore"],
        interval_seconds: int = 3600,
    ) -> None:
        self._stores = stores
        self._interval = max(60, interval_seconds)  # minimum 1 minute
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background reporting thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("MetricsReportLogger already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="metrics-reporter", daemon=True
        )
        self._thread.start()
        logger.info(
            "MetricsReportLogger started (interval=%ds, platforms=%s)",
            self._interval,
            list(self._stores.keys()),
        )

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            logger.info("MetricsReportLogger stopped")

    def report_once(self) -> None:
        """Emit one round of metrics logs immediately (for testing or manual trigger)."""
        for platform_id, store in self._stores.items():
            try:
                collector = MetricsCollector(store)
                snap = collector.snapshot(time_range_hours=24)
                snap["_platform"] = platform_id
                snap["_type"] = "metrics_snapshot"
                logger.info(json.dumps(snap, ensure_ascii=False))
            except Exception:
                logger.exception(
                    "Failed to collect metrics for platform=%s", platform_id
                )

    def _run(self) -> None:
        """Daemon loop: emit metrics on interval."""
        while not self._stop_event.wait(self._interval):
            try:
                self.report_once()
            except Exception as e:  # noqa: BLE001
                # 上报异常若逃逸会静默杀死指标线程（僵尸线程），下轮继续即可
                logger.error("指标上报异常（已忽略，下轮继续）: %s", e, exc_info=True)


# ── Convenience: start from main.py ──────────────────────────────────────

def start_metrics_reporter(
    stores: dict[str, "SQLiteStore"],
    interval_seconds: int = 3600,
) -> MetricsReportLogger:
    """Convenience function to instantiate and start the reporter.

    Call once from main.py after all store instances are initialized:
        start_metrics_reporter(app_instance.get_all_stores())
    """
    reporter = MetricsReportLogger(stores=stores, interval_seconds=interval_seconds)
    reporter.start()
    return reporter
