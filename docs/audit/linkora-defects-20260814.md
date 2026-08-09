# Linkora 代码缺陷审计报告

> 审计时间：2026-08-14
> 审计范围：src/ + web/ + scripts/（Python 端）
> 代码规模：~45K LOC / 139 Python 文件 / 约 500+ Exception 捕获点

---

## 一、严重级别汇总

| 级别 | 数量 | 描述 |
|------|------|------|
| 🔴 P0（立即修复） | 4 | 数据丢失风险、安全漏洞、核心功能故障 |
| 🟠 P1（尽快修复） | 8 | 功能缺陷、竞态条件、资源泄漏 |
| 🟡 P2（计划修复） | 15 | 代码质量、可维护性、防御性不足 |
| 🟢 P3（长期优化） | 多个 | 架构优化、技术债清理 |

---

## 二、🔴 P0 缺陷（立即修复）

### P0-1: SQLite WAL 模式下的并发写入竞态

**位置**：`src/memory/sqlite_store.py` + `src/platform/runtime_inbound.py`

**问题描述**：
- SQLite 使用 WAL 模式，但多处并发写入未加行级锁
- `_start_memory_cleanup_scheduler`、`_start_decision_cleanup_scheduler`、`_start_messages_cleanup_scheduler`、`_start_conversation_summary_scheduler` 四个 daemon 线程同时操作同一 DB 文件
- 虽然有 `_memory_lock`，但 cleanup 操作的 SELECT → DELETE 不是原子操作

**风险**：SQLite Busy Timeout 或 Integrity Error，导致记忆/决策/消息清理失败

**复现路径**：高负载场景（多平台 + 高频消息 + 大知识库）

**建议修复**：
```python
# 在清理操作周围加事务锁
with self.store.conv_conn(platform).execution_options(
    isolation_level="DEFERRED"
) as conn:
    conn.execute("BEGIN")
    # 查询删除...
    conn.commit()
```

---

### P0-2: 全局状态变量_thread_local 不安全

**位置**：`src/shared_state.py`, `src/config.py`, `src/semantic.py`, `src/llm/client.py`

**问题描述**：
- `src/llm/client.py:L79` `_MAX_EXTRA_GLOBAL_ATTEMPTS = 10` 模块级常量
- `_llm_state` 是单例但用 `threading.Lock` 保护，读操作未加锁（见注释"以场景看偏差不超过一次赋值"）
- ContextVar 仅覆盖部分路径，`get_current_platform()` 在非请求线程可能返回错误值

**风险**：多平台环境下配置错乱、限流判断错误导致 API Key 耗尽

**建议修复**：
```python
from contextvars import ContextVar
_PLAT_CTX: ContextVar[str] = ContextVar('platform')

def set_current_platform(pid: str):
    _PLAT_CTX.set(pid)

def get_current_platform() -> str:
    try:
        return _PLAT_CTX.get()
    except LookupError:
        return "dingtalk"  # 默认值
```

---

### P0-3: 敏感信息明文日志

**位置**：`src/poller_strategy.py:L171` `_mask_oid()` 函数未被调用

**问题描述**：
- 定义了 `_mask_oid()` 用于脱敏 openDingTalkId，但日志中直接使用原始 ID
- `runtime_inbound.py:L186` `except Exception as e:` 后打印完整异常堆栈（含 token）

**风险**：用户敏感标识符泄露到日志文件

**建议修复**：
1. 所有包含 `openDingTalkId`、`sender_id` 的日志调用 `_mask_oid()`
2. 异常日志脱敏：
```python
except Exception as e:
    safe_msg = re.sub(r'[a-zA-Z0-9]{20,}', '***', str(e))
    logger.error(f"处理失败: {safe_msg}")
```

---

### P0-4: faiss 索引幽灵向量内存泄漏

**位置**：`src/memory/vector_index.py`

**问题描述**：
- `remove()` 方法仅删除映射关系，faiss 底层向量不清除
- `phantom_rebuild_ratio=0.3` 阈值过高，需积累大量删除才触发重建
- `_maybe_rebuild_locked()` 逻辑复杂且有边界条件 bug（代码中可见断行问题）

**风险**：长时间运行后内存持续增长，HNSW 索引查询性能下降

**建议修复**：
```python
# 降低阈值到 0.1，并增加定时主动重建
def maybe_rebuild(self) -> bool:
    with self._lock:
        live = len(self._id_map)
        total = self._index.ntotal if self._index else 0
        phantom = total - live
        ratio = phantom / max(live, 1)
        if ratio > 0.1 or (total > 10000 and phantom > 1000):
            return self._maybe_rebuild_core()
        return False
```

---

## 三、🟠 P1 缺陷（尽快修复）

### P1-1: 异常处理过于宽泛（~500 处）

