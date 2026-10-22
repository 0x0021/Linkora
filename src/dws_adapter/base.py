"""DwsAdapter 引擎钩子 mixin（CLI 拼命令 / 错误分类 / 本地 profile 缓存）。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

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


class DwsAdapterBaseMixin(DwsAdapterBase):
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
        """钉钉 ``dws`` 命令语法：``dws <args> -f json -y [--no-browser] [--dry-run] [--profile]``。

        ⚠️ 刻意**不**传 dws 的三个输出相关全局 flag（2026-10-22 实测 v1.0.63 结论）：
        适配器层的职责是「拿到原始 JSON 后在 Python 侧归一化」，一旦输出被 CLI 侧
        改写，归一化赖以工作的字段就可能被裁掉，且失效方式是**静默降级**而非报错：

        - ``--fields <逗号分隔字段>``：只在**顶层**做投影。传
          ``--fields name,conversationId`` 时，``+chat-list`` 返回 ``{}``（不报错）——
          因为 ``name``/``conversationId`` 嵌在 ``chats[]`` 内而非顶层。适配器的
          ``_get_result()`` 会拿到空 dict，归一化全部退化为空列表。
        - ``--jq <表达式>``：同样改写输出形状；且表达式写错（字段不存在）时返回
          ``null`` 且 **exit code 0**，无任何错误信号可据以判失败。
        - ``--mock``：**只对 Atomic 生效、对 Shortcut 无效**（实测
          ``+chat-list --mock`` 仍返回真实空结果，``+chat-members-list --mock`` 仍走
          真实语义解析并报 ``resolution_not_found``）。能力残缺，无法作为统一的
          测试替身；且原子命令的 mock 返回体带 ``"_mock": true`` +
          ``contractVersion`` 标记，适配器无识别逻辑，误传会污染真实数据路径。

        反之 ``--dry-run`` 是**采纳**的（见下方），它只预览副作用、不改写输出形状，
        与归一化层正交。
        """
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

        ⚠️ 判定顺序不可调换：必须先排除「瞬态可重试错误」（超时 / 限流 / 连接
        抖动）。DWS 任何 ``success=false`` 的响应外壳都带通用的
        ``reason=business_error``，而可重试的瞬态错误同样带这个外壳；若被下面的
        ``business_error`` 关键字拦下判为「良性」，就会**跳过重试**，令 ``retries``
        配置形同虚设。（2026-09-15 事故：``server_error_code=TIMEOUT_ERROR`` 且
        ``retryable=true`` 的群消息拉取失败被误判良性、永不重试，线上表现为零星
        「列出 X 的消息失败」告警。）
        """
        if classify_dws_error(error_msg) is DwsRetryableError:
            return False  # 瞬态错误必须走重试，不算良性
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

    @staticmethod
    def _readable_dws_error(err: Exception) -> str:
        """从 dws 错误串里提取**人可读**的失败原因。

        dws CLI 失败时抛出的异常串形如::

            dws exit 3: {
              "ok": false,
              "error": {"type":"validation","message":"以下参与者无法解析为唯一 userId：..."}
            }

        JSON 壳对 LLM 与人都很难读（要跨行拼接、含转义换行）。本方法尝试把
        内嵌 JSON 解析出来，取 ``error.message``（或 ``message``/``errorMsg``）；
        解析不出就原样返回异常串。
        """
        import json
        import re

        text = str(err)
        # 取第一对大括号之间的内容（dws 错误 JSON 就在其中）
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return text
        try:
            payload = json.loads(match.group(0))
        except (ValueError, TypeError):
            return text
        if not isinstance(payload, dict):
            return text
        err_obj = payload.get("error")
        if isinstance(err_obj, dict):
            msg = err_obj.get("message") or err_obj.get("errorMsg")
            if msg:
                return str(msg)
        msg = payload.get("message") or payload.get("errorMsg")
        if msg:
            return str(msg)
        return text

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
