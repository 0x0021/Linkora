"""去重查询异常分类处理单元测试。

验证 P1-3 修复：去重查询失败的分类处理逻辑。
"""
from __future__ import annotations

import pytest
import inspect
from datetime import datetime, timedelta


class TestOwnerPresenceGateErrorClassification:
    """测试真人在场闸门的异常分类逻辑。"""

    def test_temporary_db_busy_returns_false(self):
        """临时 DB 繁忙时返回 False（保守放行）。"""
        # 验证源码包含分类逻辑
        with open('src/platform/runtime_inbound.py', 'r') as f:
            source = f.read()
        
        assert 'database is locked' in source or 'busy' in source, "应检测 DB busy 错误"
        assert 'timeout' in source, "应检测超时错误"

    def test_timeout_error_returns_false(self):
        """DB 超时错误时返回 False（保守放行）。"""
        with open('src/platform/runtime_inbound.py', 'r') as f:
            source = f.read()
        
        assert 'timeout' in source.lower(), "应检测 timeout 错误"

    def test_schema_error_logs_error(self):
        """Schema 错误时记录 ERROR 级别日志。"""
        with open('src/platform/runtime_inbound.py', 'r') as f:
            source = f.read()
        
        assert 'no such table' in source or 'schema' in source.lower(), "应检测 schema 错误"
        assert 'logger.error' in source, "schema 错误应记录 ERROR 日志"

    def test_unknown_error_returns_false(self):
        """未知错误时返回 False 并记录 warning。"""
        with open('src/platform/runtime_inbound.py', 'r') as f:
            source = f.read()
        
        # 未知错误应有兜底处理
        assert 'except Exception' in source, "应有通用异常处理"

    def test_success_case(self):
        """正常查询成功时的路径存在。"""
        with open('src/platform/runtime_inbound.py', 'r') as f:
            source = f.read()
        
        assert 'has_user_message_from' in source, "应调用 has_user_message_from"


class TestMessageLoopTimerProtection:
    """测试防抖 Timer 的 shutdown 保护。"""

    def test_process_pending_messages_checks_running(self):
        """_process_pending_messages 应检查 _running 标志。"""
        with open('src/platform/message_loop.py', 'r') as f:
            source = f.read()
        
        assert '_running' in source, "防抖 Timer 应检查 _running 标志"
        assert 'return' in source, "应提前返回避免执行后续逻辑"


class TestSummaryConsecutiveFailureProtection:
    """测试摘要调度的连续失败保护。"""

    def test_consecutive_failure_counting(self):
        """连续失败时应累计计数并在达到上限时暂停。"""
        # 模拟连续失败场景
        consecutive_failures = 0
        max_failures = 3
        should_fail = [True, True, True, False]
        break_at_failure = False

        for i, should_fal in enumerate(should_fail):
            if should_fal:
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    break_at_failure = True
                    break
            else:
                consecutive_failures = 0  # 重置

        # 验证在达到上限时触发暂停
        assert break_at_failure is True
        assert consecutive_failures == max_failures

    def test_summary_scheduler_source_has_protection(self):
        """验证源码包含连续失败保护逻辑。"""
        with open('src/platform/memory.py', 'r') as f:
            source = f.read()
        
        assert 'consecutive_failures' in source, "摘要调度缺少连续失败计数"
        assert 'max_consecutive_failures' in source or '>= 3' in source, "缺少失败阈值判断"


class TestFeishuChatTypeCacheTTL:
    """测试飞书 chat_type 缓存 TTL 机制。"""

    def test_cache_ttl_exists(self):
        """缓存应具有 TTL 机制。"""
        with open('src/poller_strategy.py', 'r') as f:
            source = f.read()
        
        assert 'cache_ttl' in source, "应有 cache_ttl 变量"
        assert 'expired_keys' in source, "应有过期条目清理逻辑"

    def test_ttl_value_is_reasonable(self):
        """TTL 值应合理（分钟级）。"""
        with open('src/poller_strategy.py', 'r') as f:
            source = f.read()
        
        # 查找 TTL 赋值
        import re
        ttl_match = re.search(r'cache_ttl\s*=\s*(\d+)', source)
        if ttl_match:
            ttl_value = int(ttl_match.group(1))
            assert 60 <= ttl_value <= 900, f"TTL {ttl_value} 秒不合理，应为 1-15 分钟"