**位置**：全项目，主要文件：
- `src/poller_strategy.py`（21 处）
- `src/poller_core_parse.py`（5 处）
- `src/platform/memory.py`（9 处）
- `src/llm/client.py`（多处）

**问题描述**：
```python
# 典型模式（危险）
except Exception as e:
    logger.warning(f"xxx failed: {e}")
    # 静默继续，不告知调用方

# noqa: BLE001 滥用（91 处）证明规则被系统性规避
```

**风险**：
- LLM 调用失败被吞掉，用户收到空回复
- 消息解析错误不暴露，难以定位格式问题
- 数据库操作失败不重试，可能导致数据不一致

**建议修复优先级**：
1. LLM 调用路径：必须区分网络错误 vs 内容错误
2. 数据库写操作：失败应抛出自定义 `DBWriteError`
3. IM 适配器：区分权限错误、限频错误、超时

---

### P1-2: 防抖定时器在 shutdown 后仍可能触发

**位置**：`src/platform/runtime_lifecycle.py:L344`

```python
timer = threading.Timer(delay, self._process_pending_messages, args=(key,))
timer.start()
self._pending_timers[key] = timer
```

**问题描述**：
- shutdown 时取消 timer，但如果 timer 已启动但在执行前被取消，`cancelled=True` 检查缺失
- `_process_pending_messages` 内部再次检查 `self._running`，但此时 `store` 可能已关闭

**风险**：shutdown 后仍尝试发消息，引发 `AttributeError` 或 `sqlite3.ProgrammingError`

**建议修复**：
```python
def _process_pending_messages(self, key: str):
    if not self._running or self._shutdown_event.is_set():
        return
    # ... 原有逻辑
```

---

### P1-3: 多平台消息路由错误

**位置**：`src/platform/runtime_inbound.py:L125`

```python
except Exception as e:
    logger.debug("[inbound] 去重查询异常: %s", e)
    # 保守放行（不静默），避免 DB 抖动误杀正常回复
```

**问题描述**：
- 去重查询失败时"保守放行"意味着可能发送重复回复
- 多平台下 `_processed_msg_ids` 内存缓存未按 platform 隔离

**风险**：重复消息导致 LLM token 浪费、用户体验差（同一个问题收到多条回复）

**建议修复**：
```python
# 使用平台隔离的缓存 key
cache_key = f"{platform}:{msg_id}"
if cache_key in self._processed_msg_ids:
    return None
```

---

### P1-4: 飞书 chat_type 纠错缓存未清理

**位置**：`src/poller_strategy.py:L131`

```python
cache = getattr(self, "_feishu_conv_info_cache", None)
if cache is None:
    cache = {}
    self._feishu_conv_info_cache = cache
```

**问题描述**：
- 缓存永不过期，旧会话信息累积
- 会话类型变更后（如单聊变群聊）不会更新

**建议修复**：添加 TTL 过期机制或使用 `functools.lru_cache(maxsize=100, ttl=3600)`

---

### P1-5: 工具调用结果过期检测不完整

**位置**：`src/llm/router.py:L55-85`

**问题描述**：
- `check_stale_tool_results()` 解析 JSON 查找 `_ts` 字段，但部分工具（如 `web_search`）未序列化时间戳
- TTL 硬编码，无法通过配置调整

**风险**：LLM 使用过期搜索结果给出错误答案

**建议修复**：
```python
# 统一在工具执行后注入时间戳
tool_result["_ts"] = datetime.now().isoformat()
tool_result["_ttl"] = TOOL_RESULT_TTL.get(tool_name, 600)
```

---

### P1-6: OCR 图片下载无大小限制

**位置**：`src/poller_core_ocr.py`

**问题描述**：
- 从钉钉/飞书下载图片时未限制文件大小
- 恶意或异常链接可能返回超大文件，耗尽磁盘

**建议修复**：
```python
response = ssrf_safe_get(url, timeout=30, allow_redirects=True)
if len(response.content) > 10 * 1024 * 1024:  # 10MB
    logger.warning("图片过大，跳过: %d bytes", len(response.content))
    return None
```

---

### P1-7: 对话摘要无限循环风险

**位置**：`src/platform/memory.py:L340-365`

**问题描述**：
- `while self._running:` 循环中，如果 LLM 持续失败，会一直重试
- 虽然有 `max_summaries_per_cycle` 限制，但下一轮会重新开始

**风险**：LLM 服务故障时占用所有并发槽位，阻塞正常消息处理

**建议修复**：
```python
# 添加连续失败计数
consecutive_failures = 0
max_consecutive_failures = 3

for chat_id in candidates:
    try:
        summary = await generate_summary(...)
        consecutive_failures = 0  # 重置
    except Exception as e:
        consecutive_failures += 1
        if consecutive_failures >= max_consecutive_failures:
            logger.error(f"连续 {max_consecutive_failures} 次摘要失败，暂停本轮")
            break
```

