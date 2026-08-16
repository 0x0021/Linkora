# Phase 2 设计方案：RAG 引文溯源 + 置信度产品化

> 状态：设计稿（待评审）
> 作者：交付编排（团队 lead 直接产出，arch-fallback 因网络中断改由 lead 撰写）
> 日期：2026-07-25
> 关联：三阶段 roadmap ② —— 「BGE 本地离线 + 混合重排是硬差异化；把『答案来自哪段知识、置信度多少』在回复里显式呈现」

---

## 1. 一句话目标

把当前**仅内部存在、从不呈现给用户**的 RAG 检索证据（命中文档、片段、相似度）显式化：
在真实回复里可选地追加「引文来源 + 置信度」，让数字分身的回答**可溯源、可信任**，把已有的检索质量转化为**可感知的产品价值**。

零行为变更原则不适用于本阶段（这是新增能力），但必须**默认关闭、灰度开启**，不破坏现有 2125 passed 基线与回复观感。

---

## 2. 现状盘点（已读代码确认）

### 2.1 内部已具备的信号

| 信号 | 位置 | 现状 |
|------|------|------|
| `best_score`（最高相似度） | `src/llm/style.py:241` → `rag_inject.py:36` → `agent._last_kb_best_score` (`prompt_builder.py:81`) → `AgentReply.confidence` (`agent.py:472`) | ✅ 全链路已透传 |
| `evidence_source`（是否命中 KB） | `agent.py:473`（`"kb"` / `None`） | ✅ 已设置 |
| 命中文档标题 / 片段 / 分数 | `src/llm/style.py:245-256` 循环里的 `source` / `content` / `score` | ⚠️ **仅拼进文本 blob `knowledge_parts`，结构化元数据被丢弃** |
| `reply.best_chunk` | `main.py:1059` 用 `getattr` 读取，写入 draft 表 `rag_best_chunk` | ❌ **全库无任何赋值点 → 永远是 None**（占位属性，草稿溯源实际拿不到片段） |

### 2.2 已消费 confidence 的地方（不能破坏）

- `main.py:1023 _should_handoff_low_confidence`：单聊 + 开关开 + `best_score` 弱命中 → 转人工草稿。
- `main.py:1047 _notify_owner_draft`：把草稿落 `message_drafts` 表（`rag_confidence` / `rag_threshold` / `rag_best_chunk`）+ DM 通知主人。
- **关键缺口**：真实回复发送走 `main.py:1248 _send_reply(message, reply_text.text)` —— 只发纯文本，**从不附引文/置信度**。

### 2.3 另一条独立路径（勿混淆）

- `web/routers/kb.py:553` 的 `kb_chat` 问答系统提示已含「在末尾标注引用来源编号」——但那是 **Web 知识库问答页专用**，与 IM 主回复链路无关。

### 结论

要做引文产品化，**唯一根因改动点是 `style.py` 检索层**：它已经有 `source/score/snippet`，只是没把结构化列表带出来。补一条「结构化引文列表」透传链，末端在 `_send_reply` 前按开关拼接页脚即可。BGE 本地离线重排是**可选增强**，插在检索结果与引文列表之间。

---

## 3. 目标与非目标

**目标（本阶段交付）**
1. 检索层结构化输出命中片段元数据（标题、分数、片段文本）。
2. 全链路透传到 `AgentReply.citations` + 补齐 `reply.best_chunk`（顺带修好草稿溯源永空 bug）。
3. 真实 IM 回复可选追加「引文 + 置信度」页脚（默认关，配置灰度）。
4. 置信度分级话术（高/中/低）＋阈值可配。
5. （可选）Web 端一个只读面板展示最近若干条回复的引文命中情况。

**非目标（本阶段不做）**
- 不改检索算法本身的召回逻辑（除 BGE 重排作为可选插件）。
- 不做多轮引文合并 / 跨消息引用图谱。
- 不动 `kb_chat` Web 问答路径（已自带编号）。

---

## 4. 数据结构设计

### 4.1 新增 `Citation` dataclass（建议置于 `src/llm/rag_inject.py`）

```python
@dataclass
class Citation:
    source: str          # 文档标题（style.py 的 r["source"]）
    score: float         # 相似度 0..1
    snippet: str         # 命中片段（extract_relevant_snippets 首条，截断 ~80 字）
    doc_id: str | None = None  # 若 kb_search 结果含 id 则带上，便于前端跳转
```

### 4.2 透传链改动（最小侵入）

```
style.retrieve_relevant_knowledge()
    ── 现: return (text, best_score)
    ── 改: return (text, best_score, citations: list[Citation])

rag_inject.RagInjectResult
    ── 新增字段: citations: list[Citation] = field(default_factory=list)

prompt_builder（第 81 行附近）
    ── agent._last_kb_citations = rag_result.citations   # 新增 thread-local 属性

agent.AgentReply
    ── 新增: citations: list[Citation] = field(default_factory=list)
    ── 新增/补齐: best_chunk: str | None = None

agent._mk_reply()（第 472-473 行块内）
    ── reply.citations = getattr(self, "_last_kb_citations", []) or []
    ── reply.best_chunk = reply.citations[0].snippet if reply.citations else None
```

> `_last_kb_citations` 须与 `_last_kb_best_score` 一样放 `self._tl`（threading.local），并在 `prompt_builder` 每轮重置（对齐第 64 行 `agent._last_kb_best_score = None` 的清零逻辑），避免并发请求串味。

---

## 5. 呈现层设计（真实回复页脚）

### 5.1 插入点

`main.py:1912` 分支 `elif reply_text.text:` 内，`_send_reply(message, reply_text.text)` **之前**，按配置将 `reply_text.text` 拼接引文页脚。

