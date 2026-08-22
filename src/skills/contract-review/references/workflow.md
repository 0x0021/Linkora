# 合同审查完整流程 (Step 0–6 与辅助操作)

## 执行环境（必须满足后再执行脚本）

1. **脚本复制到用户工作空间**：须先将本技能 `contract-review/scripts/` 下的全部脚本复制到用户当前工作空间目录（例如用户项目根目录下的 `scripts/` 或当前工作目录），后续所有 `python scripts/xxx.py` 均在该工作空间目录下执行，不得直接引用技能安装路径执行。
2. **脚本依赖库**：须将脚本依赖库加入执行环境。**requests** 为必选依赖，须在环境中声明或安装，任选其一：
   - 安装到当前 Python 环境：`pip install requests`（或 `pip install -r requirements.txt` 若技能提供）；
   - 或将包含 requests 的路径加入 `PYTHONPATH` 环境变量，确保执行 `python scripts/xxx.py` 时能正确解析 `import requests`。

## 环境变量
**统一服务地址**：所有脚本的 `--base-url` 默认均为 `https://trip.dingtalk.com`

## 标准产出（审查结果）

- **文件路径**：`output/<上传文件名（去扩展名）>_review_result.json`（由 `query_review_result.py` 传入 `--file-name <fileName>` 生成）。未传 `--file-name` 时默认为 `output/review_result.json`；也可用 `--output` 指定完整路径。
- **用途**：脚本将查询审查结果接口的完整返回写入该 JSON；流程中**模型必须读取与当前合同对应的该文件**，解析 `result` 下的 `summary`、`annotations`、`clearWordPath`、`wordPath` 等，再生成《审查结果报告》并呈现给用户。

## Task Progress

- Step 0: 获取当前用户 — 通过 dws contact user get-self 获取 corpId、userId
- Step 1: 查询审查权益 — 根据 corpId 查询权益；若全部为 0 则引导留资并结束，否则进入合同审查流程
- Step 2: 上传文件 — 本地合同上传到钉盘，拿到钉盘文件信息
- Step 3: 合同解析 — 提取合同主体与 AI 推荐审查方式
- Step 4: 展示解析结果并选择审查方式 — 结合推荐与有权益的模式，让用户确认立场、审查方式、结果类型等
- Step 5: 提交审查 — 用户确认后发起审查任务
- Step 6: 审查结果 — AI 速审则轮询输出报告；人工/专人则告知预计完成时间并引导至钉钉合同助手查询

### Step 0: 获取当前用户详情

先通过 **dws contact user get-self** 获取当前用户详情，从返回中取得：
- `corpId` — 供后续查询权益、上传及所有 API 使用
- `userId`（即 staffId）— 供上传、解析、提交审查使用

**注意**：`corpId`、`userId` 仅供内部调用，**不得直接向用户显示**。若未接入钉钉通讯录 MCP 或调用失败，则从上下文或向用户确认获取后再继续 Step 1。

### Step 1: 查询审查权益

**必须先**根据 Step 0 的 corpId 查询审查权益：

```bash
python scripts/query_benefit.py --corp-id <corpId> [--base-url "https://trip.dingtalk.com"]
```