---

### P1-8: 配置文件热重载竞态

**位置**：`src/config.py`, `src/dev_hot_reload.py`

**问题描述**：
- `config.yaml` 变更时通过热重载更新，但正在使用 config 的请求可能读到半更新状态
- `AppConfig` 是 Pydantic model，deep copy 开销大

**建议修复**：使用 `Copy-on-Write` 语义或原子替换：
```python
import atomicwrites
# 先写临时文件，再原子替换
atomicwrites.atomic_replace(config_path, new_content)
```

---

## 四、🟡 P2 缺陷（计划修复）

### P2-1: 大文件难以维护（>800 行）

| 文件 | 行数 | 建议拆分方向 |
|------|------|-------------|
| `tools/weather.py` | 939 | 天气查询/地理编码/缓存分开 |
| `tools/parse_document.py` | 932 | PDF/Word/Excel 各一个子类 |
| `config_models.py` | 901 | 按功能域拆分为 `config_models/*.py` |
| `llm/reply.py` | 887 | prompt 构建/风格控制/后处理分离 |
| `im_adapter/wecom.py` | 885 | 认证/消息/媒体分开 |
| `im_adapter/feishu.py` | 876 | 同上 |
| `poller_strategy.py` | 859 | discovery/message_fetch/conversation_agg 分离 |
| `tools/web_search.py` | 850 | 搜索引擎适配/结果解析/缓存分离 |
| `poller_core_parse.py` | 822 | 类型检测/内容提取/元数据处理分离 |

### P2-2: 测试覆盖率不均

**现状**：
- 190+ 测试文件，但集中在 `test_poller*.py`、`test_sqlite_store.py`
- 关键路径缺少测试：`llm/router.py`、`platform/lifecycle.py`、`memory/classifier.py`

**建议**：
1. 为核心业务逻辑（意图匹配、工具路由、RAG 注入）补充单元测试
2. 为异常路径补充失败用例（网络超时、DB 不可用、API 限流）

### P2-3: 日志系统冗余

**位置**：`src/utils/logger.py`（495 行）

**问题描述**：
- Rich 彩色日志与标准 logging 混用
- 结构化日志（JSON）与非结构化日志并存，解析困难

**建议**：统一为结构化日志，按环境选择输出格式（开发环境彩色，生产环境 JSON）

### P2-4: 类型注解不完整

**现状**：
- 部分函数有完整类型注解，部分只有 `Any`
- `src/tools/base.py:L452` 基类方法缺少返回类型

**建议**：为公共 API 添加完整类型注解，运行 `mypy --strict`

### P2-5: 魔法数字散落地

**位置**：多处硬编码数值

**示例**：
- `poller_strategy.py`: `history_session_gap_minutes = 360`
- `vector_index.py`: `hnsw_ef = 64`, `efSearch = max(hnsw_ef, 32)`
- `reply.py`: 截断长度 `60` 字符

**建议**：提取为模块级常量或配置项

### P2-6: 异步/同步混合混乱

**位置**：`src/llm/client.py`, `src/platform/runtime_inbound.py`

**问题描述**：
- 部分方法是 `async`，部分是同步，调用链混杂
- `await` 关键字散落，IDE 自动补全失效

**建议**：统一为 async/await 或添加明确边界（如 `run_coroutine_threadsafe`）

### P2-7: 环境变量读取不规范

**位置**：多处 `os.environ.get()`

**问题描述**：
- 无类型转换，依赖调用方自行处理
- 无默认值保护，可能返回 `None`

**建议**：创建统一的 `env.py` 工具函数：
```python
def get_env_int(name: str, default: int = 0) -> int:
    val = os.environ.get(name)
    return int(val) if val is not None else default
```

### P2-8: 文档与实现脱节

**位置**：`docs/` 目录

**问题描述**：
- `architecture.md` 描述的是重构前的结构
- `configuration.md` 的配置项已过时
- README.md 的 Quick Start 路径错误

**建议**：
1. 在 CI 中添加文档检查（`mkdocs build --strict`）
2. 关键配置项添加 `# noqa: docs-outdated` 标记，定期复核

### P2-9: 依赖版本锁定过严

**位置**：`requirements.lock`, `uv.lock`

**问题描述**：
- 锁定版本导致升级困难
- 安全补丁可能需要手动更新 lock 文件

**建议**：改用 `~=` 或 `^` 语义化版本约束，定期运行 `uv sync --upgrade`

### P2-10: 前端静态资源无缓存策略

**位置**：`web/static/`

