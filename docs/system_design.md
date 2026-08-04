# Linkora(灵桥) · Token 优化增量设计：H2-A 后台异步摘要 + H5/H6 降轮次

> 作者：高见远（软件架构师） · 项目：Linkora 钉钉/多 IM Python AI bot 后端（Python 3.14）
> 性质：**已有项目增量改造**（非全新设计）；已落地的 H4/H8、H2-B、H1、per-turn H4 不再重复。
> 输出语言：中文

---

## 0. 关键现状发现（决定设计走向，必读）

阅读源码后确认一个**决定性事实**，它直接约束 H2-A 是否真的能触发：

| 项 | 当前值（grep 确认） | 后果 |
|----|----|----|
| `history_window`（部署值，H2-B 已落） | dingtalk=5 / feishu=6 / wecom=6 | 拉历史窗口（DB 读取上限） |
| `_apply_history_tiering` 的 `max_recent`（硬编码默认） | `6` | 触发摘要条件：`len(history) > max_recent` |
| 实际 `len(history)` | `= min(history_window, 真实消息数)` | 被 `history_window` 截断 |

**结论**：对所有三个平台，`history_window (5/6/6) ≤ max_recent (6)`，因此 `len(history) ≤ 6` 恒成立 → `_apply_history_tiering` 的 `else` 摘要分支**当前是死代码**（详见 `agent.py:575-597`：`if len(history) <= max_recent: return history`）。

这意味着：**H2-A 的异步摘要，在当前部署值下根本不会触发**。要让 H2-A 产生真实收益，必须让 `max_recent < history_window`（使摘要分支可达）。这把 H2-A 与 H5/H6 强耦合——降轮次（调小 `max_recent`）正是激活 H2-A 的前提。本设计的 H5/H6 推荐值即围绕这个耦合给出。

---

## 1. 实现方案 + 框架选型

### 1.1 线程模型选型（H2-A 核心）

| 方案 | 评估 | 结论 |
|------|------|------|
| asyncio 协程 | 全代码库是**同步 + `threading.local()`** 模型；`agent._tl` 用 `threading.local()`，异步事件循环会打破该约定，且 `SQLiteStore` 是 per-thread 连接 | ❌ 违背既有跨线程约定，排除 |
| 线程池 `ThreadPoolExecutor` | 允许同一 `chat_id` 并发跑两个摘要 → 写回竞态（两条消息并发改同一份 history 摘要） | ❌ 需额外加锁，复杂度高 |
| **单后台 daemon 线程 + 队列（串行）** | 与现有 `DatabaseBackupCoordinator`（单 daemon 线程、`_stop_event`、`_run_queue` 串行）风格**完全一致**；同一 chat 天然串行，写回无并发竞态 | ✅ **采用** |

**选型结论**：新增 `SummaryScheduler`——单 daemon 线程 + `queue.Queue` + per-chat `pending` 去重集合（锁保护）。完全对齐 `DatabaseBackupCoordinator`（`src/db_backup.py:191`）。

### 1.2 异步落库一致性边界（H2-A 重点 ①）

- **热路径只读不写**：主回复链路中 `_apply_history_tiering` 只做**同步的快读** `store.get_conversation_summary(chat_id)`（本地 DB 读，无 LLM），随后**立即返回**主回复所需的历史，**不等**摘要 LLM。
- **写回在后台**：摘要计算与 `upsert` 全部发生在 `SummaryScheduler` 的 daemon 线程。该线程使用**自己的 per-thread SQLite 连接**（WAL 已开启，`busy_timeout=5000`），与主请求线程的连接隔离。
- **写回原子性**：先 `summarize_conversation` 拿到完整字符串 → 仅在拿到非空摘要后执行**单条 UPSERT**（SQLite 事务原子）→ 不存在"半成品行"。
- **CAS 代际（状态机）**：`conversation_summaries` 行带 `generation` 整型。Worker 读取当前 `gen`，计算 `gen+1`，执行 `UPDATE ... WHERE chat_id=? AND generation=?`；若影响行数=0（理论不可能，但作防御）则跳过写。这是显式的"乐观锁/状态机"边界。
- **同 chat 并发双写防护**：`pending: set[chat_id]` + `threading.Lock`。同一 chat 在 in-flight 期间**只入队一次**；配合单 worker FIFO，物理上不可能对同 chat 并发写。

