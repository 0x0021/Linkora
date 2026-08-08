#!/usr/bin/env bash
# 重新生成依赖锁文件（requirements.lock + uv.lock）。
#
# 依赖真源：requirements.txt（直接依赖，全部 == 钉版）。
# 改了 requirements.txt 之后必须重跑本脚本并提交生成物，否则 CI 的
# `scripts/check_deps.py` 门禁会失败。
#
# 注意：同步修改 requirements.txt 时，也要同步 pyproject.toml 的
# [project].dependencies（两边逐条镜像，由 check_deps.py 强制校验）。
#
# 为什么不再用 `pip freeze`：freeze 会把当前 venv 里的所有东西都写进去
# （pyinstaller / ruff / playwright 等构建与技能依赖），污染运行时锁文件；
# `uv pip compile` 只解析 requirements.txt 的真实传递闭包，且 --universal
# 可跨平台（macOS 开发 / Linux CI 与 Docker）共用同一份锁。
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "错误：未找到 uv。请先安装：brew install uv （或 pipx install uv）" >&2
    exit 1
fi

# requires-python 下限，与 pyproject.toml 的 requires-python 保持一致。
# 【务必与 pyproject.toml 同步】此前钉在 3.12 而 pyproject 已升到 >=3.14，
# 导致 `uv pip compile --python-version 3.12` 按 3.12 解析闭包，选出的是兼容
# 3.12 的旧版本（如 tokenizers 回退 0.22.2、rapidocr 回退 1.2.3），与 CI 实际
# 使用的 3.14.6 解析结果不一致 —— 这正是「本地重生成锁文件后 CI 变红」的根因。
PY_FLOOR="3.14"

echo "==> 生成 requirements.lock（requirements.txt 的完整传递闭包）"
uv pip compile requirements.txt \
    --universal \
    --python-version "${PY_FLOOR}" \
    --no-header \
    -o /tmp/linkora-req.lock

# 补回文件头说明（uv --no-header 不会写注释）
{
    cat <<'HEADER'
# Linkora 依赖锁文件 —— requirements.txt 的完整传递闭包（含精确版本）。
#
# 生成方式（需要 uv >= 0.11）：
#     bash scripts/lock_deps.sh
#
# 约定：
#   - requirements.txt 是【直接依赖】的唯一真源（全部 == 钉版），同时镜像到
#     pyproject.toml 的 [project].dependencies；本文件是解析后的完整闭包（含传递依赖）。
#   - CI 与 Docker 均安装本文件，保证「测试环境 == 发布环境」可复现。
#   - --universal 表示跨平台解析：带 ; 环境标记的条目按 OS/架构条件安装
#     （如 linux 上 torch 的 nvidia-* / triton），因此同一份锁文件在 macOS 与 Linux 均可用。
#   - 改动 requirements.txt 后必须重新生成本文件并提交，
#     否则 `python scripts/check_deps.py` 会在 CI 报错。
HEADER
    cat /tmp/linkora-req.lock
} > requirements.lock
rm -f /tmp/linkora-req.lock

echo "==> 生成 uv.lock（基于 pyproject.toml）"
uv lock

echo "==> 校验四处依赖声明一致性"
python3 scripts/check_deps.py

echo
echo "完成。请提交 requirements.lock 与 uv.lock。"