**问题描述**：
- JS/CSS 文件无 hash 命名，浏览器缓存导致更新不及时
- 无 Service Worker，不支持离线访问

**建议**：构建时添加 content hash，配置 CDN 缓存策略

### P2-11: Web API 无速率限制

**位置**：`web/api.py`, `web/routers/*.py`

**问题描述**：
- 管理接口无认证隔离（除 login 外）
- 批量操作（如删除所有对话）无确认机制

**建议**：添加 JWT 认证 + RBAC + 操作审计日志

### P2-12: 错误码体系不完善

**位置**：全项目

**问题描述**：
- 异常用字符串描述，客户端难以统一处理
- 无标准错误码（如 `ERR_DB_BUSY`, `ERR_RATE_LIMIT`）

**建议**：定义 `LinkoraError(BaseException)` 层级：
```python
class LinkoraError(Exception): pass
class DBError(LinkoraError): pass
class LMError(LinkoraError): pass
class IMAdapterError(LinkoraError): pass
```

### P2-13: 配置校验不足

**位置**：`src/config.py`

**问题描述**：
- 部分配置项无范围校验（如 `interval_seconds` 负数）
- 平台 ID 无白名单校验

**建议**：添加 `model_validator` 进行跨字段校验

### P2-14: 消息合并逻辑复杂

**位置**：`src/poller_utils.py`, `src/poller_core_parse.py`

**问题描述**：
- `merge_consecutive_messages()` 有 300+ 行，逻辑嵌套深
- 多种合并触发条件（时间窗口、相同发送者、相似内容）

**建议**：提取为策略模式，每种合并逻辑一个类

### P2-15: 工具调用参数校验缺失

**位置**：`src/tools/base.py`

**问题描述**：
- 工具参数直接传给 LLM，无 schema 校验
- 危险操作（如删除消息）无二次确认

**建议**：添加参数白名单 + 危险操作确认回调

---

## 五、🟢 P3 长期优化建议

### 5.1 架构层面

1. **微服务化**：将 Poller、LLM Engine、Memory Store、Web API 拆分为独立进程，通过 gRPC/消息队列通信
2. **插件化**：IM 适配器、工具、意图识别器全部走插件接口，支持运行时热加载
3. **事件溯源**：关键状态变更写入事件日志，支持回溯和调试

### 5.2 性能优化

1. **向量检索**：考虑迁移到 Milvus/Qdrant，支持分布式和持久化
2. **消息队列**：引入 Redis/RabbitMQ，解耦消息接收和处理
3. **CDN 加速**：图片和静态资源走 CDN

### 5.3 可观测性

1. **Metrics**：集成 Prometheus，暴露关键指标（消息延迟、LLM 调用成功率、DB 连接池使用率）
2. **Tracing**：接入 Jaeger/Zipkin，追踪跨组件调用链
3. **告警**：关键错误自动发送告警（企业微信/钉钉机器人）

### 5.4 开发者体验

1. **本地开发环境**：提供 `docker-compose.dev.yml`，一键启动完整开发环境
2. **代码生成**：为常用模式（工具、意图、适配器）提供 CLI 模板
3. **Playwright 测试**：为核心流程添加 E2E 测试

---

## 六、修复优先级建议

### 第一周（P0）
- [ ] P0-1: SQLite 并发写入加锁
- [ ] P0-2: 全局状态改为 ContextVar
- [ ] P0-3: 敏感信息日志脱敏
- [ ] P0-4: 索引重建阈值调优

### 第二周（P1-1 ~ P1-4）
- [ ] 分类处理异常（网络/DB/API）
- [ ] 修复 shutdown 竞态
- [ ] 平台隔离缓存
- [ ] 飞书缓存 TTL

### 第三周（P1-5 ~ P1-8）
- [ ] 工具结果时间戳标准化
- [ ] OCR 下载大小限制
- [ ] 摘要循环失败保护
- [ ] 配置热重载原子化

### 第四周及以后（P2）
- [ ] 大文件拆分
- [ ] 补充关键路径测试
- [ ] 日志系统统一
- [ ] 类型注解补全

---

## 附录：关键指标统计

```
代码行数统计：
- src/                  44,500 LOC
- tests/                44,000 LOC
- web/                  13,000 LOC
- scripts/              3,000 LOC
- docs/                 7,000 LOC
总计：~111,500 LOC

异常捕获统计：
- 总数：502 处
- 含 noqa: BLE001：91 处
- 仅 warning 无 rethrow：约 120 处（潜在风险）

大文件统计（>500 行）：
- 15 个文件超过 500 行
- 9 个文件超过 800 行
- 最大：weather.py (939 行)
```

---

**报告生成**：Agnes AI Agent  
**审计方法**：静态分析 + 人工复核  
**下次复审建议**：2026-09-14（修复完成后）