### 1.3 失败兜底（H2-A 重点 ②）

- 主回复**永远先发**：调度动作（`queue.put`）在构造完主回复消息之后、或与其并行；主回复链路**从不 `await`/`join`** 摘要线程。
- LLM 超时/异常：`summarize_conversation` 既有实现已 `return ""`（`agent.py:1853`）。Worker 内 `if not summary: return`，**不写库**，缓存保持旧值（或仍为空）——主回复不受影响、无脏数据。
- DB 写异常：包裹 try/except，仅记日志，不影响主线程。

### 1.4 触发条件（H2-A 重点 ④）

**保持 `_apply_history_tiering` 的 `len(history) > max_recent` 作为触发语义不变**（不改为基于 `history_window` 跨度）。理由：
- 触发判定与"注入轮次"概念一致（超过 `max_recent` 条才需压摘要），语义最清晰；
- `history_window` 只负责"能从 DB 读到多少原料"，二者解耦后，H5/H6 调 `max_recent` 即可独立激活摘要。

### 1.5 一句话流程（H2-A）

> 旧：`计算摘要(同步等 LLM) → 用摘要拼历史 → 发回复`
> 新：`读缓存摘要(快) → 若有且新鲜则用之 / 若无则降级(recent 仅) → 发回复 → 后台线程算摘要写回(供下一轮)`

---

## 2. 文件列表（相对路径，标注 新增/修改）

| 路径 | 操作 | 说明 |
|------|------|------|
| `src/llm/summary_scheduler.py` | **新增** | `SummaryScheduler` 单 daemon 线程 + 队列 + pending 去重 + 写回（CAS） |
| `src/memory/sqlite_store.py` | **修改** | ① 新增 `conversation_summaries` 表迁移；② 新增 `get_conversation_summary` / `upsert_conversation_summary` |
| `src/llm/agent.py` | **修改** | `_apply_history_tiering` 改为`读缓存→降级→调度`；新增 `_read_cached_summary` / `_maybe_schedule_summary`；`__init__` 接收 `summary_scheduler`；`max_recent` 改从配置读取；`summarize_conversation` 保持可后台调用（不动逻辑） |
| `src/config.py` | **修改** | `AdvancedConfig` 新增 `history_tiering_recent` / `summary_async_enabled` / `summary_max_age_seconds` / `summary_min_coverage_ratio` / `summary_max_messages` / `summary_min_older` |
| `main.py` | **修改** | 每个平台 `LLMAgent` 创建后 `SummaryScheduler(agent, store).start()`；app 退出 `stop()`（对齐 `_start_backup_scheduler` 风格 `main.py:379`） |
| `tests/test_async_summary.py` | **新增** | 异步摘要不阻塞、失败后不脏写、同 chat 并发不双写、缓存命中/降级路径 |
| `tests/test_history_tiering.py` | **新增** | `max_recent` 降级回归（沿用 `test_rag_gating.py` 既有断言风格） |
| `docs/system_design.md` / `docs/sequence-diagram.mermaid` / `docs/class-diagram.mermaid` | **新增** | 本文档与图 |

---

## 3. 数据结构和接口

### 3.1 新增配置（`src/config.py` · `AdvancedConfig`）

```python
# H5/H6 + H2-A 统一阈值（**具体数值见 §8 待明确事项，由用户拍板**）
history_tiering_recent: int = 6          # 注入 LLM 的"近期完整"条数（原硬编码 6）
summary_async_enabled: bool = True       # H2-A 总开关
summary_max_age_seconds: int = 600       # 缓存摘要新鲜度窗口（秒）
summary_min_coverage_ratio: float = 0.6  # 缓存摘要须覆盖≥60%当前 older 才采用
summary_max_messages: int = 0            # 透传给 summarize_conversation 的 max_messages
summary_min_older: int = 2               # older 段至少 N 条才压摘要（沿用原 `len(older)>=2`）
```

