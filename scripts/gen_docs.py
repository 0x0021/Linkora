#!/usr/bin/env python3
"""Linkora 文档自动生成脚本。

用途：
1. 从测试文件自动生成测试覆盖报告
2. 从 git log 自动生成 CHANGELOG 条目
3. 从源码注释自动生成 API 文档

用法：
    scripts/gen_docs.py --all        # 生成所有文档
    scripts/gen_docs.py --changelog  # 仅生成 CHANGELOG
    scripts/gen_docs.py --coverage   # 仅生成测试覆盖报告
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime

# 自动探测最新 tag 失败时的兜底起点（仅用于 git 不可用/无 tag 的极端场景）。
# 正常情况下 --since 不传即自动取最新 tag，不会走到这里。
_FALLBACK_SINCE_TAG = "v0.4.8"


def _latest_tag() -> str | None:
    """返回仓库最新 tag，作为 CHANGELOG 的起始点。

    历史教训：此处曾硬编码 ``v0.4.4`` / ``v0.4.6``，每次发版后都会过期，
    导致生成的「待发布变更」里混进大量早已发布的版本（v0.4.5~v0.4.8）内容。
    现改为自动探测，随发版自动跟进。

    Returns:
        最新 tag 名；浅克隆（无 tag）或 git 不可用时返回 None。
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_git_log_since(tag: str) -> list[str]:
    """获取自指定 tag 以来的 git log。"""
    try:
        result = subprocess.run(
            ["git", "log", f"{tag}..HEAD", "--pretty=format:%s"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except subprocess.CalledProcessError as e:
        print(f"Git log 获取失败：{e}")
        return []


def parse_commit_message(msg: str) -> dict:
    """解析 commit message，提取 type 和 scope。"""
    match = re.match(r"^(\w+)(\((\w+)\))?:\s*(.+)$", msg)
    if match:
        return {
            "type": match.group(1),
            "scope": match.group(3) or "general",
            "message": match.group(4),
        }
    return {"type": "other", "scope": "general", "message": msg}


def generate_changelog(since_tag: str | None = None) -> str:
    """生成 CHANGELOG 条目。

    Args:
        since_tag: 起始 tag。为 ``None`` 时自动取仓库最新 tag（取不到则回退
            ``_FALLBACK_SINCE_TAG``），使输出始终等于「自上次发版以来的未发布变更」。
    """
    tag = since_tag or _latest_tag() or _FALLBACK_SINCE_TAG
    commits = get_git_log_since(tag)
    if not commits:
        return f"## {datetime.now().strftime('%Y-%m-%d')} (未发布)\n\n> 自 {tag} 以来无变更\n"

    # 按类型分组
    types = {
        "feat": ("新功能", []),
        "fix": ("缺陷修复", []),
        "refactor": ("重构", []),
        "perf": ("性能优化", []),
        "test": ("测试", []),
        "docs": ("文档", []),
        "chore": ("杂项", []),
    }

    for commit in commits:
        parsed = parse_commit_message(commit)
        if parsed["type"] in types:
            types[parsed["type"]][1].append(commit)

    # 生成 Markdown
    now = datetime.now().strftime("%Y-%m-%d")
    lines = [
        "# Linkora 待发布变更（自动生成，请勿手工编辑）\n",
        f"> 生成时间：{now}　|　起始 tag：{tag}（自动取仓库最新 tag）　|　"
        "生成方式：`scripts/gen_docs.py --changelog`\n",
        f"\n## {now} (未发布)\n",
        f"\n> 自 {tag} 以来的变更\n\n",
    ]

    for chinese_name, items in types.values():
        if items:
            lines.append(f"### {chinese_name}\n")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

    return "\n".join(lines)


def generate_coverage_report() -> str:
    """生成测试覆盖报告。

    注意：此处曾硬编码 ``.venv/bin/python``，在 CI（ubuntu-latest 用系统 Python
    装依赖、无 .venv）必然失败并静默落到兜底值，导致报告长期停留在过期的
    「1370 个」。改用 ``sys.executable`` 跟随当前解释器。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--co", "-q"],
            capture_output=True,
            text=True,
            check=True,
        )
        # 解析收集的测试数量
        match = re.search(r"collected (\d+) items", result.stdout)
        if match:
            count = match.group(1)
            return f"- 测试文件：190+ 个\n- 测试用例：{count} 个\n"
    except Exception as e:
        print(f"生成覆盖报告失败：{e}")
    return "- 测试用例：收集失败（见上方错误信息）\n"


def main():
    parser = argparse.ArgumentParser(description="Linkora 文档自动生成")
    parser.add_argument("--all", action="store_true", help="生成所有文档")
    parser.add_argument("--changelog", action="store_true", help="仅生成 CHANGELOG")
    parser.add_argument("--coverage", action="store_true", help="仅生成测试覆盖报告")
    parser.add_argument(
        "--since",
        default=None,
        help="CHANGELOG 起始 tag（默认自动取仓库最新 tag，即「自上次发版以来的未发布变更」）",
    )
    args = parser.parse_args()

    # 生成 CHANGELOG
    # 注：段落分隔头走 stderr，保证 stdout 是纯净 Markdown，可直接重定向成文件
    if args.changelog or args.all:
        changelog = generate_changelog(args.since)
        print("=== CHANGELOG ===", file=sys.stderr)
        print(changelog)

    # 生成测试覆盖报告
    if args.coverage or args.all:
        coverage = generate_coverage_report()
        print("=== TEST COVERAGE ===", file=sys.stderr)
        print(coverage)


if __name__ == "__main__":
    main()
