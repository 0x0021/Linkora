# 权限与安全说明

本文汇总 Linkora 的权限与安全保障。漏洞上报请见仓库根目录 `SECURITY.md`（本文不涉及漏洞上报流程）。

## 1. Web 管理台鉴权

管理台（`web/api.py`，默认端口 8080）采用 **HTTP Basic Auth + JWT Bearer** 双模式。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `web.auth_enabled` | `true` | 是否开启全局认证。**fail-closed**：`auth_enabled=true` 且 `auth_password` 为空时拒绝启动**而非放行**。 |
| `web.auth_username` | `admin` | 登录用户名 |
| `web.auth_password` | （必填） | 登录密码，留空且开启认证会启动报错——务必修改 |
| `web.host` | `127.0.0.1` | 监听地址；安全默认仅本机回环 |
| `web.jwt_secret` | `''`（空） | JWT 签名密钥；**生产务必设置固定高熵值**，留空则每次重启生成临时随机密钥（旧令牌失效） |

### 登录限流（防爆破）

登录失败计于内存：`_AUTH_MAX_FAILS = 5`（窗口 `_AUTH_FAIL_WINDOW = 300s`），超限封锁 `_AUTH_BLOCK_SECONDS = 300s`；维度为 IP 为主、账号（`IP|username`）为辅，防止固定 IP 多账号轮询。

### 敏感端点纵深防御

即便 `web.auth_enabled = false`（信任的 LAN / 反代场景），下列端点**仍强制要求凭据**：
- 所有非 GET 写操作（`POST/PUT/PATCH/DELETE`）；
- 显式敏感只读：`/api/config/export`、`/api/logs`。

### 白名单路径（免认证）

`/`、`/health`、`/api/platforms`、`/api/auth/login`、`/api/auth/me`，以及前缀 `/static/`、`/api/image/`、`/api/skill-icons/`（后两者供前端 `<img>` 直链免 Basic Auth，内部已有签名 token / 二次校验）。

## 2. 外联拦截

`tools.block_outbound_to_third_party`（默认 `true`）**硬拦截 AI 主动联系第三方**，防止 AI 在未经确认时对外发送消息。属安全默认，关闭需显式配置。

## 3. 多平台数据隔离

每个平台独立 SQLite 数据库（`./data/<id>-ai.db`）、独立轮询器、独立 CLI 适配器；`PlatformContext` + 按平台的 contextvar（`set_current_platform` / `get_current_platform`）在线程内路由到正确平台库，数据不串台。存储层对 PII 做脱敏，FAISS 向量索引 / BM25 倒排 / 重排序均按库隔离。

## 4. 传输与响应头

`web` 响应注入安全头（本地内网管理工具，非对公网服务）：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、受限 `Content-Security-Policy` 与 `Permissions-Policy`。

## 5. 部署边界建议

- 绑定 `0.0.0.0` 且未开启认证时，管理台可能**公网裸奔**——务必经反向代理并开启认证（`web-api` 启动会打印告警但不阻断）。
- 配置文件自动备份（`data/config-backups/`，保留最近 30 份）**含明文密钥**，已被 `.gitignore` 排除，切勿入库。
- 生产建议由 Dockerfile / 部署脚本预装 SkillHub CLI 并保持 `skillhub.auto_install = false`（默认关，避免运行时自动拉取执行安装脚本）。
