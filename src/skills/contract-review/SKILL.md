---
name: contract-review
description: 智能审查合同，当用户上传合同文件要求合同审查、检查合同风险、合同审核、解析合同主体、审查条款、评估风险等级、生成审查报告时使用。不要在非合同文件或仅咨询法条等非审查场景时触发。
metadata:
  label: 智能合同审查
intent_keywords:
- 合同审查
- 合同审核
- 审查合同
- 检查合同风险
- 合同风险检查
- 合同评估
- 审查报告
- 合同分析
- 上传合同审查
- 查权益
- 取消审查
- ai速审
- 人工快审
- 专人专审
- 合同体检
- 合同检查
- 风险审查
- 条款审查
- 合同解析
- 审查结果
weight: 0.4
system_prompt: '# 技能指令（AI 生成建议）


  ## 匹配领域

  （无）


  ## 触发关键词

  合同审查, 合同审核, 审查合同, 检查合同风险, 合同风险检查, 合同评估, 审查报告, 合同分析, 上传合同审查, 查权益, 取消审查, ai速审, 人工快审,
  专人专审, 合同体检, 合同检查, 风险审查, 条款审查, 合同解析, 审查结果


  ## 说明

  以上内容由 AI 根据 SKILL.md 分析生成。请根据实际需求修改后再确认覆盖。'
---

# 钉钉智能法务合同审查

## 严格禁止

- 禁止自动打开浏览器或访问网页；链接与入口仅以文字告知，由用户自行操作
- 禁止跳过 Step 0、Step 1；必须先获取用户信息并查询权益，权益全为 0 时引导留资并结束
- 禁止向用户展示 corpId、userId、taskId、spaceId、fileId 等内部参数
- 禁止编造 fileId、spaceId、taskId；必须从 get-self、upload、create_contract_review 等返回值中提取
- 禁止在未查权益或权益为 0 时执行上传与审查
- 禁止向用户展示权益英文 code（ai_contract_review 等）；仅展示中文类型与次数
- 禁止在人工快审/专人专审时轮询出报告；仅告知预计完成时间并引导至钉钉合同助手
- AI 速审完成后必须读取 `output/<文件名基础>_review_result.json` 再生成报告，不得跳过
- 禁止在技能目录直接执行脚本；必须先将本技能 `scripts/` 下的脚本复制到用户工作空间目录，并在该工作空间目录下执行
- 禁止在未配置依赖的情况下执行脚本；必须将脚本依赖库加入执行环境，**requests** 须已安装或已在环境变量（如 PYTHONPATH）中声明，例如 `pip install requests`

## 命令总览

| 脚本 | 用途 | 必填参数 |
|------|------|----------|
| `query_benefit.py` | 查询审查权益 | corp-id |
| `upload_file_to_dingpan.py` | 上传合同到钉盘 | file, corp-id, staff-id |
| `analysis_contract.py` | 合同解析 | corp-id, user-id, file-name, file-id, file-size, space-id, file-type |
| `create_contract_review.py` | 提交审查任务 | corp-id, user-id, review-type, review-position, review-result-type, file-name, file-id, file-size, space-id, file-type |
| `query_review_result.py` | 查询/轮询审查结果 | corp-id, review-type, task-id, file-name（出报告时必传） |
| `abolish_review_record.py` | 取消审查 | corp-id, review-type, task-id |

## 意图判断决策树

用户说「审查合同/检查风险/合同审核/上传合同审查」：走完整流程 Step 0→1→2→3→4→5→6；先 get-self → query_benefit，权益为 0 则结束。
用户说「查权益/还剩几次」：query_benefit.py（需 corpId，来自 get-self 或上下文）。
用户说「取消审查」：abolish_review_record.py（需 corpId、reviewType、taskId）。
关键区分：仅当用户选择 **AI 速审** 时在 Step 6 轮询并在此输出报告；**人工快审、专人专审** 不轮询，只告知预计时间并引导到钉钉合同助手查询。

## 核心工作流

以下命令均在「已复制 scripts 到用户工作空间并配置依赖」的目录下执行。

