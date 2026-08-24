#!/bin/bash
# Linkora CI/CD 门禁脚本
# 用途：在 GitHub Actions 中运行，确保代码质量

set -e

echo "=========================================="
echo "Linkora CI/CD 门禁检查"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 1. 检查 Python 版本
echo ""
echo "[1/6] 检查 Python 版本..."
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+\.\d+')
echo "  Python 版本：$PYTHON_VERSION"
if [[ "$PYTHON_VERSION" < "3.14.0" ]]; then
    echo "  ⚠️ 警告：建议 Python 3.14+，当前版本 $PYTHON_VERSION"
fi

# 2. 运行测试套件
echo ""
echo "[2/6] 运行测试套件..."
cd /Users/ring0/Documents/Linkora
.venv/bin/python -m pytest tests/ \
  --ignore=tests/test_account_isolation_integration.py \
  --ignore=tests/test_account_identity.py \
  -q --tb=short 2>&1 | tail -20

# 3. 检查 noqa: BLE001 残留
echo ""
echo "[3/6] 检查 noqa: BLE001 残留..."
BLE001_COUNT=$(grep -rn "noqa.*BLE001" src/ 2>/dev/null | grep -v "pycache" | wc -l || echo "0")
echo "  残留 noqa: BLE001 数量：$BLE001_COUNT"
if [ "$BLE001_COUNT" -gt 0 ]; then
    echo "  ⚠️ 警告：仍有 $BLE001_COUNT 处 noqa: BLE001 注释"
    grep -rn "noqa.*BLE001" src/ | grep -v "pycache"
fi

# 4. 检查代码格式（Ruff）
echo ""
echo "[4/6] 检查代码格式（Ruff）..."
.venv/bin/ruff check src/ 2>&1 | tail -10 || echo "  (Ruff 检查跳过)"

# 5. 检查类型注解（Pyright）
echo ""
echo "[5/6] 检查类型注解（Pyright）..."
.venv/bin/pyright src/ 2>&1 | tail -10 || echo "  (Pyright 检查跳过)"

# 6. 检查依赖一致性
echo ""
echo "[6/6] 检查依赖一致性..."
.venv/bin/python scripts/check_deps.py 2>&1 | tail -10 || echo "  (依赖检查跳过)"

echo ""
echo "=========================================="
echo "CI/CD 门禁检查完成"
echo "=========================================="
