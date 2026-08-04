#!/usr/bin/env python3
"""依赖声明一致性校验。

背景：本项目历史上同时存在四处依赖声明，且互相漂移过（requirements.txt 钉
fastapi==0.140.0 而 requirements.lock 停在 0.139.0，uv.lock 里甚至完全没有
fastapi/openai 条目），导致「CI 测的版本 != 实际发布的版本」，可复现性承诺失效。

本脚本把「四源一致」固化成可执行断言，在 CI 里当门禁跑：

    requirements.txt                # 直接依赖唯一真源（全部 == 钉版）
      ├── pyproject.toml            # [project].dependencies 逐条镜像
      ├── requirements.lock         # 完整传递闭包（CI / Docker 实际安装）
      └── uv.lock                   # uv 工作流的锁文件

只做**离线**静态比对（不联网、不解析依赖），毫秒级，可安全放进任何流水线。

用法：
    python scripts/check_deps.py            # 校验，失败退出码 1
    python scripts/check_deps.py --strict   # 把环境版本告警也升级为失败
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# requirements.txt 中属于「测试期」的包：它们在 pyproject 里归到 dev extra，
# 而非 [project].dependencies。比对时需要合并两处再与 requirements.txt 对齐。
TEST_ONLY = {"pytest", "pytest-cov", "pytest-timeout"}


def norm(name: str) -> str:
    """PEP 503 名称归一化：Pillow / pillow / huggingface_hub -> 统一小写连字符。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


