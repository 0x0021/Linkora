#!/usr/bin/env bash
# Linkora 发版辅助脚本 —— 锁死「发版=打 tag + 创建 GitHub Release」工序。
#
# 背景：v0.4.4 之后曾长期漏发 tag/Release（只改了 CHANGELOG 与文档站，
# 手工 git tag + gh release 这一步断档），导致「文档宣 v0.4.7、GitHub Release 停在
# v0.4.4」的断裂。本脚本把这道半公开、易忘的工序标准化，减少人为漏发。
#
# 约定：
#   - 版本号以 pyproject.toml 的 `version` 为真相；也可在命令行显式传入覆盖。
#   - Release notes 直接从 docs/CHANGELOG.md 抽取对应版本段（## vX.Y.Z 到下一个 ## v 为止），
#     因此发版前请确保 CHANGELOG 已写好该版本段，且标题格式为 `## vX.Y.Z (YYYY-MM-DD)`。
#   - 发版前若 CHANGELOG 顶部存在 `## 未发布 (YYYY-MM-DD)` 段，须先将其标题改成
#     `## vX.Y.Z (YYYY-MM-DD)`（即「未发布段转正」）再跑本脚本——脚本只认 `## vX.Y.Z `
#     开头的段，不会自动合并未发布段，找不到就报错中止。
#   - tag 名固定为 `v<version>`（annotated），GitHub Release 标题为 `灵桥 Linkora v<version>`。
#   - 远程固定为 `github`（0x0021/Linkora）；若该 remote 不存在则回退 `origin`。
#   - 本脚本不触碰任何源码与配置（含 config.yaml），只动 tag / Release / CHANGELOG 读取。
#
# 用法：
#   bash scripts/release.sh                 # 发行 pyproject.toml 当前 version
#   bash scripts/release.sh 0.4.8           # 发行指定版本
#   bash scripts/release.sh --dry-run       # 只打印计划 + notes 预览，不实际打 tag/发 Release
#   bash scripts/release.sh 0.4.8 --dry-run
set -euo pipefail

cd "$(dirname "$0")/.."

# ---------- 参数解析 ----------
VERSION=""
DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --help|-h) sed -n '1,40p' "$0"; exit 0 ;;
    -*) echo "未知参数：$a" >&2; exit 1 ;;
    *) VERSION="$a" ;;
  esac
done

# ---------- 依赖检查 ----------
need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "错误：未找到 $1" >&2; exit 1; }; }
need_cmd git
need_cmd gh

# ---------- 解析版本号 ----------
if [[ -z "$VERSION" ]]; then
  VERSION=$(grep -m1 '^version = ' pyproject.toml | sed -E 's/version = "([^"]+)"/\1/')
fi
if [[ -z "$VERSION" ]]; then
  echo "错误：无法从 pyproject.toml 解析 version，且未传入版本号" >&2
  exit 1
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：版本号格式应为 X.Y.Z，收到：$VERSION" >&2
  exit 1
fi
TAG="v${VERSION}"

# ---------- 解析远程名（github 优先，回退 origin）----------
REMOTE="github"
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  REMOTE="origin"
  if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
    echo "错误：未找到 remote 'github' 或 'origin'" >&2
    exit 1
  fi
fi

echo "==> 目标：发行 ${TAG}（remote=${REMOTE}）"

# ---------- 前置校验 ----------
# 1) 工作区干净（未被 --dry-run 时强制，但仍警告）
if [[ -n "$(git status --porcelain)" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "⚠️  工作区有未提交改动（dry-run 下继续预览）："
    git status --porcelain | sed 's/^/      /'
  else
    echo "错误：工作区有未提交改动，请先提交/暂存后再发版：" >&2
    git status --porcelain >&2
    exit 1
  fi
fi

# 2) tag 是否已存在
if git tag -l "$TAG" | grep -qx "$TAG"; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "⚠️  tag ${TAG} 已存在（dry-run 继续预览 notes；实际发行会冲突需先删旧 tag）"
  else
    echo "错误：tag ${TAG} 已存在，重复发行会冲突。如需重新发，请先删除旧 tag。" >&2
    exit 1
  fi
fi

# 3) gh 是否已登录
if ! gh auth status >/dev/null 2>&1; then
  echo "错误：gh 未登录，请先 `gh auth login`" >&2
  exit 1
fi

# 4) CHANGELOG 是否含该版本段
VER_ESC=$(printf '%s' "$VERSION" | sed 's/[.]/\\./g')
NOTES=$(awk "/^## v${VER_ESC} /{f=1; next} /^## v[0-9]/{if(f)exit} f" docs/CHANGELOG.md)
if [[ -z "$(echo "$NOTES" | tr -d '[:space:]')" ]]; then
  echo "错误：docs/CHANGELOG.md 未找到版本段 '## v${VERSION} '（标题需形如 '## v${VERSION} (YYYY-MM-DD)'）" >&2
  exit 1
fi

# ---------- 预览 / 执行 ----------
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "---------- DRY RUN：Release notes 预览（来自 docs/CHANGELOG.md）----------"
  echo "$NOTES"
  echo "---------------------------------------------------------------------------"
  echo "[dry-run] 将执行："
  echo "  git tag -a ${TAG} -m '灵桥 Linkora ${TAG}'"
  echo "  git push ${REMOTE} main"
  echo "  git push ${REMOTE} ${TAG}"
  echo "  gh release create ${TAG} --repo <remote> --title '灵桥 Linkora ${TAG}' --notes-file <CHANGELOG 段>"
  echo "（未实际执行）"
  exit 0
fi

# 创建临时 notes 文件（gh release create --notes-file 需要）
NOTES_FILE=$(mktemp "/tmp/linkora-release-${VERSION}-XXXXXX")
printf '%s\n' "$NOTES" > "$NOTES_FILE"

REPO=$(git remote get-url "$REMOTE" | sed -E 's#\.git$##; s#(https://[^/]+/|git@[^:]+:)##')

cleanup() { rm -f "$NOTES_FILE"; }
trap cleanup EXIT

echo "==> 创建 annotated tag ${TAG}"
git tag -a "$TAG" -m "灵桥 Linkora ${TAG}"

echo "==> 推送 main 与 tag"
git push "$REMOTE" main
git push "$REMOTE" "$TAG"

echo "==> 创建 GitHub Release"
gh release create "$TAG" \
  --repo "$REPO" \
  --title "灵桥 Linkora ${TAG}" \
  --notes-file "$NOTES_FILE"

echo
echo "✅ 已发行 ${TAG}：https://github.com/${REPO}/releases/tag/${TAG}"
