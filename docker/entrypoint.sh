#!/bin/bash
set -euo pipefail

# ===== 非 root 身份切换（Docker 官方模式）=====
# 以 root 启动时，用 gosu 切换到 app(uid 1000) 重跑本脚本，
# 使 dws 登录与 python 主进程都在非 root 身份下运行，降低被攻破时的权限面。
# 注意：dws 凭证目录由 DWS_CONFIG_DIR 显式指定（默认 ~/.dws），与运行用户无关，
# 但必须以 app 身份写入 /home/app/.dws 才能被同身份的 python 适配器读到。
if [ "$(id -u)" = "0" ]; then
    export DWS_CONFIG_DIR="${DWS_CONFIG_DIR:-/home/app/.dws}"
    exec gosu app "$0" "$@"
fi

# 非 root 身份下，确保凭证目录存在且归属当前用户
echo "运行身份: $(id -u):$(id -g) ($(whoami 2>/dev/null || echo unknown))"
mkdir -p "${DWS_CONFIG_DIR:-/home/app/.dws}"

# 兼容旧部署：若曾有以 root 写入的 /root/.dingtalk 凭证，提示迁移（不阻断）
if [ -d /root/.dingtalk ] && [ -z "$(ls -A "${DWS_CONFIG_DIR:-/home/app/.dws}" 2>/dev/null)" ]; then
    echo "[提示] 检测到旧 /root/.dingtalk 凭证，可手动迁移至 ${DWS_CONFIG_DIR}"
fi

echo "=========================================="
echo "  灵桥 (Linkora) - 容器启动脚本"
echo "=========================================="
echo "时区: ${TZ:-Asia/Shanghai}"
echo "工作目录: $(pwd)"
echo "DWS 凭证目录: ${DWS_CONFIG_DIR:-/home/app/.dws}"
echo ""

# ===== 检查 dws 是否可用 =====
if ! command -v dws >/dev/null 2>&1; then
    echo "[错误] 未找到 dws 命令行工具"
    echo "请确保 dws 已安装，或通过 volume 挂载 dws 二进制到 /usr/local/bin/dws"
    echo ""
    echo "可选方案："
    echo "  1. 在 Dockerfile 中添加 dws 安装步骤"
    echo "  2. 使用 docker run -v /path/to/dws:/usr/local/bin/dws ..."
    echo "  3. 设置 DWS_CLI_PATH 环境变量指向 dws 路径"
    exit 1
fi
echo "[OK] dws 已就绪: $(dws --version 2>&1 || echo 'unknown')"

# ===== 环境变量转配置 =====
# 如果提供了 DWS_PROFILE，确保使用它
if [ -n "$DWS_PROFILE" ]; then
    echo "[INFO] 使用 DWS profile: $DWS_PROFILE"
    export DWS_PROFILE
fi

# ===== 检查并执行首次认证 =====
DWS_AUTH_STATUS=$(dws auth status -f json 2>/dev/null || echo '{}')
IS_AUTHENTICATED=$(echo "$DWS_AUTH_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('authenticated', d.get('result',{}).get('authenticated', False)))" 2>/dev/null || echo "False")

if [ "$IS_AUTHENTICATED" = "True" ] || [ "$IS_AUTHENTICATED" = "true" ]; then
    echo "[OK] DWS 已认证"
else
    echo ""
    echo "=========================================="
    echo "  需要进行 DWS 认证"
    echo "=========================================="
    echo ""

    if [ "$DWS_HEADLESS_AUTH" = "1" ] || [ "$DWS_HEADLESS_AUTH" = "true" ]; then
        echo "[模式] 无头设备码认证 (Device Code Flow)"
        echo ""
        echo "请在浏览器中打开以下链接并完成授权："
        echo ""
        # 使用 device flow 模式
        dws auth login --device -y 2>&1 || {
            echo "[错误] 设备码认证失败"
            exit 1
        }
    else
        echo "[模式] 交互式登录 (需要浏览器)"
        echo ""
        echo "如果在无头环境运行，请设置环境变量 DWS_HEADLESS_AUTH=1"
        echo "使用设备码方式进行认证"
        echo ""
        dws auth login -y 2>&1 || {
            echo "[错误] 登录失败"
            exit 1
        }
    fi

    # 验证认证结果
    DWS_AUTH_STATUS=$(dws auth status -f json 2>/dev/null || echo '{}')
    IS_AUTHENTICATED=$(echo "$DWS_AUTH_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('authenticated', d.get('result',{}).get('authenticated', False)))" 2>/dev/null || echo "False")

    if [ "$IS_AUTHENTICATED" = "True" ] || [ "$IS_AUTHENTICATED" = "true" ]; then
        echo "[OK] DWS 认证成功"
    else
        echo "[警告] 认证状态检测失败，继续尝试启动..."
    fi
fi

# ===== 准备数据目录 =====
mkdir -p /app/data/backups
mkdir -p /app/logs

# ===== 配置文件处理 =====
if [ ! -f /app/config.yaml ]; then
    if [ -f /app/config.yaml.example ]; then
        echo "[INFO] 未找到 config.yaml，从 example 复制默认配置"
        cp /app/config.yaml.example /app/config.yaml
    else
        echo "[警告] 未找到 config.yaml 和 config.yaml.example，将使用代码默认配置"
    fi
fi

# ===== Web 界面控制 =====
WEB_ARGS=""
if [ "$ENABLE_WEB" = "1" ] || [ "$ENABLE_WEB" = "true" ]; then
    WEB_PORT=${WEB_PORT:-8080}
    WEB_ARGS="--web=${WEB_PORT}"
    echo "[INFO] Web 管理界面已启用，端口: ${WEB_PORT}"
else
    echo "[INFO] Web 管理界面已禁用 (无头模式)"
fi

# ===== 启动主程序 =====
echo ""
echo "=========================================="
echo "  启动 灵桥 (Linkora) 服务"
echo "=========================================="
echo ""

exec python3 main.py $WEB_ARGS "$@"