- **若所有 restBenefit 均为 0**：告知用户暂无审查权益，引导通过**[合同审查权益增购申请](https://alidocs.dingtalk.com/notable/share/form/v01AJdl65AWWGQ4vOke_06vjC1B_VCSkPNG)**提交申请，流程结束。
- **若至少有一个 restBenefit 不为 0**：继续 Step 2。

权益 code 对应：`ai_contract_review` → AI_REVIEW（AI 速审），`ai_human_contract_review` → HUMAN_RECHECK（人工快审），`human_legal_affairs_text_service` → HUMAN_REVIEW（专人专审）。仅处理以上三种权益；`human_legal_affairs_consult` 已下线，不得向用户展示。仅向用户展示中文权益类型和次数，不得展示英文 code。

### Step 2: 上传文件

使用 Step 0 的 corpId、userId：

```bash
python scripts/upload_file_to_dingpan.py \
  --file "/path/to/contract.docx" \
  --corp-id <corpId> \
  --staff-id <userId> \
  [--base-url "https://trip.dingtalk.com"]
```

`--corp-id` 可通过环境变量 `DINGTALK_CORP_ID` 设置。返回 LawFileDTO，后续依赖：spaceId, fileId, fileName, fileType, fileSize。**不得向用户显示 spaceId、fileId**；仅展示文件名、文件大小、文件类型。

### Step 3: 合同解析

```bash
python scripts/analysis_contract.py \
  --corp-id <corpId> --user-id <userId> \
  --file-name <fileName> --file-id <fileId> --file-size <fileSize> \
  --space-id <spaceId> --file-type <fileType> \
  [--base-url "https://trip.dingtalk.com"]
```

返回值：reviewType（AI 推荐审查方式）、reviewPosition、companyList、wordCount。AI 速审约 10 分钟，人工快审约半日，专人专审约 2 工作日，具体随文件大小变化。

### Step 4: 展示解析结果并选择审查方式

- 展示解析结果（立场、推荐审查方式、字数等）。
- **可选审查方式** = AI 推荐 ∩ Step 1 有权益的模式；若用户坚持选其他有权益方式也可允许。
- 必须让用户确认：审查立场、审查方式（AI 速审/人工快审/专人专审）、审查结果类型（直接修订/风险提示）、可选自定义审查要求。

审查方式说明：**AI 速审**—法律 AI，约 10 分钟；**人工快审**—AI+法务，约半日；**专人专审**—法务专家 1 对 1，约 2 工作日。确认后进入 Step 5。

### Step 5: 提交审查

```bash
python scripts/create_contract_review.py \
  --corp-id <corpId> --user-id <userId> \
  --review-type AI_REVIEW \
  --review-position "甲方：A 公司" \
  --review-result-type RISK_STATEMENT \
  --file-name <fileName> --file-id <fileId> --file-size <fileSize> \
  --space-id <spaceId> --file-type <fileType> \
  [--company-list "A 公司，B 公司"] \
  [--custom-review-rules "额外审查要求"] \
  [--base-url "https://trip.dingtalk.com"]
```

`--review-type`: AI_REVIEW | HUMAN_RECHECK | HUMAN_REVIEW。`--review-result-type`: CONTRACT_REVIEW | RISK_STATEMENT。返回 taskId、planFinishTime。**taskId 不得向用户显示**。

### Step 6: 审查结果

- **AI 速审**：使用 query_review_result.py 的 `--poll` 模式，直到状态非 REVIEWING；脚本写入 `output/<文件名基础>_review_result.json`，**必须读取该 JSON** 后根据 result.summary、annotations、clearWordPath、wordPath 生成《审查结果报告》并呈现。
- **人工快审/专人专审**：不轮询。将 planFinishTime 转为 YYYY 年 MM 月 dd 日 HH:mm，告知用户届时到**钉钉合同助手**查询。

状态流转：REVIEWING → COMPLETED | FAILED | ABOLISH

## 辅助操作

### 取消审查

```bash
python scripts/abolish_review_record.py \
  --corp-id <corpId> --review-type <reviewType> --task-id <taskId> \
  [--base-url "https://trip.dingtalk.com"]
```

### 查询权益余量

```bash
python scripts/query_benefit.py --corp-id <corpId> [--base-url "https://trip.dingtalk.com"]
```

权益类型仅展示中文：AI 速审、人工快审、专人专审。不得向用户展示英文 code。

## 注意事项

- 执行本技能**禁止自动打开浏览器**；链接/入口仅以文字告知，由用户自行操作。
- 脚本位于本技能 `scripts/` 下；以本技能目录为工作目录，执行前确保环境变量已设置。
- 仅当用户选择 **AI 速审** 时才在此轮询并输出报告；人工快审、专人专审不轮询，仅告知预计时间并引导至钉钉合同助手。
- 文档下载地址默认 6 小时过期，过期后重新调用 query_review_result.py 获取新地址。
