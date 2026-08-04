# 部署指南

## 本地运行

### 前置依赖

- Python 3.9+（推荐 3.13 / 3.14；当前开发环境 3.14）
- dws CLI（钉钉官方命令行工具，`dws auth login` 完成认证）
- macOS / Windows / Linux

### 安装与启动

```bash
# 克隆项目
git clone <repo-url> linkora
cd linkora

# 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 复制配置模板并修改
cp config.yaml.example config.yaml
# 必须修改: llm.api_key / llm.base_url

# 钉钉登录（首次）
dws auth login

# 前台运行（带 Web 管理后台）
python main.py --web                 # 默认端口 8080
```

### 常用参数

```bash
# 自定义端口
python main.py --web 9000

# 仅运行（不开 Web）
python main.py

# 测试规则匹配
python main.py --test-rule "在吗"
```

## macOS 后台服务（launchctl）

```bash
# 安装并启动
./scripts/install-mac.sh

# 查看状态
launchctl list | grep linkora

# 查看日志
tail -f logs/linkora.log

# 卸载
./scripts/uninstall-mac.sh
```

## Windows 后台服务（nssm）

需要预先安装 [nssm](https://nssm.cc/download)：

```powershell
# 安装并启动
.\scripts\install-win.ps1

# 查看状态
nssm status Linkora

# 卸载
.\scripts\uninstall-win.ps1
```

## Docker 部署

### 快速启动

```bash
docker compose up -d
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_WEB` | `0` | 是否启动 Web 管理后台（`1`=启用，端口 8080） |

### 数据持久化

Docker Compose 使用以下挂载持久化数据：

- `./data` — SQLite 数据库、备份
- `./logs` — 日志
- `./dingtalk` — dws 认证凭据（容器内 `/home/app/.dws`，非 root 用户 app 持有）
- `./config.yaml` — 配置文件（只读挂载）

> 注意：容器以非 root 用户 `app`(uid 1000) 运行。dws 凭证真实目录为
> `/home/app/.dws`（受 `DWS_CONFIG_DIR` 环境变量控制），与旧文档提及的
> `/root/.dingtalk` 无关；请勿再挂载 `/root/.dingtalk`。

### dws 认证（Docker）

容器内 dws 使用 Device Code Flow 登录：

```bash
docker compose exec app dws auth login
```

按照提示在浏览器中完成授权即可。凭据保存在 `./dingtalk` 目录（容器内 `/home/app/.dws`）中，重启不丢失。

### 多组织预登录（可选）

如果你需要同时服务多个钉钉组织，可以使用预登录脚本：

```bash
# 1. 在 config.yaml 中配置目标组织列表
# target_orgs:
#   - dingxxxxxxxxxxxx
#   - dingyyyyyyyyyyyy

# 2. 运行预登录脚本
python scripts/prelogin_multiple_orgs.py
```

脚本会逐个组织执行设备流登录，token 持久化到 `~/.dws/profiles.json`，后续轮询时自动使用对应组织的认证凭据。

### 构建镜像

```bash
./build.sh
# 或
docker build -t linkora:latest .
```
