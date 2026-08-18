#!/usr/bin/env python3
"""配置漂移防护 —— 比对 live config.yaml 与 config.yaml.example 的 key 结构。

设计目标（对应 2026-08-17 事故：live config 被整份覆盖成 example，丢全部真实定制）：
- 防止「live 有 / example 缺」的 key：一旦执行 `cp example config.yaml` 用模板覆盖运行配置，
  这些参数会被**静默整段删除**（平台级 llm/rag/tools 覆盖、少样本定制等均在此列）。
- 提示「example 有 / live 缺」的 key：通常是模板比 live 多了新参数，同步时会补进 live，风险低。

安全：只比较 key 路径（dotted path），绝不读取或打印任何配置 *值*，避免 secret 泄漏。

用法：
    # 本地守卫：覆盖前自检，live 独有非密钥 key 即报错
    python scripts/check_config_drift.py

    # 指定路径 / CI 用法（CI 中 live 通常 = example 拷贝，恒等，此脚本在 CI 意义有限）
    python scripts/check_config_drift.py --live config.yaml --example config.yaml.example

    # 也把「example 独有 key」视为错误（更严格，适合模板同步前自检）
    python scripts/check_config_drift.py --fail-on-example-only

退出码：
    0  无数据丢失风险（live 覆盖 example 不会丢非密钥参数）
    1  发现 live 独有非密钥 key（覆盖即丢，默认阻断）—— 或 --fail-on-example-only 时 example 独有 key
    2  用法 / 文件错误
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("::error:: 需要 PyYAML（pip install pyyaml）", file=sys.stderr)
    sys.exit(2)

# 密钥/敏感项启发式：这些 key 本就该只在 live 存在，example 不含属正常，
# 不应被当作「覆盖即丢失」的风险误报。
_SECRET_RE = re.compile(r"(secret|password|passwd|token|api[_-]?key|apikey|private[_-]?key|"
                        r"credential|auth|cert|salt|cookie)", re.IGNORECASE)


def _flatten(node: object, prefix: str = "") -> set[str]:
    """将嵌套 dict/list 拍平为 dotted key 路径集合（只取结构，不取值）。"""
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.add(p)
            out |= _flatten(v, p)
    elif isinstance(node, list):
        # 列表内部通常同构 dict，逐元素展开以捕获嵌套 key；
        # 不引入 index 维度，仅依赖元素结构是否一致。
        for item in node:
            out |= _flatten(item, prefix)
    return out


def _is_secret(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1]
    return bool(_SECRET_RE.search(leaf))


def _load(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层应为 mapping，实为 {type(data).__name__}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Linkora 配置漂移防护")
    parser.add_argument("--live", default="config.yaml", help="运行期配置（默认 config.yaml，gitignored）")
    parser.add_argument("--example", default="config.yaml.example", help="模板配置")
    parser.add_argument("--fail-on-example-only", action="store_true",
                        help="example 独有 key 也视为错误（更严格）")
    parser.add_argument("--no-block", action="store_true",
                        help="只打印预警、退出码恒为 0（供 pre-commit 本地钩子使用，不阻断提交）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非人类可读报告")
    args = parser.parse_args()

    live_path = Path(args.live)
    example_path = Path(args.example)

    try:
        live = _load(live_path)
        example = _load(example_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"::error:: {e}", file=sys.stderr)
        return 2

    live_keys = _flatten(live)
    example_keys = _flatten(example)

    live_only = sorted(live_keys - example_keys)       # 覆盖即丢失的候选
    example_only = sorted(example_keys - live_keys)    # 模板比 live 多，同步会补进

    # 密钥项从「数据丢失风险」中剔除：本就该只在 live 存在。
    risk_keys = [k for k in live_only if not _is_secret(k)]
    secret_only = [k for k in live_only if _is_secret(k)]

    if args.json:
        print(json.dumps({
            "risk_live_only": risk_keys,
            "secret_live_only_count": len(secret_only),
            "example_only": example_only,
            "summary": {
                "example_key_count": len(example_keys),
                "live_key_count": len(live_keys),
                "data_loss_risk": len(risk_keys),
                "template_drift": len(example_only),
            },
        }, ensure_ascii=False, indent=2))
        return 1 if (risk_keys or (args.fail_on_example_only and example_only)) and not args.no_block else 0

    print(f"配置漂移检查：example={example_path}  live={live_path}")
    print(f"  example key 数：{len(example_keys)}   live key 数：{len(live_keys)}")
    print()

    if risk_keys:
        print(f"❌ [{len(risk_keys)}] live 独有且非密钥 key —— `cp example config.yaml` 会整段删除：")
        for k in risk_keys:
            print(f"     - {k}")
    else:
        print("✅ 无数据丢失风险：live 的独有 key 均为密钥/运行时项，覆盖不会丢业务参数。")

    if secret_only:
        print(f"\n🔒 [{len(secret_only)}] live 独有密钥项（预期，不计入风险，明细略）：")

    if example_only:
        print(f"\nℹ️  [{len(example_only)}] example 独有 key（模板比 live 新，同步会补进 live）：")
        for k in example_only[:50]:
            print(f"     ~ {k}")
        if len(example_only) > 50:
            print(f"     …（其余 {len(example_only) - 50} 项省略）")
    else:
        print("\n✅ example 无独有 key（模板与 live 同构）。")

    if risk_keys or (args.fail_on_example_only and example_only):
        return 1 if not args.no_block else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
