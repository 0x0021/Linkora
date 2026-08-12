# RAG 分块逻辑语义化重构

## 目标
把原来的「固定长度硬截断」分块，改成「固定长度作软目标/上限参考、遵循语义边界」的分块，避免在句子/子句中间被切断。

## 改动清单

### 1. `src/tools/utils.py` — 核心重写 `split_text`
- `max_len` 含义变更：**软目标 / 上限参考**，而非硬上限。块尽量贴近该长度，但**不切断语义单元**。
- 新增 `hard_max: int | None = None` 参数：安全天花板，默认 `max(max_len*2, 800)`。仅在病态超长单元（巨型无标点段落 / URL / 哈希）触发，且仍优先在最佳语义边界断开；无任何更细边界时才字符级兜底。
- 新增辅助函数：
  - `_atoms(text, pat)`：按边界正则切原子片段，分隔符附在前一片段末尾（拼接无损还原）。
  - `_pack_atoms(atoms, limit)`：贪婪拼装 ≤ limit 的块，仅在原子之间断开。
  - `_split_recursive(text, pats, hard_max)`：逐层细化（句子 → 子句 → 空白 → 字符兜底），保证每块 ≤ hard_max 且内部不被语义外切断。
- 边界优先级（高→低）：段落空白 → 句子结束符（`。！？!?…` 及 ASCII `. `，避开 `github.com` 类 URL）→ 子句分隔符（`，,；;：:、—–`）→ 空白 → 字符级。
- 标题粘连保留（`# 标题`/`第X章`/`1. xxx`/`一、xxx` 与下一行同块）。
- 重叠（overlap）仍支持，且整体 capped 在 `hard_max` 内。

### 2. `src/config_models.py` — 新增可配置项
- `RagConfig.chunk_hard_max: int | None = None`
- `PlatformRagConfig.chunk_hard_max: int | None = None`
- 默认 `None` → `split_text` 派生为 `chunk_size*2`，非破坏性。

### 3. `config.yaml.example`
- `rag.chunk_hard_max` 注释说明（建议设在 embedding 模型有效字符容量之下，杜绝模型侧截断）。

### 4. 调用点接入 `hard_max`
- `src/doc_sync_scheduler.py`：新增 `config` 构造参数（由 `primary.py` 注入 `self.config`），`_sync_single_doc` 读取 `rag.chunk_hard_max`。
- `src/kb/feishu_importer.py`：`import_feishu_doc` 优先取 `rag_config["chunk_hard_max"]`，否则回退到 `config.rag.chunk_hard_max`。

### 5. `tests/test_split_text_semantic.py`（新增，10 例）
覆盖：无中间截断、软目标允许超长句整段保留、安全天花板约束病态输入、句子/子句边界收尾、标题粘连、URL 不被点切断、重叠不超天花板、常规文档多块分布。

## 验证
- 既有 `TestSplitText` + `test_utils_edge` 全部保持通过（14 例）。
- 新增语义测试 10 例通过。
- 相关回归（`feishu`/`doc_sync`/`config`/`split`/`utils`/`chunk`/`importer`）共 **446 passed**。
- 冒烟：一段 VPN 接入文档（max_len=60）被切成多块，每块均以句号/逗号收尾，无中间截断。

## 行为对比（示例）
| 场景 | 旧实现 | 新实现 |
|---|---|---|
| 600 字无标点长句，max_len=200 | 整句作为 1 个超长块（可能超 embedding 上限被模型截断） | 仍整句保留（软目标），但若超 `hard_max` 则在最佳边界断开 |
| 多句正常文档 | 达到 N 字符即硬切，可能切断句子 | 在句子/子句边界切，块长围绕 N |
| 巨型 URL/哈希 | 不处理，整块超大 | `hard_max` 天花板拦截，字符级兜底（仍尽量靠边界） |

## 已知限制 / 备注
- `_clean_text` 会剥除 `# ` 与 `1. ` 编号，故 markdown `# 标题`、有序列表 `1.` 标题无法被 heading-glue 识别；仅「第X章 / 1. / 一、」等保留样式可粘连（既有行为，非本次引入）。
- `hard_max` 默认值（= `chunk_size*2`）对 BGE 类 512-token 模型偏大；如需彻底杜绝模型侧截断，请在配置中显式设更小的 `chunk_hard_max`。