### 3.2 新增表（`src/memory/sqlite_store.py` migrate）

```sql
CREATE TABLE IF NOT EXISTS conversation_summaries (
    chat_id                 TEXT PRIMARY KEY,
    summary_text            TEXT NOT NULL,
    older_boundary_msg_id   TEXT NOT NULL,   -- 被摘要覆盖的 older 段"最新一条"msg_id
    covered_count           INTEGER NOT NULL,-- 被摘要覆盖的消息条数
    generation              INTEGER NOT NULL DEFAULT 0,  -- CAS 代际
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
```

### 3.3 关键函数签名

```python
# ---- src/llm/summary_scheduler.py (新增) ----
@dataclass
class SummaryJob:
    chat_id: str
    older: list[Message]
    generation: int
    created_at: str

class SummaryScheduler:
    def __init__(self, agent: "LLMAgent", store: "SQLiteStore") -> None: ...
    def start(self) -> None: ...                       # 起 daemon 线程，立即返回
    def stop(self) -> None: ...                        # _stop_event.set()
    def schedule(self, chat_id: str, older: list[Message]) -> None:
        """非阻塞：pending 去重后 queue.put(SummaryJob)"""
    def _worker_loop(self) -> None: ...                # while not stop: job=queue.get(); _process_job(job)
    def _process_job(self, job: SummaryJob) -> None:
        # 1. summary = self._agent.summarize_conversation(job.older, ...)
        # 2. if not summary: return                      # 失败兜底，不写
        # 3. ok = self._store.upsert_conversation_summary(...)  # CAS
        # 4. self._pending.discard(chat_id)

# ---- src/memory/sqlite_store.py (修改，新增两个方法) ----
def get_conversation_summary(self, chat_id: str) -> "SummaryRow | None":
    """读 chat 缓存摘要（快，本地）。返回 None 表示无缓存。"""
def upsert_conversation_summary(self, chat_id: str, summary: str,
                                older_boundary_msg_id: str, covered_count: int) -> bool:
    """CAS 写回：UPDATE ... WHERE chat_id=? AND generation=?；影响行数>0 返回 True。"""

# ---- src/llm/agent.py (修改) ----
class LLMAgent:
    def __init__(self, ..., summary_scheduler: "SummaryScheduler | None" = None):
        self._summary_scheduler = summary_scheduler
        # max_recent 改从 self._history_tiering_recent 读取（_cache_advanced_config）

    def _apply_history_tiering(self, history, max_recent=None) -> list[Message]:
        max_recent = max_recent or self._history_tiering_recent
        if len(history) <= max_recent:
            return history
        recent = history[-max_recent:]
        older = history[:-max_recent]
        if older and len(older) >= self._summary_min_older:
            cached = self._read_cached_summary(history[0].chat_id, older)
            if cached:
                return [cached] + recent
            # 缓存未命中：降级为仅 recent（安全），并异步补算
            self._maybe_schedule_summary(history[0].chat_id, older)
        return recent

    def _read_cached_summary(self, chat_id, older) -> "Message | None":
        row = self.store.get_conversation_summary(chat_id)
        if not row: return None
        age = now - row.updated_at
        coverage = row.covered_count / max(1, len(older))
        if age <= summary_max_age_seconds and coverage >= summary_min_coverage_ratio:
            return Message(msg_id=f"summary_{row.covered_count}", chat_id=...,
                           chat_type=..., content=f"[摘要]{row.summary_text}",
                           sender_name="系统", sender_id="system", role="system",
                           created_at=history[0].created_at)
        return None

    def _maybe_schedule_summary(self, chat_id, older) -> None:
        if self._summary_scheduler and self._summary_async_enabled and older:
            self._summary_scheduler.schedule(chat_id, older)

    def summarize_conversation(self, messages, max_messages=0) -> str:
        # 保持不变（既有的同步 LLM 调用；现在既被后台线程调用，也被需要时同步调用）
```

