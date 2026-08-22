# 脚本参数与返回字段

## query_benefit.py

- **用途**：根据 corpId 查询审查权益。
- **必填**：`--corp-id <corpId>`
- **可选**：`--base-url`
- **返回**：`result.benefitResponses[]`，每项含 `code`、`usedBenefit`、`restBenefit`。权益 code：ai_contract_review / ai_human_contract_review / human_legal_affairs_text_service。

## upload_file_to_dingpan.py

- **用途**：将本地合同上传到钉盘。
- **必填**：`--file`、`--corp-id`、`--staff-id`
- **可选**：`--base-url`（或 UPLOAD_BASE_URL 环境变量）；`--corp-id` 可由 DINGTALK_CORP_ID 提供
- **返回**：LawFileDTO — spaceId, fileId, fileName, fileType, fileSize（后续步骤全部依赖）

## analysis_contract.py

- **用途**：合同解析，提取主体与 AI 推荐审查方式。
- **必填**：`--corp-id`、`--user-id`、`--file-name`、`--file-id`、`--file-size`、`--space-id`、`--file-type`
- **可选**：`--base-url`
- **返回**：reviewType, reviewPosition, companyList, wordCount

## create_contract_review.py

- **用途**：提交审查任务。
- **必填**：`--corp-id`、`--user-id`、`--review-type`、`--review-position`、`--review-result-type`、`--file-name`、`--file-id`、`--file-size`、`--space-id`、`--file-type`
- **可选**：`--company-list`、`--custom-review-rules`、`--base-url`
- **返回**：taskId, planFinishTime

## query_review_result.py

- **用途**：查询审查结果；加 `--poll` 时轮询至非 REVIEWING（每 30 秒，最长 10 分钟）。
- **必填**：`--corp-id`、`--review-type`、`--task-id`、`--file-name`（AI 速审出报告时必传，用于写出 `output/<文件名基础>_review_result.json`）
- **可选**：`--poll`、`--base-url`、`--output`
- **产出**：完整 JSON 写入 output 路径；报告相关在 result 下：summary, annotations, clearWordPath, wordPath

## abolish_review_record.py

- **用途**：取消审查。
- **必填**：`--corp-id`、`--review-type`、`--task-id`
- **可选**：`--base-url`
