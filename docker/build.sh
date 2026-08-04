#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

IMAGE_NAME="${IMAGE_NAME:-linkora}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKERFILE="${DOCKERFILE:-${PROJECT_DIR}/Dockerfile}"

echo "=========================================="
echo "  构建 灵桥 (Linkora) Docker 镜像"
echo "=========================================="
echo "镜像名: ${IMAGE_NAME}:${IMAGE_TAG}"
echo "Dockerfile: ${DOCKERFILE}"
echo "上下文: ${PROJECT_DIR}"
echo ""

cd "$PROJECT_DIR"

# 检查 Dockerfile 是否存在
if [ ! -f "$DOCKERFILE" ]; then
    echo "[错误] 未找到 Dockerfile: $DOCKERFILE"
    exit 1
fi

# 检查 config.yaml 是否存在
if [ -f "config.yaml" ]; then
    echo "[提示] 检测到 config.yaml，将作为只读挂载到容器中"
else
    echo "[提示] 未找到 config.yaml，首次启动将从 example 复制默认配置"
fi

echo ""
echo "开始构建镜像..."
echo ""

docker build \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    -f "$DOCKERFILE" \
    --build-arg TZ=Asia/Shanghai \
    "$PROJECT_DIR"

echo ""
echo "=========================================="
echo "  构建完成"
echo "=========================================="
echo ""
echo "镜像: ${IMAGE_NAME}:${IMAGE_TAG}"
echo ""
echo "快速启动命令："
cat <<EOF
  # 1. 首次启动（进行 dws 认证）
  docker run -it --rm \\
    -v "\$(pwd)/data:/app/data" \\
    -v "\$(pwd)/logs:/app/logs" \\
    -v "\$(pwd)/config.yaml:/app/config.yaml:ro" \\
    -e DWS_HEADLESS_AUTH=1 \\
    -e ENABLE_WEB=0 \\
    ${IMAGE_NAME}:${IMAGE_TAG}

  # 2. 后台运行
  docker compose up -d
EOF
echo ""
