# 常见问题

## 启动报 `dws: command not found`

确保 `dws` 在 PATH 中，或在 `config.yaml` 设置 `dws.cli_path` 为绝对路径。

## 消息发不出去

检查：
1. DWS 是否登录：`dws auth status`
2. 工具速率限制：`send_message` 默认 128 次/小时，可在 `tools.rate_limit` 调整
3. 发送对象是否在黑名单：`rules.blacklist`
4. 组织是否开启了 CLI 数据访问权限：联系组织管理员

## 向量模型加载失败

确保 `models/` 下有 BGE 模型文件（首次启动会尝试从 HuggingFace 下载）。若无网络，可手动下载后放入。

## 端口被占用

修改 `config.yaml` 中 `web.port` 即可。

## 想禁用某些工具

在 `config.yaml` 中从 `tools.available` 列表里删除对应名称即可（LLM 不会看到这些工具）。

## AI 不回复

检查：
1. AI 回复冷却时间：`poller.reply_cooldown_seconds`（默认 60）
2. 是否命中了黑名单或白名单规则
3. 用户是否已经回复了该会话（AI 会跳过已回复的会话）
4. 消息是否是"纯礼貌结束语"（如"谢谢"、"好的"），系统会自动跳过

## 部门架构加载失败

如果提示"该组织尚未开启 CLI 数据访问权限"，需要联系组织管理员在钉钉开放平台开启 CLI 数据访问权限。开启后刷新页面即可。

## 大量 TOKEN_VERIFIED_FAILED 错误 / 反复弹出验证窗口

这是**组织级别的权限/认证问题**，最常见于**多组织**场景：你当前登录的 DWS profile 属于组织 A，但 `dws` 拉到的某些会话属于组织 B，而你对组织 B 没有 CLI 数据访问权限。

**为什么 dws 会弹窗**：`dws` CLI 在收到 `TOKEN_VERIFIED_FAILED` 或 token 过期时会自动弹出 OAuth 浏览器窗口要求重新授权，这是 dws 自身行为。

**系统的防御措施**：

1. **本地文件优先**：认证状态检测优先读取 `~/.dws/profiles.json`，不调用 dws 命令，减少弹窗触发
2. **提前续期**：启动时和每 5 分钟检查认证状态，token 即将过期时（提前 2 小时）自动触发静默登录
3. **设备流静默登录**：使用 `--device --no-browser` 参数，不弹出浏览器，在终端显示 userCode 和短链接供用户在其他设备上完成授权
4. **内存级跳过名单**：检测到跨组织权限错误的会话 ID 存入内存，后续轮询直接跳过

排查步骤：
1. 检查 dws 是否登录：`dws auth status`
2. 若已登录仍报错，在「设置 → 轮询高级 → 目标组织」中**指定你实际要服务的组织**（单选 corpId）
3. 若确为权限缺失，联系对应组织管理员在钉钉开放平台开启 CLI 数据访问权限

> 提示：dws 的弹窗是其自身行为，本系统无法阻止。建议在目标组织明确后，使用 `python scripts/prelogin_multiple_orgs.py` 预登录多个组织并持久化 token。

## 机器人是不是没在收消息？

若日志长时间安静，可用「list-all 主通道空轮探针」确认：当 `list-all` 连续 `poller.list_all_empty_alert_rounds`（默认 6）轮都没拉到任何新消息时，会打印 `[收信探针] list-all 主通道已连续 N 轮未拉到任何新消息` 告警。这**通常是正常的**（确实没人发消息）；若持续为空且你确定有人发消息，再检查账号登录 / 目标组织 CLI 权限。一旦某轮拉到消息，计数立即归零并提示「恢复收信」。