```bash
# 0. 必须! 获取用户 — 提取 corpId, userId
dws contact user get-self

# 1. 必须! 查权益 — 全为 0 则引导留资并结束
python scripts/query_benefit.py --corp-id <corpId>

# 2. 上传 — 提取 spaceId, fileId, fileName, fileType, fileSize
python scripts/upload_file_to_dingpan.py --file <path> --corp-id <corpId> --staff-id <userId>

# 3. 解析 — 提取 reviewType, reviewPosition, companyList, wordCount
python scripts/analysis_contract.py --corp-id <corpId> --user-id <userId> --file-name <fileName> --file-id <fileId> --file-size <fileSize> --space-id <spaceId> --file-type <fileType>

# 4. 必须! 用户确认立场、审查方式、结果类型后再提交
# 5. 提交 — 提取 taskId, planFinishTime
python scripts/create_contract_review.py --corp-id <corpId> --user-id <userId> --review-type <AI_REVIEW|HUMAN_RECHECK|HUMAN_REVIEW> --review-position "..." --review-result-type <CONTRACT_REVIEW|RISK_STATEMENT> --file-name <fileName> --file-id <fileId> --file-size <fileSize> --space-id <spaceId> --file-type <fileType>

# 6a. AI 速审：轮询至完成，必须传 --file-name，再读取 output/<文件名基础>_review_result.json 生成报告
python scripts/query_review_result.py --corp-id <corpId> --review-type AI_REVIEW --task-id <taskId> --file-name <fileName> --poll
# 6b. 人工快审/专人专审：不轮询，告知 planFinishTime（转 YYYY年MM月dd日 HH:mm）并引导至钉钉合同助手
```

## 上下文传递规则

| 操作 | 从返回中提取 | 用于 |
|------|-------------|------|
| get-self | corpId, userId | query_benefit, upload, analysis, create_review |
| query_benefit | benefitResponses[].code/restBenefit | Step 4 可选审查方式、权益为 0 则结束 |
| upload_file_to_dingpan | spaceId, fileId, fileName, fileType, fileSize | analysis_contract, create_contract_review；fileName 还用于 query_review_result 的 --file-name 与产出路径 |
| analysis_contract | reviewType, reviewPosition, companyList | Step 4 展示与用户确认；create_review 的 --review-position 等 |
| create_contract_review | taskId, planFinishTime | query_review_result（taskId）；人工/专人时 planFinishTime 告知用户 |
| query_review_result（--poll） | 写入 output/<文件名基础>_review_result.json | 读取后解析 result.summary/annotations/clearWordPath/wordPath 生成报告 |

## 数据格式（易错对照）

```bash
# [正确] --review-type 枚举
--review-type AI_REVIEW
--review-type HUMAN_RECHECK
--review-type HUMAN_REVIEW

# [错误] 小写或错误拼写会导致接口失败
--review-type ai_review
--review-type HUMAN_CHECK
```

```bash
# [正确] --file-name 与产出路径一致：取 Step 2 返回的 fileName，query_review_result 写入 output/<去掉扩展名的 fileName>_review_result.json
--file-name "合同.docx"   # 产出 output/合同_review_result.json

# [错误] 不传 --file-name 时默认 output/review_result.json，多合同场景无法区分
```

## 错误处理

1. 遇到错误 — 展示给用户，不自行猜测或替代方案
2. 1003 权益不足 — 调用 query_benefit.py 检查余量，提示用户通过权益增购申请获取
3. 1001 请求参数异常 — 检查 corpId、userId、fileId、spaceId 等是否从上游步骤提取

完整错误码见 [references/error-codes.md](./references/error-codes.md)。

## 详细参考（按需读取）

- [references/workflow.md](./references/workflow.md) — 完整 Step 0–6 与辅助操作、环境变量、标准产出
- [references/scripts-reference.md](./references/scripts-reference.md) — 各脚本参数与返回字段
- [references/error-codes.md](./references/error-codes.md) — 错误码与处理方式
- [references/report-template.md](./references/report-template.md) — AI 审查结果报告模板
