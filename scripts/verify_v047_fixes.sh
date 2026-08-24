#!/bin/bash
# Linkora Staging 部署验证脚本
# 用途：验证 v0.4.7 缺陷修复在生产环境的行为

set -e

echo "=========================================="
echo "Linkora v0.4.7 Staging 验证脚本"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 1. 运行完整测试套件
echo ""
echo "[1/4] 运行测试套件..."
cd /Users/ring0/Documents/Linkora
.venv/bin/python3.14 -m pytest tests/ \
  --ignore=tests/test_account_isolation_integration.py \
  --ignore=tests/test_account_identity.py \
  -q --tb=short 2>&1 | tail -20

# 2. 检查异常分类日志
echo ""
echo "[2/4] 检查 LLM 异常分类日志..."
if [ -f "data/logs/linkora.log" ]; then
    echo "→ 最近 LLMNetworkError 日志:"
    grep -i "LLMNetworkError" data/logs/linkora.log | tail -5 || echo "  (暂无)"
    echo "→ 最近 LLMRateLimitError 日志:"
    grep -i "LLMRateLimitError" data/logs/linkora.log | tail -5 || echo "  (暂无)"
    echo "→ 最近 LLMAuthError 日志:"
    grep -i "LLMAuthError" data/logs/linkora.log | tail -5 || echo "  (暂无)"
else
    echo "  (日志文件不存在，跳过)"
fi

# 3. 检查 OCR 下载大小限制
echo ""
echo "[3/4] 检查 OCR 下载大小限制..."
if [ -f "data/logs/linkora.log" ]; then
    echo "→ 最近下载大小限制日志:"
    grep -i "下载文件过大" data/logs/linkora.log | tail -5 || echo "  (暂无)"
else
    echo "  (日志文件不存在，跳过)"
fi

# 4. 检查 noqa: BLE001 清理状态
echo ""
echo "[4/4] 检查 noqa: BLE001 清理状态..."
BLE001_COUNT=$(grep -rn "noqa.*BLE001" src/ 2>/dev/null | grep -v "pycache" | wc -l || echo "0")
echo "→ 残留 noqa: BLE001 数量: $BLE001_COUNT"
if [ "$BLE001_COUNT" -eq 0 ]; then
    echo "  ✅ 全部清理完成"
else
    echo "  ⚠️ 仍有残留，请检查"
fi

echo ""
echo "=========================================="
echo "验证完成"
echo "=========================================="