> 类图见 `docs/class-diagram.mermaid`；时序图（含 4 个场景）见 `docs/sequence-diagram.mermaid`。

---

## 4. 程序调用流程（要点）

完整时序见 `docs/sequence-diagram.mermaid`，覆盖 4 个场景：
1. **缓存命中**：热路径零 LLM 阻塞（H2-A 主收益），发完回复后顺带入队补算。
2. **缓存未命中**：降级为 `recent` 仅（安全不失忆），主回复不阻塞，后台线程算完写回供下一轮。
3. **异步摘要失败兜底**：`summarize_conversation` 返回空 → Worker 不写库，缓存保持旧值，主回复早已发出。
4. **同 chat 并发两回合**：`pending` 去重 + 单 daemon 线程 FIFO → 同一 chat 物理串行写回，无双写/错乱。

---

## 5. 任务列表（有序、含依赖、按实现顺序排列）

> 约束：≤5 个任务；T01 为基础设施；每任务 ≥3 文件；尽量仅依赖 T01。
> 既有测试 `test_feishu_wecom_history_window_le_6` / `test_history_window_le_5` / RAG 护栏类保持不动，新测试独立文件。

- **T01 · 数据层 + 基础设施**【P0】
  - 源文件：`src/memory/sqlite_store.py`、`src/config.py`、`src/llm/summary_scheduler.py`(新)
  - 内容：① `conversation_summaries` 表迁移；② `get_conversation_summary`/`upsert_conversation_summary`；③ `AdvancedConfig` 六个新字段（带默认值）；④ `SummaryScheduler` 骨架（start/stop/schedule/worker_loop/process_job，写回逻辑齐全）。
  - 依赖：无 · 优先级 P0

- **T02 · H2-A 异步摘要接线**【P0】
  - 源文件：`src/llm/agent.py`、`src/memory/sqlite_store.py`(配合)、`main.py`
  - 内容：① `_apply_history_tiering` 改为 `读缓存→降级→调度`，`max_recent` 改读配置；② 新增 `_read_cached_summary`/`_maybe_schedule_summary`；③ `LLMAgent.__init__` 接收 `summary_scheduler`；④ `main.py` 每平台 `SummaryScheduler(agent, store).start()`，退出 `stop()`。
  - 依赖：T01 · 优先级 P0

- **T03 · H5/H6 降轮次 + 质量护栏**【P1】
  - 源文件：`src/config.py`、`src/llm/agent.py`、`tests/test_history_tiering.py`(新)
  - 内容：① `history_tiering_recent` 默认调为推荐值（见 §8，经配置暴露）；② `summary_min_coverage_ratio` 护栏；③ 新增降级回归测试。**具体数值以 §8 用户拍板为准**，代码中用配置默认值，不写死 magic number。
  - 依赖：T01, T02 · 优先级 P1

- **T04 · 异步摘要测试 + 集成验证**【P1】
  - 源文件：`tests/test_async_summary.py`(新)、`src/llm/summary_scheduler.py`、`src/llm/agent.py`
  - 内容：① 异步不阻塞主回复（计时断言）；② 失败后不脏写（DB 无新行）；③ 同 chat 并发不双写（`pending` 去重 + generation CAS）；④ 缓存命中/降级路径；⑤ 运行需 `KMP_DUPLICATE_LIB_OK=TRUE`（macOS 导入 torch）。
  - 依赖：T02, T03 · 优先级 P1

---

## 6. 依赖包列表

**无新增第三方依赖。** 全部使用标准库 + 既有依赖：
- `threading`（标准库，daemon 线程 / `Event` / `Lock`）
- `queue.Queue`（标准库，任务队列）
- 既有：`sqlite3`（标准库，WAL）、`dataclasses`（标准库）、`pytest`（测试，已装）

