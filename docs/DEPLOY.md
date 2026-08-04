# 灵桥 (Linkora) — 部署指南

## 各平台部署前置条件

### 钉钉

1. 在[钉钉开放平台](https://open.dingtalk.com/)创建企业内部应用
2. 获取 AppKey 和 AppSecret
3. 配置应用权限（消息接收、通讯录读取、文档读写等）
4. 安装 DWS CLI：`curl -fsSL https://dtalkapp.sjtu.edu.cn:443/dwscript/install.sh | bash`
5. 运行 `dws auth login` 完成首次认证（Docker 部署时由 `entrypoint.sh` 自动处理）

### 飞书

1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用
2. 获取 App ID 和 App Secret
3. 配置事件订阅（消息与群组）的回调地址
4. 安装 lark-cli 并完成认证

### 企业微信

1. 在[企业微信管理后台](https://work.weixin.qq.com/)创建自建应用
2. 获取 CorpID、AgentID 和 Secret
3. 配置接收消息的回调 URL
4. 安装 wecom-cli 并完成认证

---

## 部署方式

### 方式一：直接部署（macOS / Linux）

```bash
# 1. 克隆 & 安装
git clone <repo-url> dingtalk
cd dingtalk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env          # 编辑填入 LLM_API_KEY
cp config.yaml.example config.yaml  # 编辑至少 llm.* 和 platforms[0].adapter.cli_path

# 3. 启动
python main.py --web 8080
```

### 方式二：Docker 部署

```bash
# 1. 构建镜像
docker build -t linkora:latest .

# 2. 准备目录
mkdir -p ./data/backups ./logs ./dingtalk

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM_API_KEY 等

# 4. 启动
docker-compose up -d
```

---

### 进程分离部署（A1）

从本版本起支持将 Web 与后台 ingestion（轮询器 + 调度器）拆分为两个独立进程，实现「改 Web 代码只重启 web 进程、不打断消息处理」。三者共用同一 SQLite（WAL 模式，并发安全，已验证）。

#### 启动模式

| 模式 | 参数 | 职责 | PID 文件 |
|------|------|------|----------|
| 一体化（默认，向后兼容） | `--mode both`（或不传） | Web + 后台同进程 | `data/linkora.pid` |
| 仅 Web | `--mode web --web 8080` | 只启动 Web 管理平台 | `data/linkora.web.pid` |
| 仅后台 | `--mode worker` | 只跑轮询器 + 调度器 | `data/linkora.worker.pid` |

#### 推荐部署：两进程分离

```bash
# 一键拉起 web + worker（Ctrl+C / SIGTERM 优雅关闭两者）
python scripts/run_linkora.py --web-port 8080

# 只改 Web 时，重启 web 进程即可，worker（消息处理）不受影响：
python main.py --mode web --web 8080

# 只重启 worker（如改了 poller 逻辑），Web 无感：
python main.py --mode worker
```

> 注意：`--mode worker` 不绑定端口；`--mode web` 必须带 `--web <port>` 才启动 Web。
> 一体化模式 `--mode both` 仍使用 `data/linkora.pid`（与既有部署脚本、测试断言兼容）。

#### systemd / launchd 拆分建议

若用进程管理器托管，建议拆成 `linkora-web` 与 `linkora-worker` 两个 unit，分别
`ExecStart=.../main.py --mode web --web 8080` 与 `ExecStart=.../main.py --mode worker`，
各自独立 `Restart=on-failure`，互不影响。

---

#### Docker 卷挂载说明

| 容器路径 | 宿主机路径 | 用途 |
|---------|-----------|------|
| `/app/data` | `./data` | SQLite 数据库 + 备份 + 知识库文件 |
| `/app/logs` | `./logs` | 应用日志 |
| `/home/app/.dws` | `./dingtalk` | DWS CLI 登录凭证（钉钉平台） |
| `/app/config.yaml` | `./config.yaml` | 配置文件（只读挂载） |

#### Docker 环境变量

| 变量 | 默认值 | 说明 |
|------|-------|------|
| `TZ` | `Asia/Shanghai` | 时区 |
| `DWS_PROFILE` | 空 | DWS 多 profile 切换 |
| `DWS_HEADLESS_AUTH` | `1` | 无头设备码认证 |
| `DWS_CONFIG_DIR` | `/home/app/.dws` | DWS 凭证目录 |
| `ENABLE_WEB` | `1` | 是否启用 Web 管理台 |
| `WEB_PORT` | `8080` | Web 管理台端口 |
| `LLM_API_KEY` | 空 | 主 LLM API Key |
| `LLM_BASE_URL` | 空 | 主 LLM 服务地址 |
| `LLM_MODEL` | 空 | 主 LLM 模型名 |
| `HF_HUB_OFFLINE` | `1` | 禁止 HuggingFace 在线下载（模型应预装或挂载） |

---

## systemd 服务配置

创建 `/etc/systemd/system/linkora.service`：

```ini
[Unit]
Description=灵桥 (Linkora) AI 智能连接平台
After=network.target

[Service]
Type=simple
User=linkora
Group=linkora
WorkingDirectory=/opt/linkora
Environment="PATH=/opt/linkora/.venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="DWS_CONFIG_DIR=/home/linkora/.dws"
EnvironmentFile=/opt/linkora/.env
ExecStart=/opt/linkora/.venv/bin/python /opt/linkora/main.py --web 8080
ExecStop=/bin/kill -TERM $MAINPID
Restart=on-failure
RestartSec=10
StandardOutput=append:/opt/linkora/logs/stdout.log
StandardError=append:/opt/linkora/logs/stderr.log

# 安全加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/linkora/data /opt/linkora/logs /home/linkora/.dws
ReadOnlyPaths=/opt/linkora/config.yaml

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable linkora
sudo systemctl start linkora
sudo systemctl status linkora
```

---

## launchd 服务配置（macOS）

创建 `~/Library/LaunchAgents/com.linkora.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.linkora</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/linkora/.venv/bin/python</string>
        <string>/opt/linkora/main.py</string>
        <string>--web</string>
        <string>8080</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/opt/linkora</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/linkora/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>DWS_CONFIG_DIR</key>
        <string>/Users/user/.dws</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/opt/linkora/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/opt/linkora/logs/stderr.log</string>
</dict>
</plist>
```

加载并启动：

```bash
launchctl load ~/Library/LaunchAgents/com.linkora.plist
launchctl start com.linkora
```

---

## 环境变量说明

灵桥支持三层配置优先级（高→低）：

1. **Shell 环境变量**（启动前 export）
2. **`.env` 文件**（项目根目录）
3. **`config.yaml`**（文件配置）

### 核心环境变量

| 变量 | config.yaml 对应字段 | 说明 |
|------|---------------------|------|
| `LLM_API_KEY` | `llm.api_key` | 主 LLM 服务商 API Key |
| `LLM_BASE_URL` | `llm.base_url` | 主 LLM 服务地址 |
| `LLM_MODEL` | `llm.model` | 主 LLM 模型名 |
| `LLM_FALLBACK_API_KEY` | `llm.fallback_api_key` | 备用 LLM 服务商 API Key |
| `LLM_FALLBACK_BASE_URL` | `llm.fallback_base_url` | 备用 LLM 服务地址 |
| `LLM_FALLBACK_MODEL` | `llm.fallback_model` | 备用 LLM 模型名 |
| `EMBEDDING_API_KEY` | — | Embedding 独立密钥（留空则回退用 `LLM_API_KEY`） |
| `DWS_CONFIG_DIR` | — | DWS 凭证目录（默认 `~/.dws`） |
| `DWS_PROFILE` | — | DWS 多 profile 切换 |
| `DWS_HEADLESS_AUTH` | — | 无头设备码认证（Docker 默认 `1`） |
| `ENABLE_WEB` | — | 是否启用 Web（Docker entrypoint 读取，`1`/`0`） |
| `WEB_PORT` | `web.port` | Web 管理台端口 |
| `HF_HUB_OFFLINE` | — | 设为 `1` 禁止 HuggingFace 在线下载模型 |

---

## 日志路径与轮转配置

### 日志文件

| 路径 | 说明 |
|------|------|
| `logs/linkora.log` | 主应用日志（含所有模块） |
| `logs/stdout.log` | systemd/launchd 标准输出 |
| `logs/stderr.log` | systemd/launchd 标准错误 |

### 轮转配置

轮转由 `config.yaml` 的 `logging` 段控制：

```yaml
logging:
  level: INFO            # 日志级别：DEBUG / INFO / WARNING / ERROR
  file: ./logs/linkora.log
  max_size_mb: 50       # 单个日志文件最大大小（MB）
  max_backups: 5        # 保留的历史日志文件数量
```

内置 `RotatingFileHandler`：当日志文件达到 `max_size_mb` 时自动轮转，旧文件命名为 `linkora.log.1`、`linkora.log.2` ...，最多保留 `max_backups` 个历史文件。

### 日志内容

- 所有模块使用标准 Python `logging`，logger 名为模块路径（如 `src.poller`、`src.rule_engine`）
- 敏感信息（API Key、Token、密码）自动脱敏为 `***REDACTED***`
- LLM 调用日志带独立颜色标记（终端模式）便于区分
- 每条日志包含 `request_id` 字段用于全链路追踪

### Docker 日志

Docker 环境下日志同时输出到：
- 容器 stdout/stderr（`docker logs linkora` 查看）
- `logs/linkora.log`（volume 挂载持久化）

可在 `docker-compose.yml` 中配置日志驱动限制：

```yaml
services:
  linkora:
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
```

---

## 健康检查

Docker 部署内置健康检查：

- **Web 启用时**：每 5 分钟 `curl http://localhost:8080/health`（返回 200 即健康）
- **Web 禁用时**：每 5 分钟 `dws doctor --json` 检查 auth 状态

健康检查配置（`docker-compose.yml`）：

```yaml
healthcheck:
  interval: 300s
  timeout: 10s
  retries: 3
  start_period: 60s
```

---

## 数据库备份

灵桥内置自动备份机制：

- 启动时立即执行一次全量备份（`backup_on_start: true`）
- 按 `backup_interval_hours` 周期增量备份
- 备份文件存放于 `data/backups/`，保留最近 `backup_max_count` 份
- 备份为完整 SQLite 文件副本（非 SQL dump）

手动备份：

```bash
# 直接复制 SQLite 文件（WAL 模式下安全）
cp data/linkora.db data/backups/manual_backup_$(date +%Y%m%d).db
```

---

## 优雅关闭

灵桥通过 `SIGTERM` 信号触发优雅关闭：

1. 取消所有定时器（轮询、备份、摘要调度）
2. 等待进行中的 LLM 调用完成（最多 30 秒）
3. `join` 所有后台线程
4. 关闭 SQLite 连接（flush WAL）
5. 退出进程

```bash
# 优雅关闭（按启动模式选对应 PID 文件）
kill -TERM $(cat data/linkora.pid)        # --mode both
kill -TERM $(cat data/linkora.web.pid)    # --mode web
kill -TERM $(cat data/linkora.worker.pid) # --mode worker

# 一键启动器会同时转发信号给 web + worker 两进程：
#   Ctrl+C 或 kill -TERM <run_linkora 的 PID>

# Docker
docker stop linkora
```
