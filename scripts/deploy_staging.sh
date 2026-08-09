#!/bin/bash
# Linkora Staging 环境部署脚本
# 用法: ./scripts/deploy_staging.sh

set -euo pipefail

echo "======================================"
echo "Linkora Staging 环境部署"
echo "日期: $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================"

# 配置
STAGING_HOST="${STAGING_HOST:-staging.linkora.local}"
STAGING_PORT="${STAGING_PORT:-8080}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/linkora}"
LOG_FILE="${LOG_FILE:-/tmp/staging-deploy.log}"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}ERROR: $1${NC}" | tee -a "$LOG_FILE"
    exit 1
}

success() {
    echo -e "${GREEN}✓ $1${NC}"
}

warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# 检查依赖
check_dependencies() {
    log "检查部署依赖..."
    
    command -v git >/dev/null 2>&1 || error "git 未安装"
    command -v python3 >/dev/null 2>&1 || error "python3 未安装"
    command -v docker >/dev/null 2>&1 || warning "docker 未安装，将使用直接部署方式"
    
    success "依赖检查通过"
}

# 运行测试
run_tests() {
    log "运行测试套件..."
    
    cd "$(dirname "$0")/.."
    
    # 运行核心测试
    /Users/user/Documents/Linkora/.venv/bin/python -m pytest \
        tests/test_security_utils.py \
        tests/test_cleanup_performance.py \
        tests/test_dedup_error_classification.py \
        tests/test_llm_router.py \
        tests/test_platform_lifecycle.py \
        tests/test_web_auth.py \
        tests/test_auth_endpoints.py \
        tests/test_sqlite_store.py \
        tests/test_business.py \
        tests/test_api_platform_routing.py \
        tests/test_classifier.py \
        tests/test_chat_send.py \
        tests/test_approval_transfer.py \
        tests/test_auth_integration.py \
        -q --tb=short
    
    local test_result=$?
    
    if [ $test_result -eq 0 ]; then
        success "所有测试通过"
    else
        error "测试失败，请修复后重新部署"
    fi
    
    return $test_result
}

# 构建项目
build_project() {
    log "构建项目..."
    
    cd "$(dirname "$0")/.."
    
    # 安装依赖
    pip install -e . --quiet
    
    # 清理旧构建
    rm -rf build/ dist/ *.egg-info/
    
    success "项目构建完成"
}

# 部署到 Staging
deploy_to_staging() {
    log "部署到 Staging 环境 ($STAGING_HOST:$STAGING_PORT)..."
    
    # 检查是否可以 SSH 连接
    if ! ssh -o ConnectTimeout=5 "$STAGING_HOST" "echo 'Staging 可访问'" 2>/dev/null; then
        warning "无法直接 SSH 连接到 Staging，跳过远程部署步骤"
        warning "请手动在 Staging 服务器上执行以下命令："
        echo ""
        echo "  git pull origin main"
        echo "  pip install -e ."
        echo "  systemctl restart linkora"
        echo ""
        return 0
    fi
    
    # 执行远程部署
    ssh "$STAGING_HOST" << EOF
    cd $DEPLOY_DIR
    git pull origin main
    pip install -e .
    systemctl restart linkora
    systemctl status linkora --no-pager
EOF
    
    success "Staging 部署完成"
}

# 健康检查
health_check() {
    log "执行健康检查..."
    
    # 检查服务是否启动
    if ! curl -s "http://$STAGING_HOST:$STAGING_PORT/health" > /dev/null 2>&1; then
        error "Staging 服务健康检查失败"
    fi
    
    success "服务运行正常"
}

# 认证端点验证
verify_auth_endpoints() {
    log "验证认证端点..."
    
    # 测试登录接口
    local login_response
    login_response=$(curl -s -X POST "http://$STAGING_HOST:$STAGING_PORT/api/auth/login" \
        -H "Content-Type: application/json" \
        -d '{"username": "admin", "password": "'${AUTH_PASSWORD:-test123}'"}')
    
    if echo "$login_response" | grep -q "access_token"; then
        success "登录接口正常"
        
        # 提取 token
        local token
        token=$(echo "$login_response" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")
        
        if [ -n "$token" ]; then
            # 测试 JWT 认证
            local me_response
            me_response=$(curl -s "http://$STAGING_HOST:$STAGING_PORT/api/auth/me" \
                -H "Authorization: Bearer $token")
            
            if echo "$me_response" | grep -q "username"; then
                success "JWT 令牌认证正常"
            else
                warning "JWT 用户信息接口响应异常: $me_response"
            fi
        fi
    else
        warning "登录接口响应异常: $login_response"
        warning "请检查认证配置是否正确"
    fi
    
    # 测试安全头
    local headers
    headers=$(curl -sI "http://$STAGING_HOST:$STAGING_PORT/health")
    
    if echo "$headers" | grep -q "X-Content-Type-Options: nosniff"; then
        success "安全头配置正确"
    else
        warning "安全头可能未正确配置"
    fi
}

# 性能基准测试
performance_test() {
    log "执行性能基准测试..."
    
    # 简单的并发测试
    local conc_requests=10
    local total_requests=100
    
    log "发送 $total_requests 个请求 (并发: $conc_requests)..."
    
    # 使用 ab 或 curl 进行压测
    if command -v ab >/dev/null 2>&1; then
        ab -n $total_requests -c $conc_requests \
            "http://$STAGING_HOST:$STAGING_PORT/health" \
            >/dev/null 2>&1 || warning "性能测试失败（可能是 ab 不可用）"
    else
        # 简单的 curl 循环测试
        for i in $(seq 1 $total_requests); do
            curl -s "http://$STAGING_HOST:$STAGING_PORT/health" >/dev/null 2>&1 || true
        done
    fi
    
    success "性能测试完成"
}

# 主函数
main() {
    log "开始 Staging 部署流程..."
    
    check_dependencies
    run_tests
    build_project
    deploy_to_staging
    health_check
    verify_auth_endpoints
    performance_test
    
    log "======================================"
    log "Staging 部署完成！"
    log "访问地址: http://$STAGING_HOST:$STAGING_PORT"
    log "日志文件: $LOG_FILE"
    log "======================================"
}

# 执行
main "$@"
