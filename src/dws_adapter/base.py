"""DwsAdapter 引擎钩子 mixin（CLI 拼命令 / 错误分类 / 本地 profile 缓存）。拆分自 dws_adapter.py。"""
from __future__ import annotations

import logging
import json
import os
from pathlib import Path
from typing import Any

from src.im_adapter.errors import (
    IMAdapterError,
    IMAdapterNonRetryableError,
    IMAdapterPermissionError,
    IMAdapterRetryableError,
)
from src.dws_adapter.core import (
    DwsError, DwsRetryableError, DwsNonRetryableError,
    DwsPermissionError, _NO_BROWSER_ENV, classify_dws_error,
)

logger = logging.getLogger(__name__)


class DwsAdapterBaseMixin:
    def __init__(self, cli_path: str = "dws", timeout: int = 30,
                 retries: int = 2, dry_run: bool = True, profile: str = "",
                 ai_tag_default: bool = True):
        super().__init__(cli_path=cli_path, timeout=timeout,
                         retries=retries, dry_run=dry_run, profile=profile)
        # 发送消息时是否默认携带 AI 标记（--ai-tag）。各发送调用未显式传 ai_tag 时回退此默认。
        self.ai_tag_default = ai_tag_default
        self._perm_warned: set[str] = set()  # 已警告过的权限错误类型
        # 本地 dws 配置目录（认证文件存储位置）
        self._dws_config_dir = Path(os.environ.get("DWS_CONFIG_DIR", "~/.dws")).expanduser()

    def _make_no_browser_env(self) -> dict[str, str]:
        """钉钉专属：阻止 dws 弹 OAuth 窗口的环境变量。"""
        return _NO_BROWSER_ENV

    def _build_command(self, args: list[str], *,
                       force_no_dry_run: bool = False) -> list[str]:
        """钉钉 ``dws`` 命令语法：``dws <args> -f json -y [--no-browser] [--dry-run] [--profile]``。"""
        cmd = [self.cli_path] + list(args) + ["-f", "json", "-y"]
        # --no-browser 是 auth login 的专属参数，不是所有 dws 子命令都支持
        # 对 auth 类命令自动添加，避免运行时弹浏览器
        if "--no-browser" not in args and (args and args[0] == "auth"):
            cmd.append("--no-browser")
        # 【并发安全】只读查询类操作需真实执行（即使全局 dry_run=True），
        # 以前通过临时改 self.dry_run 实现，但 DwsAdapter 单实例被 poller/后台摘要/web
        # 多线程共享，临时改实例状态会串线程（可能导致 dry-run 模式下真发消息）。
        # 改用参数局部化，不再触碰实例状态。
        if self.dry_run and not force_no_dry_run and "--dry-run" not in args:
            cmd.append("--dry-run")
        if self.profile and "--profile" not in args:
            cmd.extend(["--profile", self.profile])
        return cmd

    def _classify_error(self, error_msg: str) -> type[IMAdapterError]:
        """钉钉错误文本 → IMAdapter* 异常类。"""
        return classify_dws_error(error_msg)

    def _is_benign_error(self, error_msg: str) -> bool:
        """钉钉业务级错误（保密群 / 无权限 / API 不支持等）降级为 debug 日志。

        上游调用方（如 poller_strategy 的已读不回闸门）已在 catch 块里做了
        优雅降级（关闭闸门 + WARNING 日志），此处仅需避免底层重复打 ERROR 噪音。
        """
        return any(
            kw in error_msg
            for kw in (
                "保密群", "AUTH_PERMISSION_DENIED", "无法获取",
                "openCid or cid is required",
                # 钉钉 OpenAPI 业务级错误：API 对当前应用不可用/无权限，
                # 属永久性条件（非瞬态），上游已有降级兜底，不应每轮刷 ERROR。
                "business_error", "UNCLASSIFIED",
            )
        )

    def _retryable_error_class(self) -> type[IMAdapterRetryableError]:
        return DwsRetryableError

    def _non_retryable_error_class(self) -> type[IMAdapterNonRetryableError]:
        return DwsNonRetryableError

    def _permission_error_class(self) -> type[IMAdapterPermissionError]:
        return DwsPermissionError

    def _base_error_class(self) -> type[IMAdapterError]:
        return DwsError

    def _read_local_profiles(self) -> dict:
        """从本地 profiles.json 读取认证信息（零网络调用，不会弹窗）。

        Returns:
            profiles.json 的完整内容，失败返回空 dict
        """
        profiles_path = self._dws_config_dir / "profiles.json"
        try:
            if not profiles_path.exists():
                return {}
            with open(profiles_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("读取本地 profiles.json 失败: %s", e)
            return {}

    def _get_current_profile_local(self) -> dict:
        """从本地文件读取当前 profile 信息（零网络调用，不会弹窗）。

        Returns:
            当前 profile dict，没有则返回空 dict
        """
        data = self._read_local_profiles()
        if not data:
            return {}
        profiles = data.get("profiles") or []
        if not profiles:
            return {}
        current_id = data.get("currentProfile") or data.get("primaryProfile") or ""
        if current_id:
            for p in profiles:
                if p.get("corpId") == current_id or p.get("name") == current_id:
                    return p
        # 没有当前标记，返回第一个
        return profiles[0] if profiles else {}

    def _get_result(self, data: dict) -> Any:
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def _is_personal_dingtalk_error(self, err_str: str) -> bool:
        """判断是否个人钉钉不支持的企业级 API 错误或认证失效。"""
        return any(
            code in err_str
            for code in (
                "CREATE_APP_FAILED",
                "TOKEN_VERIFIED_FAILED",
                "该组织尚未开启 CLI 数据访问权限",
                "AGENT_CODE_NOT_EXISTS",  # 认证会话失效
            )
        )