> 不引入 asyncio / 线程池第三方库，保持与 `DatabaseBackupCoordinator` 一致的同步线程风格。

---

## 7. 共享知识（跨文件约定，尤其跨线程状态一致性）

1. **`threading.local()` 约定不可破**：`agent._tl` 是 per-request 线程局部状态；`LLMAgent` 实例被多平台/多请求共享，**不得在后台线程读写 `self._tl`**。`SummaryScheduler._process_job` 只调用 `agent.summarize_conversation`（仅用 `self.client` + `logger`，不碰 `_tl`）——已确认 `summarize_conversation` 不读 `_tl`/`_cache_*`。
2. **SQLite 连接隔离**：`SQLiteStore.conn` 是 per-thread 连接 + WAL。后台 Worker 必须经由 `store` 方法（其内部走 per-thread 连接），**不要**在 Worker 里持有/复用主线程的 cursor。
3. **LLMClient 线程安全**：主回复链路已以 `max_concurrent_replies=4` 并发调用 `client.chat`，故 `client.chat` 在后台线程调用安全。
4. **写回一致性三件套**：① 单 daemon 线程串行 ② per-chat `pending` 去重（锁保护）③ `generation` CAS。三者共同保证"同一 chat 摘要不会被并发改写/丢失"。
5. **不写半成品**：仅当 `summarize_conversation` 返回非空才执行 UPSERT；摘要占位 `Message` 只在**读缓存**路径由存储文本构造，绝不由"正在计算的"中间值构造。
6. **部署生效方式**：`history_window` 是部署值（gitignored `config.yaml`，需重启）；新增的 `AdvancedConfig` 阈值同理走配置，重启生效。H2-B 的既有值不受影响。
7. **测试环境**：macOS 导入 torch 需 `KMP_DUPLICATE_LIB_OK=TRUE`（pytest 运行前置）。
8. **既有测试不回归**：H2-B 的 `test_feishu_wecom_history_window_le_6`、`test_history_window_le_5` 及 RAG 护栏测试保持不变；新逻辑用新文件覆盖。

---

## 8. 待明确事项（H5/H6 具体降级数值 — **请用户拍板**）

> 硬前提：任何降级不得导致连续追问失忆、跨轮指代消解失败（"它""刚才那个"）、长任务上下文丢失。以下给出**推荐值 + 质量影响评估**，方案中不写死实现值，全部经 `AdvancedConfig` 配置暴露。

### 8.1 `max_recent`（现硬编码 6）→ 推荐降到 4

- **推荐区间：3–5，默认推荐 4。**
- 理由：
  - 注入 LLM 的"近期完整"条数从 6→4，直接削 system+history token（每轮省约 2 条消息，≈160–300 tokens，取决于均值长度）。
  - **质量护栏 - 保证不失忆的最小阈值**：`max_recent ≥ 4` = 保留最近 **2 个完整 Q&A 对**，足以支撑「它/刚才那个」这类**1 轮内**指代消解与连续追问。降到 3 仅留 1.5 对，2 轮+ 指代风险上升。
  - **长任务**：当前长任务的工作上下文靠"近期完整条"承载；如需更强保障，可保留 5–6（但收益递减）。异步摘要成熟后，older 段由新鲜摘要兜底，`max_recent` 取 4 即可。
- **⚠️ 与 H2-A 的耦合（决定性）**：要让摘要分支**可达**，必须满足 `max_recent < history_window`。`max_recent=4` 时：
  - feishu/wecom `history_window=6` → 6>4 ✅ 摘要可触发（需 ≥6 条消息时 older=2 条起）。
  - dingtalk `history_window=5` → 5>4，但 older 段 = `history[:1]`，不足 `summary_min_older=2` → **dingtalk 仍不触发摘要**。需把 dingtalk 窗口提到 **6**（见 8.2）。

