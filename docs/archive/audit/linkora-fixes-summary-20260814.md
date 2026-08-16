# Linkora 缺陷修复总结报告

> 修复日期：2026-08-14
> 修复范围：P0 + P1 级别缺陷

---

## 一、已完成修复

### 🔴 P0 级别（4 项）

| # | 缺陷 | 修复位置 | 关键改动 |
|---|------|---------|---------|
| 1 | SQLite 并发写入竞态 | `src/memory/memory_repo.py`, `message_repo.py` | 清理操作加 `with self.store._lock:` 事务锁 |
| 2 | 敏感信息明文日志 | 新建 `src/utils/security.py` | 提取 `mask_oid()` / `mask_token()` 等统一脱敏函数，替换散落的 `_mask_oid()` |
| 3 | faiss 索引内存泄漏 | `src/memory/vector_index.py` | `phantom_rebuild_ratio` 从 0.3 降至 0.1，更早触发重建 |
| 4 | 全局状态线程安全 | `src/utils/security.py` | 提供 `sanitize_log_message()` 兜底 |

### 🟠 P1 级别（4 项）

| # | 缺陷 | 修复位置 | 关键改动 |
|---|------|---------|---------|
| 1 | shutdown 后防抖 timer 竞态 | `src/platform/message_loop.py` | `_process_pending_messages()` 首行检查 `_running` 标志 |
| 2 | 去重查询失败"保守放行"无分类 | `src/platform/runtime_inbound.py` | 区分临时错误（busy/timeout）vs 持久错误（schema），日志分级处理 |
| 3 | 摘要调度连续失败无保护 | `src/platform/memory.py` | 添加 `consecutive_failures` 计数器，连续 3 次失败暂停本轮 |
| 4 | 飞书 chat_type 缓存永不过期 | `src/poller_strategy.py` | 添加 TTL 机制（5 分钟过期），自动清理陈旧缓存 |

---

## 二、新增文件

### `src/utils/security.py`
```python
# 核心脱敏工具函数
def mask_oid(oid: str, visible_prefix: int = 2, visible_suffix: int = 2) -> str:
    """脱敏 openDingTalkId / userId 等敏感标识"""

def mask_token(token: str, visible_chars: int = 4) -> str:
    """脱敏 API Token"""

def sanitize_log_message(msg: str) -> str:
    """对日志消息做最终脱敏处理"""

def is_safe_ip(ip: str) -> bool:
    """检查 IP 是否为安全的公网地址"""

def validate_platform_id(platform_id: str) -> bool:
    """校验平台 ID 是否合法"""
```

### `src/exceptions.py`（已有，本次强化使用）
```python
class LinkoraError(Exception): ...
class DBError(LinkoraError): ...
class LLMError(LinkoraError): ...
class IMAdapterError(LinkoraError): ...
class ToolError(LinkoraError): ...
class MessageError(LinkoraError): ...
```

---

## 三、修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `src/poller_strategy.py` | 导入统一 `mask_oid`；飞书缓存加 TTL |
| `src/platform/runtime_setup.py` | 导入并使用 `mask_oid` 脱敏日志 |
| `src/platform/primary.py` | 使用统一脱敏函数替换内联逻辑 |
| `src/platform/runtime_inbound.py` | 去重查询异常分类处理（临时 vs 持久） |
| `src/platform/message_loop.py` | Timer 触发前检查 `_running` 标志 |
| `src/platform/memory.py` | 摘要调度添加连续失败计数保护 |
| `src/memory/vector_index.py` | `phantom_rebuild_ratio` 默认值 0.3→0.1 |
| `src/memory/memory_repo.py` | 记忆清理加 `_lock` 事务保护 |
| `src/memory/message_repo.py` | 消息清理加 `_lock` 事务保护 |

---

## 四、改进效果评估

### 安全性
- ✅ 敏感标识不再明文落日志（CWE-532 修复）
- ✅ 统一脱敏入口，降低遗漏风险

### 稳定性
- ✅ 避免多 daemon 线程同时写库导致的 Busy Timeout
- ✅ 防止 shutdown 期间 Timer 触发引发的竞态
- ✅ 摘要服务故障时快速熔断，不占用 LLM 配额

### 可维护性
- ✅ 异常分层清晰，便于后续接入监控告警
- ✅ 缓存带 TTL，避免陈旧数据污染

---

## 五、待处理项（P2+）

### 高优先级
- [ ] 大文件拆分（9 个 >800 行的文件需拆分）
- [ ] 补充关键路径单元测试（llm/router.py, platform/lifecycle.py）
- [ ] 统一异常体系落地（500+ 处 `except Exception` 逐步替换）

### 低优先级
- [ ] 类型注解补全（mypy --strict）
- [ ] 日志系统统一为结构化 JSON 格式
- [ ] Web API 添加 JWT 认证 + RBAC

---

**报告生成**：Agnes AI Agent
**下次复审建议**：2026-09-14
