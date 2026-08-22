# 合同审查技能执行清单

执行本技能时按下列步骤逐项核对，确保流程与 SKILL.md 一致。

## 流程核对

| 步骤 | 动作 | 脚本/依赖 | 核对项 |
|------|------|-----------|--------|
| **Step 0** | 获取当前用户 | `dws contact user get-self` | [ ] 已取得 corpId、userId（staffId） |
| **Step 1** | 查询审查权益 | `scripts/query_benefit.py --corp-id <corpId>` | [ ] 已调用脚本；若权益全为 0 则已告知留资并结束，否则进入 Step 2 |
| **Step 2** | 上传文件 | `scripts/upload_file_to_dingpan.py --file ... --corp-id ... --staff-id ...` | [ ] 已取得 spaceId、fileId、fileName、fileType、fileSize |
| **Step 3** | 合同解析 | `scripts/analysis_contract.py`（带钉盘文件信息 + corp-id、user-id） | [ ] 已取得 reviewType、reviewPosition、companyList、wordCount |
| **Step 4** | 展示并选择审查方式 | 无脚本 | [ ] 已展示解析结果；可选方式 = AI 推荐 ∩ 有权益；用户已确认立场、审查方式、结果类型（及可选自定义要求） |
| **Step 5** | 提交审查 | `scripts/create_contract_review.py`（带用户确认参数 + 钉盘文件信息） | [ ] 已取得 taskId、planFinishTime |
| **Step 6** | 审查结果 | 分支处理 | 见下表 |

### Step 6 分支核对

| 审查方式 | 动作 | 核对项 |
|----------|------|--------|
| **AI_REVIEW** | `scripts/query_review_result.py ... --file-name <fileName> --poll` | [ ] 已传入 Step 2 的 fileName；[ ] 已执行轮询脚本；[ ] 已读取 `output/<文件名基础>_review_result.json`（与当前合同对应）；[ ] 已根据 result 生成《审查结果报告》并呈现 |
| **HUMAN_RECHECK / HUMAN_REVIEW** | 不轮询 | [ ] 已告知用户 planFinishTime，并提示届时到钉钉合同助手查询结果 |

## 其他约定

- [ ] 全程未自动打开浏览器，仅以文案告知链接/入口
- [ ] 所有脚本均从本技能 `scripts/` 目录调用，参数与 SKILL.md 一致

---

## 报告撰写参考（审查结果数据结构）

报告数据来源：本技能标准产出目录 **`output/`** 下、与用户上传合同文件名对应的 JSON：**`output/<上传文件名（去扩展名）>_review_result.json`**（调用 `query_review_result.py` 时需传 `--file-name <fileName>`，fileName 来自 Step 2 上传返回）。生成《审查结果报告》时，**必须读取与当前合同对应的该文件**（通过文件名识别是哪份合同的报告），从根节点下的 `result` 取数。

### 审查结果 JSON 结构（与 SKILL 一致）

| 路径 | 类型 | 说明 |
|------|------|------|
| `result` | Object | 审查结果主体，以下字段均在其下 |
| `result.summary` | Object / String | 审查总结；报告中的「总结」文案取 `summary.summary` 或等价字段 |
| `result.summary`（风险等级） | - | 审查总结中的风险等级（高/中/低），用于报告「审查总结」的风险等级行 |
| `result.annotations` | Array | 逐条风险批注；每项含原文、修订建议、风险等级等 |
| `result.annotations[].originalText` | String | 原文摘要，对应报告表格「原文摘要」列 |
| `result.annotations[].commentTexts.remark` | String | 审查意见/修订建议，对应报告表格「审查意见」列 |
| `result.annotations[]`（风险等级） | String | 单条批注的风险等级，对应报告表格「风险等级」列（可用图标见下） |
| `result.clearWordPath` | String | 清洁版文档下载地址，报告「文档下载」中「清洁版」 |
| `result.wordPath` | String | 批注版/修订版文档下载地址，报告「文档下载」中「批注版」 |

### 风险等级图标（与 SKILL 一致）

- 高风险 = 🔴
- 中风险 = 🟡
- 低风险 = 🟢

### 报告模板与字段映射

| 报告区块 | 数据来源 |
|----------|----------|
| **审查总结** — 风险等级 | `result.summary` 中的风险等级 |
| **审查总结** — 总结 | `result.summary.summary`（或接口实际等价字段） |
| **风险批注明细** 表格 — 序号 | 按 `result.annotations` 数组下标或接口序号 |
| **风险批注明细** 表格 — 风险等级 | `annotations[].风险等级`，用上表图标展示 |
| **风险批注明细** 表格 — 原文摘要 | `annotations[].originalText` |
| **风险批注明细** 表格 — 审查意见 | `annotations[].commentTexts.remark` |
| **文档下载** — 清洁版 | `result.clearWordPath` |
| **文档下载** — 批注版 | `result.wordPath` |

说明：若实际接口字段名与上表略有差异（如 `remark` 在 `commentTexts` 数组首项），以对应合同的 `*_review_result.json` 实际结构为准，按上述语义对应到报告即可。
