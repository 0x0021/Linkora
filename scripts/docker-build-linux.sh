#!/usr/bin/env bash
# 在 Linux 容器内构建 Linkora 的 Linux 二进制，产物落到 ./dist/linkora
# 前置：Docker 已安装且 daemon 运行中（macOS 用 Docker Desktop）
# 文档：docs/BINARY_PACKAGING_PLAN.md §3.2
set -euo pipefail

IMAGE="linkora-builder:local"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "✗ 未找到 docker，请先安装 Docker Desktop 并启动 daemon" >&2
  exit 1
fi

echo "==> 构建镜像 $IMAGE（基于 python:3.13-slim）"
docker build -f Dockerfile.build -t "$IMAGE" .

echo "==> 启动一次性容器并取出 dist/"
CID="$(docker create "$IMAGE")"
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
rm -rf dist
mkdir -p dist
docker cp "$CID":/build/dist/. ./dist/

echo "==> 完成："
ls -lh dist/linkora
echo "    可放到任意 glibc x64 Linux 运行：./dist/linkora --mode web --web 8080"
