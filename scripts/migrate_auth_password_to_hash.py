#!/usr/bin/env python3
"""把 live config.yaml 的 web.auth_password 从明文迁移为 PBKDF2 哈希。

安全约束（红线）：
  - 明文密码**绝不打印、绝不落盘**；只在内存中参与 hash_password / verify_password。
  - 迁移前**先用新哈希校验原明文**，确认 verify_password(原明文, 新哈希) == True，
    即「换成哈希后同一个密码仍能登录」，再写盘；否则中止不写。
  - 写盘前把原 config.yaml 备份到 data/config-backups/。
  - **只替换 auth_password 这一行**，不重写整个文件（避免丢参数）。
  - 幂等：已是 pbkdf2_sha256$ 开头则直接跳过。

用法：.venv/bin/python scripts/migrate_auth_password_to_hash.py [--dry-run]
"""
from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config.yaml"
BACKUP_DIR = ROOT / "data" / "config-backups"
LINE_RE = re.compile(r"^(\s*auth_password\s*:\s*)(.*)$")


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv

    # 延迟导入，确保用项目 venv 的 PBKDF2 实现
    from web.auth_middleware import hash_password, verify_password

    if not CONFIG.exists():
        print(f"ERROR: {CONFIG} 不存在", file=sys.stderr)
        return 1
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    lines = CONFIG.read_text(encoding="utf-8").splitlines(keepends=True)

    target_idx = None
    for i, ln in enumerate(lines):
        if LINE_RE.match(ln):
            target_idx = i
            break
    if target_idx is None:
        print("ERROR: 未找到 auth_password 行", file=sys.stderr)
        return 1

    m = LINE_RE.match(lines[target_idx])
    assert m is not None
    prefix, raw_val = m.group(1), m.group(2).strip()
    current = raw_val.strip("\"'")

    if current.startswith("pbkdf2_sha256$"):
        print("SKIP: auth_password 已是 PBKDF2 哈希，无需迁移。")
        return 0
    if not current:
        print("ERROR: auth_password 为空，无法迁移（fail-closed）。", file=sys.stderr)
        return 1

    # 关键：明文只在内存中流转，全程不打印
    new_hash = hash_password(current)

    # 迁移前证明：同一密码对新哈希校验通过（登录不断）
    if not verify_password(current, new_hash):
        print("ERROR: 新哈希无法校验原密码，中止迁移（fail-closed）。", file=sys.stderr)
        return 1
    # 反向：错误密码必须被拒
    if verify_password(current + "_wrong", new_hash):
        print("ERROR: 新哈希错误地接受了错误密码，中止迁移。", file=sys.stderr)
        return 1

    if dry:
        print("DRY-RUN: 校验通过。将把 auth_password 替换为 PBKDF2 哈希。")
        return 0

    # 备份
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"config.yaml.bak-{ts}-pre-pbkdf2"
    shutil.copy2(CONFIG, backup)

    # 只替换这一行；保留缩进与换行风格，值不加引号（PBKDF2 串无 YAML 特殊字符）
    nl = "\n" if lines[target_idx].endswith("\n") else ""
    lines[target_idx] = f"{prefix}{new_hash}{nl}"
    CONFIG.write_text("".join(lines), encoding="utf-8")

    # 写盘后二次验证：文件里是哈希，且能校验原密码
    check = CONFIG.read_text(encoding="utf-8")
    stored = None
    for ln in check.splitlines():
        mm = LINE_RE.match(ln)
        if mm:
            stored = mm.group(2).strip().strip("\"'")
            break
    ok = bool(stored) and stored.startswith("pbkdf2_sha256$") and verify_password(current, stored)

    print(f"备份: {backup.relative_to(ROOT)}")
    print(f"已写入 PBKDF2 哈希（长度 {len(stored)}，前缀 pbkdf2_sha256$…）")
    print("写盘后校验原密码:", "通过 ✅（登录不受影响）" if ok else "失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
