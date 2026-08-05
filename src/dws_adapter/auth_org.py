"""DwsAdapter 认证/组织 mixin（auth status/login、profile、org 切换）。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging
from datetime import datetime

from src.dws_adapter.core import DwsError, is_org_config_problem

logger = logging.getLogger(__name__)


class DwsAdapterAuthOrgMixin(DwsAdapterBase):
    def auth_status(self) -> dict:
        """检查认证状态。返回 {"authenticated": bool, ...}"""
        try:
            data = self.run(["auth", "status"])
            result = self._get_result(data)
            if not isinstance(result, dict):
                logger.warning("auth_status 返回非字典格式: %s", type(result))
                return {"authenticated": False, "error": "invalid_response"}

            if "authenticated" in result:
                return result

            if result.get("success") is True:
                logger.debug("auth_status 返回 success=true，尝试提取认证信息")
                user_info = {}
                for key in ("user_id", "user_name", "corp_name", "expires_at"):
                    if key in result:
                        user_info[key] = result[key]
                    elif key in data:
                        user_info[key] = data[key]
                return {"authenticated": True, **user_info}

            logger.warning("auth_status 无法确定认证状态: %s", result)
            return {"authenticated": False, "error": "unknown_status"}
        except Exception as e:
            logger.warning("检查登录状态失败: %s", e)
            return {"authenticated": False, "error": str(e)}

    def auth_login(self, device_flow: bool = False,
                   no_browser: bool = True) -> dict:
        """触发登录流程。会阻塞直到登录完成。"""
        args = ["auth", "login", "-y"]
        if device_flow:
            args.append("--device")
        if no_browser:
            args.append("--no-browser")
        return self.run(args, timeout=300, force_no_dry_run=True)

    def profile_list(self) -> dict:
        """列出已登录的 dws profile（含认证状态 / 过期时间）。

        认证查询必须真实执行（不受全局 dry_run 影响）。
        注意：组织未配置 CLI 权限时，dws 会抛错，这里把该特定错误
        向上抛出，使 is_authenticated() 能据此返回 "org_not_configured"
        （否则会被当成普通未登录，触发无意义的重登弹窗）。
        """
        try:
            return self.run(["profile", "list"], force_no_dry_run=True)
        except DwsError as e:
            # 组织未开启 CLI 权限：抛出，让 is_authenticated 识别为 org_not_configured
            if is_org_config_problem(str(e)):
                raise
            return {}

    def get_current_org(self) -> dict:
        """返回当前 DWS profile 所属组织 {corp_id, corp_name}。

        优先从本地 profiles.json 读取（零网络调用，不会弹窗）；
        本地读取失败时才回退到 dws 命令。

        多组织环境下用于判断「目标组织」，便于只轮询该组织的会话、
        持久化跳过其他组织的会话（避免反复触发跨组织权限验证/弹窗）。
        """
        # 优先读本地文件（零网络、不弹窗）
        profile = self._get_current_profile_local()
        if profile:
            return {
                "corp_id": profile.get("corpId", ""),
                "corp_name": profile.get("corpName", ""),
            }

        # 回退：调用 dws 命令（可能弹窗）
        try:
            data = self.run(["profile", "list"], force_no_dry_run=True)
            result = self._get_result(data)
            if isinstance(result, dict):
                profiles = result.get("profiles", []) or []
                current = result.get("currentProfile") or ""
                for p in profiles:
                    if p.get("corpId") == current or p.get("isCurrent"):
                        return {
                            "corp_id": p.get("corpId", ""),
                            "corp_name": p.get("corpName", ""),
                        }
                if profiles:
                    return {
                        "corp_id": profiles[0].get("corpId", ""),
                        "corp_name": profiles[0].get("corpName", ""),
                    }
        except Exception as e:
            logger.warning("[DWS] 获取当前组织失败: %s", e)
        return {"corp_id": "", "corp_name": ""}

    def list_orgs(self) -> list[dict]:
        """列出已登录 DWS 的组织（供设置页「目标组织」单选下拉）。"""
        try:
            data = self.run(["profile", "list"])
            result = self._get_result(data)
            if isinstance(result, dict):
                return [
                    {
                        "corp_id": p.get("corpId", ""),
                        "corp_name": p.get("corpName", ""),
                    }
                    for p in result.get("profiles", []) or []
                ]
        except Exception as e:
            logger.warning("[DWS] 列出组织失败: %s", e)
        return []

    def use_org(self, corp_id: str) -> bool:
        """切换到指定组织（若该 corpId 有已登录的 profile）。

        用于设置页「目标组织」切换。仅在传入的 corpId 确实存在于 profile 列表时调用，
        调用失败不影响当前轮询（防御式）。返回是否成功切换。
        """
        if not corp_id:
            return False
        try:
            self.run(["profile", "use", corp_id])
            logger.info("[DWS] 已切换目标组织: %s", corp_id)
            return True
        except Exception as e:
            logger.warning("[DWS] 切换目标组织失败（忽略，继续使用当前组织）: %s", e)
            return False

    def is_authenticated(self) -> bool | str:
        """通过本地 profiles.json 判断当前是否已登录且 token 有效。

        零网络调用、不会触发 dws 弹窗。直接读取 ~/.dws/profiles.json 检查：
        - 是否存在 active 状态的 profile
        - expiresAt 是否未过期

        Returns:
            True: 已登录且 token 有效
            False: 未登录 / token 过期（可尝试重新登录）
            "org_not_configured": 组织未开启 CLI 数据访问权限（重登无法解决）

        注意：org_not_configured 无法通过本地文件判断，必须真实调用 dws 才知道。
        本地文件检测只返回 True/False，org_not_configured 由调用方在
        真实业务 API 失败后通过 is_org_config_problem() 自行判定。
        """
        profile = self._get_current_profile_local()
        if not profile:
            return False

        status = profile.get("status")
        if status and status != "active":
            return False

        exp = profile.get("expiresAt") or profile.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(exp)
                if exp_dt.tzinfo is not None:
                    # 带时区：先转为本地时区，再去掉 tzinfo，避免与本地 now() 比较错误
                    exp_dt = exp_dt.astimezone().replace(tzinfo=None)
                if exp_dt <= datetime.now():
                    return False
            except (ValueError, TypeError) as _exc:
                logger.warning(f"is_authenticated: swallowed exception: {_exc}")
                pass
        return True
