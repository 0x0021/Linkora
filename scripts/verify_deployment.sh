#!/bin/bash
# Linkora 部署验证脚本
# 用法: ./scripts/verify_deployment.sh [staging|production]

set -euo pipefail

ENV="${1:-staging}"
HOST="${ENV}_HOST"
PORT="${!ENV:+${STAGING_PORT:-8080}}"
BASE_URL="http://${!HOST:-localhost}:${PORT}"

echo "======================================"
echo "Linkora ${ENV^^} 环境验证"
echo "URL: $BASE_URL"
echo "日期: $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================"

PASS=0
FAIL=0

check() {
    local name="$1"
    local cmd="$2"
    echo -n "测试 $name... "
    if eval "$cmd" >/dev/null 2>&1; then
        echo "✓ PASS"
        ((PASS++))
    else
        echo "✗ FAIL"
        ((FAIL++))
    fi
}

check_json() {
    local name="$1"
    local url="$2"
    local expected="$3"
    echo -n "测试 $name... "
    if curl -sf "$url" | grep -q "$expected"; then
        echo "✓ PASS"
        ((PASS++))
    else
        echo "✗ FAIL"
        ((FAIL++))
    fi
}

echo ""
echo "=== 1. 基础健康检查 ==="
check "服务可达" "curl -sf $BASE_URL/health"
check "登录端点可用" "curl -sf -X POST $BASE_URL/api/auth/login -H 'Content-Type: application/json' -d '{\"username\":\"test\",\"password\":\"test\"}'"

echo ""
echo "=== 2. 认证功能验证 ==="
# 测试无认证访问被拒绝
check "未认证请求被拒绝" "! curl -sf $BASE_URL/api/platforms"

echo ""
echo "=== 3. 安全头检查 ==="
check "X-Content-Type-Options" "curl -sI $BASE_URL/health | grep -i 'x-content-type-options: nosniff'"
check "X-Frame-Options" "curl -sI $BASE_URL/health | grep -i 'x-frame-options: deny'"
check "Referrer-Policy" "curl -sI $BASE_URL/health | grep -i 'referrer-policy: no-referrer'"

echo ""
echo "=== 4. API 端点检查 ==="
check "平台列表端点" "curl -sf $BASE_URL/api/platforms"
check "系统路径端点" "curl -sf $BASE_URL/api/system/paths"
check "健康检查端点" "curl -sf $BASE_URL/api/platforms/health"

echo ""
echo "=== 5. 前端资源检查 ==="
check "静态资源目录" "curl -sf $BASE_URL/static/"
check "主页面" "curl -sf $BASE_URL/ | grep -i linkora"

echo ""
echo "======================================"
echo "验证结果: $PASS 通过, $FAIL 失败"
echo "======================================"

if [ $FAIL -eq 0 ]; then
    echo "✓ 所有验证通过！"
    exit 0
else
    echo "✗ 存在失败的验证项，请检查配置"
    exit 1
fi
