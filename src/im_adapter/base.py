"""IM CLI 适配器抽象基类。

把「执行引擎」（拼命令 → subprocess → 解析 JSON → 错误分类 → 退避重试 →
文件下载校验）从具体平台（钉钉 ``dws``）中抽离出来，作为可继承的模板。
平台差异通过以下**钩子方法**注入，子类覆写即可：

====================  =========================================================
钩子                   作用
====================  =========================================================
``_build_command``    把业务 args 拼成完整 CLI 命令（输出格式 / 干跑 / profile 等平台语法）
``_classify_error``    把 CLI 错误文本映射为 ``IMAdapter*`` 异常类
``_make_no_browser_env`` 子进程环境（阻止 OAuth 弹窗等）
``_is_benign_error``   业务级错误是否降级为 debug 日志（如「保密群」）
``_retryable_error_class`` 等 4 个  返回本平台使用的异常类（便于日志 / 类型区分）
====================  =========================================================

能力方法（发消息 / 拉会话 / 上传媒体等）**不在此声明**，由各具体平台适配器实现，
接口契约见 ``src.im_adapter.capabilities.IMCapabilitySkeleton``（含全部方法桩与文档）。

典型用法::

    class MyAdapter(BaseIMAdapter):
        def _build_command(self, args, force_no_dry_run=False): ...
        def _classify_error(self, text): ...

线程安全：用 ``force_no_dry_run`` 局部化干跑参数，**从不修改实例状态**，
因此单个共享实例可被 poller / web / 后台线程安全复用。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

# 重试退避上限（秒）：防止 retries 配置被调大时 2**attempt 指数退避暴涨，
# 长时间阻塞调用线程（原逻辑 wait = min(2 ** attempt, MAX_BACKOFF_SECONDS) 无封顶）。
MAX_BACKOFF_SECONDS = 30

from .errors import (
    IMAdapterError,
    IMAdapterNonRetryableError,
    IMAdapterPermissionError,
    IMAdapterRetryableError,
    IMAdapterShutdownError,
)

logger = logging.getLogger(__name__)


class BaseIMAdapter:
    """IM CLI 适配器基类：提供通用执行引擎，平台语法通过钩子注入。

    不要直接实例化本类——请继承并实现 ``_build_command`` / ``_classify_error``
    以及各能力方法（参考 ``src/im_adapter/feishu.py`` / ``wecom.py`` 骨架）。
    """

    #: 干跑参数名。钉钉用 ``--dry-run``，部分平台语法不同，可覆写。
    dry_run_flag: str = "--dry-run"

    def __init__(self, cli_path: str = "dws", timeout: int = 30,
                 retries: int = 2, dry_run: bool = True, profile: str = ""):
        self.cli_path = cli_path
        self.timeout = timeout
        self.retries = retries
        self.dry_run = dry_run
        self.profile = profile
        # 子进程环境（阻止 CLI 内部 OAuth 弹窗），具体值由子类 _make_no_browser_env 决定
        self._no_browser_env: dict[str, str] = self._make_no_browser_env()

    # ------------------------------------------------------------------
    # 可覆写钩子
    # ------------------------------------------------------------------

    def _make_no_browser_env(self) -> dict[str, str]:
        """构造无浏览器弹窗的子进程环境（通用版）。平台可覆写以追加专属变量。"""
        return {
            **os.environ,
            "BROWSER": "/bin/echo",
            "BROWSER_NON_INTERACTIVE": "1",
        }

    def _build_command(self, args: list[str], *,
                       force_no_dry_run: bool = False) -> list[str]:
        """把业务参数拼成完整 CLI 命令（通用版：仅加干跑 / profile）。

        钉钉 / 飞书 / 企微的语法差异很大，请在各子类覆写本方法。
        """
        cmd = [self.cli_path, *list(args)]
        if self.dry_run and not force_no_dry_run:
            cmd.append(self.dry_run_flag)
        if self.profile and "--profile" not in args:
            cmd.extend(["--profile", self.profile])
        return cmd

    @staticmethod
    def _extract_json(output: str) -> Any | None:
        """从 CLI stdout 中提取最后一个合法 JSON 对象/数组。

        某些 CLI（如 lark-cli）会把安装提示、进度条、npm 风格日志等非 JSON
        内容打到 stdout，真正的 JSON 响应夹杂其中。委托给 ``llm_json`` 的
        唯一真源实现 ``extract_last_json``（取最后一次成功解析的对象），
        消除本地的裸 json 解析副本。空输出兼容旧行为返回 ``{}``。
        """
        if not output:
            return {}
        from src.utils.llm_json import extract_last_json
        return extract_last_json(output)

    def _classify_error(self, error_msg: str) -> type[IMAdapterError]:
        """把 CLI 错误文本映射为异常类。通用版：一律 ``IMAdapterError``（不可重试）。

        子类应覆写以识别各自平台的「可重试 / 权限 / 不可重试」模式。
        """
        return IMAdapterError

    def _is_benign_error(self, error_msg: str) -> bool:
        """业务级错误是否降级为 debug 日志。通用版：``False``（按 error 记）。"""
        return False

    def _retryable_error_class(self) -> type[IMAdapterRetryableError]:
        return IMAdapterRetryableError

    def _non_retryable_error_class(self) -> type[IMAdapterNonRetryableError]:
        return IMAdapterNonRetryableError

    def _permission_error_class(self) -> type[IMAdapterPermissionError]:
        return IMAdapterPermissionError

    def _base_error_class(self) -> type[IMAdapterError]:
        return IMAdapterError

    def _shutdown_error_class(self) -> type[IMAdapterShutdownError]:
        """子进程被信号终止（Ctrl+C 等关机场景），非可重试。"""
        return IMAdapterShutdownError

    # ------------------------------------------------------------------
    # 通用执行引擎
    # ------------------------------------------------------------------

    def run(self, args: list[str], timeout: int | None = None,
            retries: int | None = None,
            operation: str = "",
            force_no_dry_run: bool = False) -> dict:
        """执行一条 CLI 命令，返回解析后的 JSON dict（通用引擎）。

        流程：拼命令 → subprocess → 解析 stdout JSON → 失败按 ``_classify_error``
        抛异常 → 可重试错误指数退避重试。线程安全：干跑靠 ``force_no_dry_run``
        局部化，不触碰实例状态。
        """
        timeout = timeout if timeout is not None else self.timeout
        retries = retries if retries is not None else self.retries
        if retries < 0:
            raise ValueError(f"retries must be >= 0, got {retries}")

        cmd = self._build_command(args, force_no_dry_run=force_no_dry_run)
        last_error: IMAdapterError | None = None
        for attempt in range(retries + 1):
            try:
                logger.debug("正在运行: %s", " ".join(cmd))
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, encoding="utf-8",
                    env=self._no_browser_env,
                )
                if result.returncode != 0:
                    stderr = result.stderr.strip() or result.stdout.strip()
                    if result.returncode < 0:
                        # 负数退出码 = 子进程被信号杀死（如 Ctrl+C 时整个进程组
                        # 收到 SIGINT，lark-cli 被一并终止 → returncode 为 -2）。
                        # 这几乎只发生在父进程退出阶段，属正常关机而非真实故障，
                        # 降级为 debug 且不重试，避免关机时刷满 ERROR 日志。
                        sig = -result.returncode
                        logger.debug("%s 子进程被信号 %d 终止（可能处于关机阶段）: %s",
                                     self.cli_path, sig, stderr)
                        raise self._shutdown_error_class()(
                            f"{self.cli_path} terminated by signal {sig}: {stderr}")
                    error_class = self._classify_error(stderr)
                    raise error_class(f"{self.cli_path} exit {result.returncode}: {stderr}")

                output = result.stdout.strip()
                if not output:
                    return {}

                data = self._extract_json(output)
                if data is None:
                    raise IMAdapterError(
                        f"Failed to parse JSON from {self.cli_path} stdout\n{output[:500]}")

                if isinstance(data, dict) and data.get("success") is False:
                    err = data.get("error", {})
                    msg = err.get("message", str(data)) if isinstance(err, dict) else str(err)
                    error_class = self._classify_error(msg)
                    raise error_class(f"{self.cli_path} error: {msg}")

                return data

            except subprocess.TimeoutExpired:
                last_error = self._retryable_error_class()(
                    f"{self.cli_path} timeout after {timeout}s")
                if attempt < retries:
                    wait = min(2 ** attempt, MAX_BACKOFF_SECONDS)
                    logger.warning("%s 第 %d 次尝试超时，%d秒后重试",
                                   self.cli_path, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    logger.error("%s 重试 %d 次后失败: 超时", self.cli_path, retries + 1)
                    raise last_error

            except self._permission_error_class() as e:
                logger.debug("%s 权限错误（不重试）: %s", self.cli_path, e)
                raise

            except self._non_retryable_error_class() as e:
                logger.debug("%s 不可重试错误: %s", self.cli_path, e)
                raise

            except self._retryable_error_class() as e:
                # 双保险：即便错误被分类为「可重试」，只要命中良性模式（如钉钉保密群、
                # 无权限等业务级错误），也降级为 debug 且不再重试——避免每轮 poller 都
                # 对注定失败的请求做指数退避刷满 ERROR 日志。
                if self._is_benign_error(str(e)):
                    logger.debug("%s 良性错误（不重试，降级）: %s", self.cli_path, e)
                    raise
                last_error = e
                if attempt < retries:
                    wait = min(2 ** attempt, MAX_BACKOFF_SECONDS)
                    logger.debug("%s 第 %d 次尝试失败（可重试）: %s，%d秒后重试",
                                 self.cli_path, attempt + 1, e, wait)
                    time.sleep(wait)
                else:
                    logger.error("%s 重试 %d 次后失败: %s", self.cli_path, retries + 1, e)
                    raise

            except self._shutdown_error_class() as e:
                # 关机阶段子进程被信号杀掉：已按 debug 记过，原样抛出让上层感知中断
                raise

            except self._base_error_class() as e:
                err_str = str(e)
                if self._is_benign_error(err_str):
                    logger.debug("%s 业务错误（降级）: %s", self.cli_path, e)
                else:
                    logger.error("%s 未知错误: %s", self.cli_path, e)
                raise

        raise last_error  # type: ignore

    def _run_download(self, args: list[str], output_path: str,
                      *, timeout: int | None = None,
                      retries: int | None = None,
                      cwd: str | None = None) -> str:
        """通用文件下载执行器：拼命令 → subprocess → 校验产物非空。

        各平台把「下载命令尾参」作为 ``args`` 传入即可复用重试与文件校验逻辑。
        仅用于会写出本地文件的命令（不受全局 ``dry_run`` 影响，强制真实执行）。

        返回写入的本地文件路径；命令退出非 0 或产物文件不存在 / 为空时抛
        ``IMAdapterError``（或其子类）。
        """
        timeout = timeout or self.timeout
        retries = retries if retries is not None else self.retries
        cmd = self._build_command(args, force_no_dry_run=True)
        last_error: IMAdapterError | None = None
        for attempt in range(retries + 1):
            try:
                _result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, encoding="utf-8",
                    env=self._no_browser_env,
                    cwd=cwd,
                )
                if _result.returncode != 0:
                    stderr = (_result.stderr or _result.stdout or "").strip()
                    if _result.returncode < 0:
                        sig = -_result.returncode
                        logger.debug("%s 下载子进程被信号 %d 终止（可能处于关机阶段）: %s",
                                     self.cli_path, sig, stderr)
                        raise self._shutdown_error_class()(
                            f"{self.cli_path} terminated by signal {sig}: {stderr}")
                    error_class = self._classify_error(stderr)
                    raise error_class(f"{self.cli_path} exit {_result.returncode}: {stderr}")
                if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    raise self._base_error_class()(
                        f"{self.cli_path} 下载未生成有效文件: {output_path}")
                MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50MB
                file_size = os.path.getsize(output_path)
                if file_size > MAX_DOWNLOAD_SIZE:
                    raise self._base_error_class()(
                        f"{self.cli_path} 下载文件过大 ({file_size / 1024 / 1024:.1f}MB)，"
                        f"超出 {MAX_DOWNLOAD_SIZE / 1024 / 1024:.0f}MB 限制: {output_path}")
                return output_path
            except subprocess.TimeoutExpired:
                last_error = self._retryable_error_class()(
                    f"{self.cli_path} download timeout after {timeout}s")
                if attempt < retries:
                    wait = min(2 ** attempt, MAX_BACKOFF_SECONDS)
                    logger.warning("%s 下载第 %d 次尝试超时，%d秒后重试",
                                   self.cli_path, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    raise last_error
            except self._permission_error_class() as e:
                logger.debug("%s 下载权限错误（不重试）: %s", self.cli_path, e)
                raise
            except self._non_retryable_error_class() as e:
                logger.debug("%s 下载不可重试错误: %s", self.cli_path, e)
                raise
            except self._retryable_error_class() as e:
                # 双保险：即便错误被分类为「可重试」，只要命中良性模式（如保密群、
                # 无权限等业务级错误），也降级为 debug 且不再重试。
                if self._is_benign_error(str(e)):
                    logger.debug("%s 下载良性错误（不重试，降级）: %s", self.cli_path, e)
                    raise
                last_error = e
                if attempt < retries:
                    wait = min(2 ** attempt, MAX_BACKOFF_SECONDS)
                    logger.warning("%s 下载第 %d 次失败（可重试）: %s，%d秒后重试",
                                   self.cli_path, attempt + 1, e, wait)
                    time.sleep(wait)
                else:
                    raise
            except self._shutdown_error_class():
                # 关机阶段子进程被信号杀掉：已按 debug 记过，原样抛出
                raise
            except self._base_error_class() as e:
                err_str = str(e)
                if self._is_benign_error(err_str):
                    logger.debug("%s 下载业务错误（降级）: %s", self.cli_path, e)
                else:
                    logger.error("%s 下载未知错误: %s", self.cli_path, e)
                raise
        raise last_error  # type: ignore