### 5.2 页脚话术（置信度分级）

| 分级 | best_score 区间 | 页脚样式（示例） |
|------|----------------|------------------|
| 高 | ≥ `high`（默认 0.75） | `\n\n—— 依据：《{标题}》（相关度 88%）` |
| 中 | `[low, high)` | `\n\n—— 参考来源：《{标题}》（相关度 62%）` |
| 低 / 未命中 | `< low` 或 None | **不追加页脚**（低置信本就走转人工草稿，避免误导） |

- 多引文时最多列 2 条，形如 `依据：《A》(88%)、《B》(80%)`。
- 群聊默认更克制（可单独开关 `citation_in_group`，默认关）。

### 5.3 配置项（`config.llm.advanced` 下新增，全部默认保守）

```yaml
llm:
  advanced:
    citation_enabled: false          # 总开关，默认关
    citation_in_group: false         # 群聊是否附引文
    citation_high_threshold: 0.75    # 高置信阈值
    citation_low_threshold: 0.50     # 低于此不附页脚（与 low_confidence_threshold 复用/独立均可）
    citation_max_items: 2            # 最多列几条
```

---

## 6. BGE 本地离线混合重排（可选增强）

**定位**：不是本阶段必须项，是「硬差异化」的加分项，可作为独立小任务在页脚能力稳定后接入。

- **插入点**：`style.py:236` 拿到 `result["results"]` 之后、`best_score` 计算之前，加一层重排：
  用本地 BGE reranker（如 `bge-reranker-base`，离线权重）对 `(query, chunk)` 打分，重排 top_k 后再取 best_score / 生成 citations。
- **离线保证**：模型权重随部署包/首启下载缓存到本地，运行期不出网 → 私有、不泄密、可离线（正是差异化卖点）。
- **降级**：BGE 加载失败或超时 → 回退原向量分数排序（best-effort，记 `[resilience]` 日志），绝不阻塞回复。
- **开关**：`llm.advanced.rerank_enabled: false` 默认关，避免默认引入 torch 推理开销。
- **性能**：reranker 为同步阻塞调用 → 若在 web 侧触发须包 `run_in_threadpool`（对齐 T6）；IM 主链路本就在 poller 线程，直接调用即可但要设超时。

---

## 7. 任务分解（有序，含依赖）

| # | 任务 | 文件 | 依赖 | 验收 |
|---|------|------|------|------|
| P2-1 | 定义 `Citation` + 扩展 `retrieve_relevant_knowledge` 返回结构化引文 | `src/llm/style.py`、`src/llm/rag_inject.py` | — | 单测：命中时 citations 非空且字段正确 |
| P2-2 | `RagInjectResult.citations` + `prompt_builder` 透传 + thread-local 重置 | `rag_inject.py`、`prompt_builder.py` | P2-1 | 单测：并发不串味 |
| P2-3 | `AgentReply.citations` + 补齐 `best_chunk` + `_mk_reply` 赋值 | `src/llm/agent.py` | P2-2 | 单测：reply.citations / best_chunk 正确；**修好草稿 rag_best_chunk 永空 bug** |
| P2-4 | 回复页脚拼接 + 分级话术 + 配置项 | `main.py`、`src/config.py`、`config.yaml.example` | P2-3 | 开关关=零变更；开=高/中命中有页脚、低/未命中无 |
| P2-5 | 回归：全量 pytest + 新增引文用例 | `tests/` | P2-4 | 基线 2125 不退化 + 新用例绿 |
| P2-6（可选） | BGE 本地离线重排插件 + 开关 + 降级 | `src/llm/style.py`、新模块 `src/llm/rerank.py` | P2-4 | 重排开=命中顺序改善；关/失败=回退不崩 |
| P2-7（可选） | Web 只读引文面板 | `web/routers/*`、前端 | P2-4 | 展示最近 N 条回复的命中引文 |

---

## 8. 决策点（需用户拍板）

1. **默认开还是默认关？** 建议**默认关**（`citation_enabled: false`），单聊先灰度，观感确认后再放开群聊。
2. **阈值是否复用 `low_confidence_threshold`？** 建议独立配置，避免转人工阈值与展示阈值耦合。
3. **BGE 重排是否本阶段做？** 建议拆为 P2-6 独立后续任务，先交付 P2-1~P2-5 的「引文页脚」闭环，快速见效。
4. **页脚样式**：`—— 依据：《标题》（相关度 88%）` 是否符合品牌调性？可调整措辞/emoji。

---

## 9. 风险与规避

| 风险 | 规避 |
|------|------|
| 页脚干扰正常闲聊观感 | 默认关 + 低置信不追加 + 群聊单独开关 |
| 引文标题冗长/含敏感内容 | 标题截断 + 复用现有敏感词过滤（`_filter_sensitive_words`）也覆盖页脚 |
| 并发请求 citations 串味 | 严格走 `self._tl` thread-local + 每轮重置（对齐既有 best_score 处理） |
| BGE 引入 torch 冷启动/内存 | 默认关；懒加载；加载失败降级；超时保护 |
| 破坏 2125 测试基线 | 开关关时全链路 no-op；P2-5 强制全量回归 |

---

## 10. 交付节奏建议

1. **先做 P2-1 ~ P2-5**（引文页脚闭环），一个 commit 一步，零行为变更靠开关兜底。
2. 用户灰度确认观感后，再评估 **P2-6 BGE 重排**（硬差异化）与 **P2-7 Web 面板**。
3. 每步 `py_compile` → 针对性回归 → 全量回归 → 中文 `type(scope)` 独立提交。
