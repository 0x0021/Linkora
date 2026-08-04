# 灵桥 (Linkora) Docker 部署指南

本目录包含将 灵桥 (Linkora) 部署到 Linux 容器的所有配置文件和脚本。

## 目录结构

```
.
├── Dockerfile              # 镜像构建文件
├── docker-compose.yml      # 容器编排配置
├── .dockerignore           # 构建忽略文件
├── docker/
│   ├── entrypoint.sh       # 容器入口脚本
│   ├── build.sh            # 构建脚本
│   └── auth-login.sh       # 首次认证登录脚本
└── data/                   # 数据目录（运行时自动创建）
    ├── linkora.db      # SQLite 数据库
    └── backups/            # 数据库备份
```

## 快速开始

### 1. 构建镜像

```bash
# 使用脚本构建
bash docker/build.sh

# 或手动构建
docker build -t linkora:latest .
```

### 2. 首次认证（重要）

在无头（headless）Linux 环境中，使用设备码方式完成钉钉认证：

```bash
bash docker/auth-login.sh
```

脚本会输出一个验证链接和设备码，在浏览器中打开链接并完成授权即可。

认证信息会保存在 Docker volume `linkora_dws-config` 中，后续启动无需重复认证。

### 3. 启动服务

```bash
# 复制示例配置
cp config.yaml.example config.yaml
# 修改配置（至少配置 LLM API Key）
vim config.yaml

# 启动服务
docker compose up -d

# 查看日志
docker compose logs -f
```

## 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `TZ` | `Asia/Shanghai` | 时区 |
| `DWS_PROFILE` | 空 | dws 配置 profile 名称 |
| `DWS_HEADLESS_AUTH` | `1` | 无头认证模式（设备码方式） |
| `ENABLE_WEB` | `0` | 是否启用 Web 管理界面 |
| `WEB_PORT` | `8080` | Web 界面端口 |
| `LLM_API_KEY` | 空 | LLM API Key（覆盖配置文件） |
| `LLM_BASE_URL` | 空 | LLM Base URL |
| `LLM_MODEL` | 空 | LLM 模型名称 |
| `HF_HUB_OFFLINE` | `1` | HuggingFace 离线模式 |

## Volumes

| 挂载点 | 说明 |
|--------|------|
| `/app/data` | 数据库文件和备份 |
| `/app/logs` | 日志文件 |
| `/home/app/.dws` | dws 配置和登录凭证（宿主绑定到 `./dingtalk`） |
| `/app/config.yaml` | 配置文件（只读挂载） |

## dws CLI 安装说明

Dockerfile 中尝试自动安装 dws CLI。如果自动安装失败，可以：

### 方案一：手动挂载 dws 二进制

```bash
# 将本地 dws 命令挂载到容器中
docker run -v /usr/local/bin/dws:/usr/local/bin/dws ...
```

或在 docker-compose.yml 中添加：
```yaml
volumes:
  - /usr/local/bin/dws:/usr/local/bin/dws:ro
```

### 方案二：修改 Dockerfile

根据你的 dws 获取方式修改 Dockerfile 中的安装步骤。

## 常用命令

```bash
# 启动服务
docker compose up -d

# 停止服务
docker compose down

# 重启服务
docker compose restart

# 查看日志
docker compose logs -f
docker compose logs -f --tail 100

# 进入容器
docker compose exec linkora bash

# 检查 dws 认证状态
docker compose exec linkora dws auth status

# 重新认证
docker compose stop
bash docker/auth-login.sh
docker compose up -d
```

## 注意事项

1. **数据持久化**：确保 `./data`、`./logs` 目录和 `dws-config` volume 正确挂载，否则重启会丢失数据和登录状态。

2. **配置文件**：配置文件以只读方式挂载到容器中，修改本地 `config.yaml` 后需要重启容器生效。

3. **端口冲突**：如果启用 Web 界面，确保 8080 端口未被占用，或修改 `WEB_PORT` 环境变量。

4. **Token 过期**：钉钉登录 Token 会定期过期，`auth_monitor` 会自动检测并尝试刷新。如果需要手动重新认证，运行 `docker/auth-login.sh`。

5. **Embedding 模型**：本地 Embedding 模型需要联网下载一次。如果无法联网，可使用 API 模式的 Embedding，或提前下载模型并挂载到 `~/.cache/huggingface`。

6. **资源需求**：
   - 最小：1 CPU, 512MB 内存（使用 API Embedding）
   - 推荐：2 CPU, 2GB 内存（使用本地 Embedding）