### 8.2 `history_window`（现 dingtalk=5 / feishu=6 / wecom=6）→ 是否再降？

- **推荐：不进一步降；反而建议 dingtalk 5→6 以激活摘要**。
- 量化权衡：
  - `history_window` 是**原料窗**（决定能读到多少 older 供摘要）。若 `history_window < max_recent + summary_min_older`，摘要分支永久死代码（即当前现状）。
  - 继续降 `history_window` 的边际收益极小（H2-B 已从 20→6，主体收益已吃），但会继续饿死摘要原料、且直接缩小"无缓存时"的兜底历史，质量风险上升。
  - **推荐统一策略**：`history_window = max_recent + 2`（即 4+2=6）。故：feishu/wecom 维持 6；**dingtalk 5→6**（轻微回调 H2-B，但净注入 recent=4 < 原 5/6，总 token 仍降）。
- **决策点**：是否接受 dingtalk 窗口 5→6？若坚持 dingtalk 维持 5，则 dingtalk 不享受异步摘要（仅享 `max_recent` 降级），可接受但需明确。
- 若用户坚持进一步降总 token，唯一安全路径是**同时**降 `max_recent` 与 `history_window` 并保持 `window = recent + 2`（例如 recent=3, window=5），但 recent=3 的失忆风险见 8.1。

### 8.3 摘要新鲜度 / 采用阈值（配置默认，可调）

- `summary_max_age_seconds=600`（10 分钟）：超过则视为过期，降级为 recent 仅并触发补算。交互式对话 10 分钟内同 chat 必有新摘要，兜底安全。
- `summary_min_coverage_ratio=0.6`：缓存摘要须覆盖当前 older 的 ≥60% 才采用；否则降级（避免"旧摘要漏掉新 older"导致失忆）。
- 这两项均为**保守默认**，可在配置中按需收紧/放松，不阻塞实现。

### 8.4 决策清单（请用户勾选）

| 项 | 选项 A（推荐） | 选项 B | 选项 C |
|----|----|----|----|
| `max_recent` | **4** | 3（更激进） | 5（更保守） |
| dingtalk `history_window` | **6**（激活摘要） | 维持 5（dingtalk 不享摘要） | — |
| feishu/wecom `history_window` | **维持 6** | 维持 6 | — |
| `summary_max_age_seconds` | **600** | 300 | 900 |
| `summary_min_coverage_ratio` | **0.6** | 0.5 | 0.7 |

> 架构侧意见：选 A 整套（max_recent=4 + dingtalk 窗口 6 + 其余维持）为「收益/质量」最佳平衡点，H2-A 对三平台全部生效，且不触发失忆护栏。

---

## 9. Anything UNCLEAR（假设与不确定）

1. **`Message` 构造字段**：`_apply_history_tiering` 原代码已用 `Message(msg_id, chat_id, chat_type, content, sender_name, sender_id, role, created_at)`（`agent.py:586`），假设该签名仍有效（已确认 `from src.models import Message`）。
2. **多平台 scheduler 实例**：每个平台有独立 `LLMAgent` + 独立 `SQLiteStore`，故**每平台一个 `SummaryScheduler`**（与每平台一个 `DatabaseBackup` 一致）。假设主程序按平台循环创建（已见 `main.py:471` 平台化构造）。
3. **`summarize_conversation` 是否读 `_tl`**：已静态确认不读；仅用 `self.client`/`logger`。若未来该方法被改为依赖 `_tl`，需在 Worker 中补全线程局部初始化——已写入 §7 约定。
4. **`covered_count` 单位**：以"被摘要覆盖的 older 消息条数"计，用于 `coverage` 比例判定；`older_boundary_msg_id` 作调试/未来精确校验用，当前仅存不强制校验。
5. **配置落地位置**：新增字段放在 `AdvancedConfig`（与 `rag_max_results` 等同级），经 `config.yaml` 的 `advanced:` 段覆盖，重启生效——与 H1/H2-B 既有部署方式一致。
