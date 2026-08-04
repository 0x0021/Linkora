#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=========================================="
echo "  灵桥 (Linkora) - 首次登录认证引导"
echo "=========================================="
echo ""

IMAGE_NAME="${IMAGE_NAME:-linkora}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# 检查镜像是否存在
if ! docker image inspect "${IMAGE_NAME}:${IMAGE_TAG}" >/dev/null 2>&1; then
    echo "[错误] 未找到镜像: ${IMAGE_NAME}:${IMAGE_TAG}"
    echo "请先运行: docker/build.sh"
    exit 1
fi

echo "[模式] 设备码登录 (Device Code Flow)"
echo ""
echo "说明："
echo "  1. 容器将输出一个设备码和验证链接"
echo "  2. 在浏览器中打开链接，输入设备码完成授权"
echo "  3. 授权成功后，登录凭证将保存在 Docker volume 中"
echo ""
echo "按 Ctrl+C 可随时退出"
echo ""

docker run -it --rm \
    -v "${PROJECT_DIR}/dingtalk:/home/app/.dws" \
    -v "${PROJECT_DIR}/data:/app/data" \
    -v "${PROJECT_DIR}/logs:/app/logs" \
    -e DWS_CONFIG_DIR=/home/app/.dws \
    -e DWS_HEADLESS_AUTH=1 \
    -e ENABLE_WEB=0 \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    python3 -c "
import subprocess, sys
result = subprocess.run(['dws', 'auth', 'login', '--device', '-y'], 
                       capture_output=False, text=True)
if result.returncode == 0:
    print()
    print('=' * 50)
    print('  认证成功！登录凭证已保存')
    print('  现在可以启动完整服务了')
    print('=' * 50)
else:
    print('认证失败，退出码:', result.returncode)
    sys.exit(result.returncode)
"

echo ""
echo "[完成] 认证流程结束"
echo ""
echo "下一步：启动完整服务"
echo "  docker compose up -d"
echo ""
echo "查看日志："
echo "  docker compose logs -f"
echo ""
