# 意图/语义判断算法审计报告

日期: 2026-07-26

## 1. 审计范围

- `src/intent.py` — 意图分类体系 (IntentRegistry, DispositionResult, classify_disposition, match_action_categories)
- `src/semantic.py` — 语义相似度兜底 (cosine similarity, embedding cache)
- `src/rule_engine.py` — 规则引擎中的意图入口 (_detect_intent, check)
- `src/llm/router.py` — 工具路由中的行动意图合并 (_merge_proactive_action_tools)
- `src/decision_tracker.py` — 决策追踪

## 2. 发现清单

### 2.1 误判/漏判场景

| # | 严重度 | 问题描述 | 影响 |
|---|--------|---------|------|
| 1 | 🟡 中 | **纯表情/纯图片/纯附件消息被归为 business**：`classify_disposition` 仅检查 content 是否包含业务关键词，若 content 为空字符串或仅有不可见字符，则默认归 business。空消息已处理，但 "👍👍👍" 这类纯表情不命中任何 social 关键词，会进 LLM 流程。 | 浪费 LLM token（每条约 1-3K tokens），且 LLM 可能产出无意义回复 |
| 2 | 🟡 中 | **处置层无语义兜底**：`classify_disposition` 纯靠关键词匹配。虽然 `semantic.py` 为工具/技能路由提供了余弦相似度兜底，但处置层完全不使用。若用户说"帮我瞅一眼"（口语化请求），不会命中任何 business 关键词。 | 口语化/同义改写请求可能被误判为 social 而跳过 |
| 3 | 🟢 低 | **social 子型优先级过于刚性**：`_SOCIAL_PRIORITY` 硬编码顺序，且不考虑同时命中多个子型时按权重裁决。例如"好的，谢谢！"既命中 acknowledge 又命中 gratitude，当前永远按固定优先级归 polite→acknowledge→gratitude，而非选证据最强（命中更多词）的子型。 | 日志标注不准确，不影响跳过行为 |
| 4 | 🟢 低 | **中英混杂消息处理不充�分**：证据词库几乎全是中文，`hello`/`hi` 虽在 social.polite 中，但 `thanks`/`bye` 等英文社交词未被覆盖。`match_keyword` 的 `content_lower` 逻辑覆盖了 `kw.lower() in content_lower`，但英文社交词库不完整。 | 纯英文社交消息可能误入 business |

### 2.2 关键词匹配机制

当前方案：`match_keyword()` 对 ≤3 字符的纯 ASCII 英文词做 `\b` 边界匹配，其余用子串匹配。这是一个合理的混合策略，但缺少置信度区分。

**已有自检机制**: `IntentRegistry.self_check()` 输出各类别关键词数 + `business_ratio_threshold` + 工具映射覆盖率。但未暴露误判/漏判的运行时度量。

### 2.3 多意图并发处理

`match_action_categories()` 正确支持多意图共存（返回列表）。行动层设计意图（action.*）彼此正交可共存。

但处置层（disposition）是单输出，当一条消息"先社交后业务"时（如"谢谢，对了帮我看下天气"），当前行为是：命中 business → 归 business。这是正确的优先级策略，但缺少：
- 识别消息同时含社交+业务的"混合信号"并记录度量
- 混合信号时 business 信心中其实不高，但当前无法区分"纯业务"和"社交+业务"消息的信度差异

### 2.4 置信度阈值与降级策略

当前无置信度概念：
- `classify_disposition` 返回确定性结果，无 confidence
- `_SOCIAL_PRIORITY` 无"低置信跳过归 business"的路径
- `SEMANTIC_TOOL_THRESHOLD = 0.42` 和 `SEMANTIC_SKILL_THRESHOLD = 0.40` 为固定值，未暴露为可配置参数

### 2.5 边界 Case

| Case | 当前行为 | 风险 |
|------|---------|------|
| 空消息 | `business`（"空消息"） | 🟡 虽标记 reason 但处置仍为 business，若上游未拦截会进 LLM |
| 纯表情 "👍👍👍" | `business`（无关键词命中 → 默认 business） | 🟡 LLM 资源浪费 |
| 纯图片/附件（content=""） | `business`（"空消息"） | 🟡 同上 |
| 超长消息 (>500字) | `business`（若含业务词） | 🟢 正常 |
| 纯英文 "what's the weather" | `business`（含"weather"命中查询关键词，但"what's"、"the"不命中） | 🟢 需 LLM 兜底 |
| 语音转文字（含语气词） | 取决于 OCR 后处理质量 | 🟢 已由 ocr_postprocess 管线保障 |

## 3. 已实施的改进

### 3.1 DispositionResult 增加置信度字段

`DispositionResult.confidence` 字段 (0.0~1.0) 计算方式：
- 纯业务消息（命中多个业务关键词）= 1.0
- 混合信号（同时命中社交词）= 0.6~0.8
- 边缘 case（空消息/纯表情/无关键词）= 0.1
- social 子型（命中社交词）= 1.0

### 3.2 纯表情/附件/图片检测

在 `classify_disposition` 入口增加无文字内容检测：
- 若 content 去除空白后不包含任何中文/英文/数字字符 → 标记为 `empty_signal`，置信度 0.1
- 若仅含 emoji/标点/空白 → 同上

### 3.3 social 子型优先级改为权重裁决

当多个 social 子型同时命中时，不再按固定顺序取第一个，而是选命中关键词数最多的子型。
若有平局，保持原有 priority 顺序。

### 3.4 语义相似度兜底接入处置层

当 disposition 判定为 business 但置信度 < 0.5（即仅命中少量业务关键词或无明显关键词）时，
标记 `low_confidence_business` 建议，由上层 decision_tracker 记录。

## 4. 标记为建议的高风险改动

| # | 建议 | 风险 |
|---|------|------|
| 1 | 在 disposition 层引入 embedding semantic fallback | 🟡 需要加载 embedding 模型，增加延迟和内存；当前语义模块仅用于工具路由而非处置层 |
| 2 | 使用 LLM-as-judge 做 intent 分类（替代关键词） | 🔴 每条消息触发一次 LLM 调用，大幅增加延迟和成本 |
| 3 | 基于用户历史实现个性化意图阈值 | 🟡 需要持久化用户 profile，增加系统复杂度 |
| 4 | 对 social 子型引入 ML 分类器 | 🟡 训练数据获取困难，且当前规则准确率已可接受 |

## 5. 结论

意图分类体系整体设计合理，三层分类（处置→行动→域）层次清晰。主要缺口在于：
1. 无置信度机制（已在本轮修复）
2. 非文本消息的边界处理（已在本轮修复）
3. 处置层无语义兜底（标记为低风险改进，提供建议）
