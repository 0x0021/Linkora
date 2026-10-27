"""告警 Webhook 出站通道。

P3-4 修复（2026-10-27）：``AlertConfig.webhook_url`` / ``channels`` 此前是**死字段**
—— 声明了但没有任何出站逻辑消费，配了不生效，而 ``docs/alerts.md`` 仍在教用户
配置它们。本模块补上真正的消费方。

安全约束
--------
Webhook URL 由部署方在配置中给出（**非终端用户可控**），但仍统一走
``src.utils.net.safe_http_request``：它会做 DNS 解析后 IP 钉死，避免
「配置里写个内网地址把 webhook 变成 SSRF 通道」这类误用，同时禁用重定向
（防止 302 到内网）。因此**无需** ``# ssrf-ok`` 豁免。

设计约束
--------
告警属于**旁路**能力：webhook 发送失败绝不能影响主回复链路，故所有异常在此
吞掉并降级为日志（不 reraise、不计入告警自身计数，避免自我放大）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.utils.net import safe_http_request

logger = logging.getLogger("linkora.alerts")

# Webhook 请求超时（秒）。旁路能力，宁可超时也不拖住主链路。
_WEBHOOK_TIMEOUT_SECONDS = 5


def send_webhook(
    url: str,
    alert_data: dict[str, Any],
    timeout: int = _WEBHOOK_TIMEOUT_SECONDS,
) -> bool:
    """向 webhook 地址 POST 一条告警 JSON。

    Args:
        url: 部署方配置的 webhook 地址（配置端点，非用户输入）。
        alert_data: 告警负载（``AlertManager._trigger_alert`` 构造的 dict）。
        timeout: 请求超时秒数。

    Returns:
        True = 投递成功；False = 未配置 / 投递失败（均已记录日志）。
    """
    if not url:
        logger.debug("[告警] webhook 未配置地址，跳过投递")
        return False

    payload = json.dumps(alert_data, ensure_ascii=False).encode("utf-8")
    try:
        resp = safe_http_request(
            "POST",
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        status = getattr(resp, "status_code", 0)
        if 200 <= int(status) < 300:
            logger.info("[告警] webhook 投递成功: %s", url)
            return True
        logger.warning("[告警] webhook 返回非 2xx: %s (status=%s)", url, status)
        return False
    except Exception as exc:
        # 旁路能力：投递失败不向上抛，避免打断主链路
        logger.warning("[告警] webhook 投递失败: %s (%s)", url, exc)
        return False
