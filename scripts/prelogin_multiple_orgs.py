#!/usr/bin/env python3
"""
多组织预登录脚本：一次性登录所有组织并保存 token。

用法：
    python scripts/prelogin_multiple_orgs.py

流程：
    1. 读取 config.yaml 中的 target_orgs 列表
    2. 对每个组织执行设备流登录（不弹窗）
    3. 输出 userCode 和验证链接，等待用户在钉钉里授权
    4. 保存所有组织的 token 到 ~/.dws/profiles.json

注意：
    - dws 本身不支持多组织 token 并存，这个脚本只是顺序登录
    - 最后登录的组织会成为当前活跃组织
    - 需要时可以切换回其他组织（token 仍在 profiles.json 里）
"""

import json
import subprocess
import sys
from pathlib import Path


def load_config():
    """加载配置文件，获取目标组织列表。"""
    import yaml
    config_path = Path(__file__).parent.parent / "config.yaml"
    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    target_orgs = config.get("target_orgs", [])
    if not target_orgs:
        print("⚠️  配置文件中未定义 target_orgs，将只登录当前组织")
        return [None]

    return target_orgs


def login_org(corp_id: str | None, corp_name: str = "") -> bool:
    """对指定组织执行设备流登录。

    Args:
        corp_id: 组织 ID（可选，不指定则登录默认组织）
        corp_name: 组织名称（仅用于日志显示）

    Returns:
        True: 登录成功
        False: 登录失败
    """
    org_label = corp_name or corp_id or "默认组织"
    print(f"\n{'='*60}")
    print(f"🔐 正在登录组织: {org_label}")
    print(f"{'='*60}")

    # 构造登录命令
    cmd = ["dws", "auth", "login", "--device-flow", "--no-browser"]
    if corp_id:
        cmd.extend(["--corp-id", corp_id])

    print(f"\n📋 执行命令: {' '.join(cmd)}")
    print("\n⏳ 请在钉钉手机端完成授权...")
    print("   等待验证码和链接输出...\n")

    try:
        # 运行登录命令（会输出 userCode 和验证链接）
        result = subprocess.run(
            cmd,
            capture_output=False,  # 让用户直接看到输出
            text=True,
        )

        if result.returncode == 0:
            print(f"\n✅ 组织 {org_label} 登录成功！")
            return True
        else:
            print(f"\n❌ 组织 {org_label} 登录失败（返回码: {result.returncode}）")
            return False

    except FileNotFoundError:
        print("\n❌ 找不到 dws 命令，请确认已安装 DingTalk Workspace CLI")
        return False
    except Exception as e:
        print(f"\n❌ 登录异常: {e}")
        return False


def check_profiles():
    """检查 profiles.json 中的登录状态。"""
    profiles_path = Path.home() / ".dws" / "profiles.json"
    if not profiles_path.exists():
        print("\n⚠️  profiles.json 不存在，可能尚未登录任何组织")
        return

    try:
        with open(profiles_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        profiles = data.get("profiles", [])
        if not profiles:
            print("\n⚠️  profiles.json 中没有组织记录")
            return

        print(f"\n{'='*60}")
        print("📊 当前登录状态（profiles.json）:")
        print(f"{'='*60}")

        current = data.get("currentProfile", "")
        for p in profiles:
            corp_id = p.get("corpId", "?")
            corp_name = p.get("corpName", "?")
            user_name = p.get("userName", "?")
            status = p.get("status", "?")
            expires_at = p.get("expiresAt", "?")

            is_current = "✓ [当前]" if corp_id == current else ""
            print(f"\n  组织: {corp_name} ({corp_id}) {is_current}")
            print(f"  用户: {user_name}")
            print(f"  状态: {status}")
            print(f"  过期: {expires_at}")

    except Exception as e:
        print(f"\n❌ 读取 profiles.json 失败: {e}")


def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║            多组织预登录工具 - DingTalk Workspace CLI           ║
╚══════════════════════════════════════════════════════════════╝

这个工具会：
1. 读取 config.yaml 中的 target_orgs 配置
2. 逐个组织执行设备流登录（不会弹窗，只输出验证码）
3. 保存所有组织的 token 到本地

请在钉钉手机端准备好：
- 确保钉钉 App 已登录
- 留意终端输出的验证码和链接
- 在手机上完成授权确认
""")

    # 1. 显示当前登录状态
    check_profiles()

    # 2. 加载目标组织列表
    target_orgs = load_config()
    print(f"\n📌 目标组织数量: {len(target_orgs)}")

    # 3. 逐个组织登录
    success_count = 0
    for org in target_orgs:
        if org is None:
            # 登录默认组织
            if login_org(None, "默认组织"):
                success_count += 1
        else:
            corp_id = org.get("corp_id") or org.get("corpId")
            corp_name = org.get("corp_name") or org.get("corpName", corp_id)
            if login_org(corp_id, corp_name):
                success_count += 1

    # 4. 显示最终状态
    print(f"\n{'='*60}")
    print(f"📊 登录完成！成功: {success_count}/{len(target_orgs)}")
    print(f"{'='*60}")
    check_profiles()

    # 5. 提示后续操作
    print("""
💡 后续操作：
   - 应用会自动读取 profiles.json 中的 token
   - token 有效期约 7 天，到期前 AuthMonitor 会自动续期
   - 如需切换组织，修改 config.yaml 中的 target_org 字段

🚀 现在可以启动主程序了: python main.py
""")


if __name__ == "__main__":
    main()