class Report:
    """收集 FAIL / WARN，最后统一输出，便于一次看全部问题而不是修一个跑一次。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self, msg: str) -> None:
        print(f"  \033[32mOK\033[0m   {msg}")


def parse_requirements(path: Path) -> dict[str, str]:
    """解析 requirements 系文件，返回 {归一化包名: 版本}。

    只收精确钉版（`name==version`）条目；带环境标记的 `pkg==1.0 ; sys_platform == 'linux'`
    也会被收下（标记不影响版本一致性判断）。
    """
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)", line)
        if m:
            pins[norm(m.group(1))] = m.group(2)
    return pins


def parse_unpinned(path: Path) -> list[str]:
    """找出 requirements 文件里**没有**用 == 钉死的依赖行（可复现性漏洞）。"""
    bad = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if not re.match(r"^[A-Za-z0-9_.\-]+\s*==", line):
            bad.append(line)
    return bad


def parse_pep508(specs: list[str]) -> tuple[dict[str, str], list[str]]:
    """解析 PEP 508 依赖列表，返回 ({包名: 版本}, 未钉版的原始条目)。"""
    pins: dict[str, str] = {}
    unpinned: list[str] = []
    for spec in specs:
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;,]+)", spec.strip())
        if m:
            pins[norm(m.group(1))] = m.group(2)
        else:
            unpinned.append(spec)
    return pins, unpinned


def parse_uv_lock(path: Path) -> tuple[dict[str, str], str | None]:
    """解析 uv.lock（TOML），返回 ({包名: 版本}, requires-python)。"""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pins = {
        norm(p["name"]): p["version"]
        for p in data.get("package", [])
        if "name" in p and "version" in p
    }
    return pins, data.get("requires-python")


def floor_of(requires_python: str | None) -> tuple[int, int] | None:
    """从 `>=3.12,<4` 之类的表达式里取出最低 Python 版本 (3, 12)。"""
    if not requires_python:
        return None
    best = None
    for clause in requires_python.split(","):
        m = re.match(r"^\s*>=\s*(\d+)\.(\d+)", clause)
        if m:
            cur = (int(m.group(1)), int(m.group(2)))
            if best is None or cur > best:
                best = cur
    return best


def compare(rep: Report, label: str, expected: dict[str, str], actual: dict[str, str]) -> None:
    """断言 expected 的每一项都在 actual 中存在且版本相同。"""
    missing = sorted(k for k in expected if k not in actual)
    mismatched = sorted(
        (k, expected[k], actual[k]) for k in expected if k in actual and actual[k] != expected[k]
    )
    for k in missing:
        rep.fail(f"{label}: 缺少 `{k}`（requirements.txt 要求 =={expected[k]}）")
    for k, want, got in mismatched:
        rep.fail(f"{label}: `{k}` 版本漂移 —— requirements.txt=={want} 但该文件=={got}")
    if not missing and not mismatched:
        rep.ok(f"{label}: {len(expected)} 个直接依赖全部同名同版本")


def check_environments(rep: Report, floor: tuple[int, int], strict: bool) -> None:
    """校验各运行环境的 Python 版本是否满足 requires-python 下限。

    这是真实踩过的坑：依赖钉到 numpy==2.5.1（要求 >=3.12）后，Dockerfile 仍是
    python:3.11-slim、CI 矩阵仍含 3.9，装依赖时才会炸。
    """
    fl = f"{floor[0]}.{floor[1]}"
    report = rep.fail if strict else rep.warn
    found_any = False

    ci = ROOT / ".github/workflows/ci.yml"
    if ci.exists():
        text = ci.read_text(encoding="utf-8")
        versions = set()
        for m in re.finditer(r"python-version:\s*\[([^\]]+)\]", text):
            versions |= set(re.findall(r"[\"']?(\d+\.\d+)[\"']?", m.group(1)))
        for m in re.finditer(r"python-version:\s*[\"'](\d+\.\d+)[\"']", text):
            versions.add(m.group(1))
        for v in sorted(versions):
            found_any = True
            if tuple(int(x) for x in v.split(".")) < floor:
                report(f"CI 矩阵含 Python {v}，低于依赖要求的 >={fl}，该 job 装依赖必失败")
        if versions and all(tuple(int(x) for x in v.split(".")) >= floor for v in versions):
            rep.ok(f"CI Python 版本 {sorted(versions)} 均满足 >={fl}")

    for name in ("Dockerfile", "Dockerfile.build"):
        df = ROOT / name
        if not df.exists():
            continue
        for m in re.finditer(r"^FROM\s+python:(\d+\.\d+)", df.read_text(encoding="utf-8"), re.M):
            found_any = True
            v = m.group(1)
            if tuple(int(x) for x in v.split(".")) < floor:
                report(f"{name} 基础镜像 python:{v} 低于依赖要求的 >={fl}，镜像构建会失败")
            else:
                rep.ok(f"{name} 基础镜像 python:{v} 满足 >={fl}")

    if not found_any:
        rep.warn("未找到可校验的 CI / Dockerfile Python 版本声明")


def main() -> int:
    ap = argparse.ArgumentParser(description="校验四处依赖声明是否一致")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="把「运行环境 Python 版本低于依赖要求」的告警升级为失败",
    )
    args = ap.parse_args()

    rep = Report()
    req_txt = ROOT / "requirements.txt"
    req_lock = ROOT / "requirements.lock"
    pyproject = ROOT / "pyproject.toml"
    uv_lock = ROOT / "uv.lock"

    for f in (req_txt, req_lock, pyproject):
        if not f.exists():
            print(f"[FATAL] 缺少必需文件: {f.relative_to(ROOT)}", file=sys.stderr)
            return 2

    print("依赖一致性校验 (requirements.txt = 直接依赖唯一真源)\n")

    direct = parse_requirements(req_txt)

    # ---- 1. requirements.txt 必须全部钉版 ----
    print("[1] requirements.txt 钉版完整性")
    unpinned = parse_unpinned(req_txt)
    if unpinned:
        for line in unpinned:
            rep.fail(f"requirements.txt 存在未钉版依赖 `{line}`（必须写成 name==version）")
    else:
        rep.ok(f"{len(direct)} 条依赖全部为 == 精确钉版")

    # ---- 2. pyproject.toml 与 requirements.txt 对齐 ----
    print("\n[2] pyproject.toml [project].dependencies")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    main_pins, main_unpinned = parse_pep508(project.get("dependencies", []))
    dev_specs = project.get("optional-dependencies", {}).get("dev", [])
    dev_pins, _ = parse_pep508(dev_specs)

    if not main_pins:
        rep.fail("pyproject.toml 未声明 [project].dependencies（运行时依赖必须在此声明）")
    for spec in main_unpinned:
        rep.fail(f"pyproject.toml 依赖 `{spec}` 未钉版")

    # requirements.txt 的测试段落在 pyproject 里归属 dev extra，比对时合并
    declared = {**main_pins, **{k: v for k, v in dev_pins.items() if k in TEST_ONLY}}
    compare(rep, "pyproject.toml", direct, declared)

    # 反向：pyproject 主依赖不应出现 requirements.txt 里没有的包
    for extra in sorted(set(main_pins) - set(direct)):
        rep.fail(f"pyproject.toml 多出 `{extra}`，requirements.txt 中不存在")
    # 测试依赖两边版本必须一致
    for k in sorted(TEST_ONLY & set(direct) & set(dev_pins)):
        if dev_pins[k] != direct[k]:
            rep.fail(f"dev extra `{k}`=={dev_pins[k]} 与 requirements.txt=={direct[k]} 不一致")

    # ---- 3. requirements.lock 覆盖全部直接依赖 ----
    print("\n[3] requirements.lock（CI / Docker 实际安装）")
    compare(rep, "requirements.lock", direct, parse_requirements(req_lock))

    # ---- 4. uv.lock ----
    print("\n[4] uv.lock")
    uv_requires = None
    if uv_lock.exists():
        uv_pins, uv_requires = parse_uv_lock(uv_lock)
        if not uv_pins:
            rep.fail("uv.lock 没有任何 [[package]] 条目（空壳锁文件，等同失效）")
        else:
            compare(rep, "uv.lock", direct, uv_pins)
    else:
        rep.ok("uv.lock 不存在，跳过（项目未使用 uv 工作流）")

    # ---- 5. requires-python 一致性 ----
    print("\n[5] requires-python 一致性")
    py_req = project.get("requires-python")
    floor = floor_of(py_req)
    if floor is None:
        rep.fail("pyproject.toml 未声明可解析的 requires-python 下限")
    else:
        if uv_requires and floor_of(uv_requires) != floor:
            rep.fail(f"uv.lock requires-python={uv_requires} 与 pyproject {py_req} 不一致")
        else:
            rep.ok(f"requires-python = {py_req}")
        check_environments(rep, floor, args.strict)

    # ---- 汇总 ----
    print()
    for w in rep.warnings:
        print(f"  \033[33mWARN\033[0m {w}")
    for f in rep.failures:
        print(f"  \033[31mFAIL\033[0m {f}")

    if rep.failures:
        print(
            f"\n依赖一致性校验未通过：{len(rep.failures)} 项错误。\n"
            "修复方式：以 requirements.txt 为准，同步 pyproject.toml 的 "
            "[project].dependencies，然后重新生成锁文件：\n"
            "    bash scripts/lock_deps.sh"
        )
        return 1

    print(f"依赖一致性校验通过（{len(rep.warnings)} 条告警）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
