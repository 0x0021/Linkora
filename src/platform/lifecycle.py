from __future__ import annotations

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging

logger = logging.getLogger(__name__)


class LifecycleMixin:
    def shutdown(self, timeout: float = 10.0) -> None:
        """优雅关闭（P0-1 修复）。

        原 handle_signal 仅置标志 + 停轮询，导致：
          - 已排程的防抖 Timer（threading.Timer）退出后仍触发 LLM/发消息；
          - 后台 daemon 线程（记忆清理/对话摘要）未被 join，finally 中的
            store.close() 可能与其并发写库（SQLite 损坏风险）。
        本方法统一负责：停止一切新工作 + 干净退出后台线程，再由调用方 close store。
        """
        logger.info("开始优雅关闭...")
        self._running = False
        self._shutdown_event.set()  # 立即唤醒所有等待中的调度循环

        # 1. 停止轮询，不再拉取新消息（【Phase 3 多平台】遍历所有平台停止各自轮询器）
        for pid, ctx in self.platforms.items():
            if ctx.poller is None:
                continue
            try:
                ctx.poller.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("停止平台 %s 轮询器时出错（可忽略）: %s", pid, e)

        # 2. 取消所有挂起的防抖定时器，避免退出后触发 LLM/发消息
        with self._timer_lock:
            pending = list(self._pending_timers.values())
            self._pending_timers.clear()
            self._pending_first_seen.clear()
            self._pending_incomplete_wait.clear()
        for t in pending:
            t.cancel()

        # 3. 停止并 join 后台调度器（各自内部 join(timeout=5)）
        try:
            self.doc_sync_scheduler.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("停止文档同步调度器出错（可忽略）: %s", e)
        if self.db_backup:
            try:
                self.db_backup.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("停止数据库备份出错（可忽略）: %s", e)
        # 3b. 停止各平台 H2-A 后台异步摘要调度器（每平台独立 daemon 线程，stop 内部 join）
        for pid, ctx in self.platforms.items():
            if ctx.summary_scheduler is None:
                continue
            try:
                ctx.summary_scheduler.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("停止平台 %s 摘要调度器出错（可忽略）: %s", pid, e)

        # 4. join 内存/摘要守护线程（已由 _shutdown_event 唤醒，应迅速退出）
        for th in self._bg_threads:
            if th.is_alive():
                th.join(timeout=timeout)

        # 5. 关闭记忆提取线程池
        try:
            self._memory_executor.shutdown(wait=True, cancel_futures=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭记忆提取线程池出错（可忽略）: %s", e)

        # 6. 停止开发态模块热重载
        if getattr(self, "_module_reloader", None) is not None:
            try:
                self._module_reloader.stop_watcher()
            except Exception as e:  # noqa: BLE001
                logger.warning("停止模块热重载出错（可忽略）: %s", e)

        logger.info("后台线程已停止，可安全关闭存储")

    def run(self, web_port: int = 0, mode: str = "both") -> None:
        """启动主引擎。

        mode 控制进程职责（A1 进程分离）：
        - "both"  （默认，向后兼容）：同进程内既跑 Web 又跑后台轮询/调度。
        - "web"   ：仅启动 Web 管理平台（不拉消息、不跑调度），可独立重启而不打断 ingestion。
        - "worker"：仅跑后台轮询器 + 调度器（不启 Web），与 web 进程共享 SQLite(WAL)。
        web/worker 两进程可由 scripts/run_linkora.py 拉起，实现「改 Web 代码只重启 web 进程」。
        """
        self._running = True
        self._shutdown_event.clear()
        # 职责门控：web/worker 两模式互斥启动对应部分，both 两者都启
        start_web = mode in ("both", "web") and web_port > 0
        start_ingestion = mode in ("both", "worker")

        def handle_signal(signum, frame):
            logger.info("收到信号 %s，正在关闭...", signum)
            self._running = False
            self._shutdown_event.set()
            # 【Phase 3 多平台】停止所有平台的轮询器
            for pid, ctx in self.platforms.items():
                if ctx.poller is not None:
                    try:
                        ctx.poller.stop()
                    except Exception:  # noqa: BLE001
                        logger.warning("[resilience] silent exception in handle_signal", exc_info=True)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        web_thread = None
        if start_web:
            try:
                from web.api import run_web
                web_host = self.config.web.host
                web_thread = threading.Thread(
                    target=run_web, args=(web_port, web_host), daemon=True
                )
                web_thread.start()
                logger.info("[%s] Web管理平台已启动: http://%s:%d", mode, web_host, web_port)
            except Exception as e:
                logger.error("网页服务器启动失败: %s", e)
        if not start_web and not start_ingestion:
            logger.warning("无效模式 %s：未启动 Web 也未启动后台，进程将空转", mode)

        logger.info("=" * 50)
        logger.info("灵桥(Linkora)已启动 [模式=%s]", mode)
        logger.info("模型: %s", "DRY-RUN" if self.config.dws.dry_run else "LIVE")
        logger.info("LLM: %s @ %s", self.config.llm.model, self.config.llm.base_url)
        logger.info("工具: %s", ", ".join(self.config.tools.available))
        logger.info("工具路由策略: %s", getattr(self.config.tools, "tool_routing_mode", "smart"))
        logger.info("轮询间隔: %ds", self.config.poller.interval_seconds)
        logger.info("数据库备份: 每%d小时，保留%d个备份", getattr(self.config.storage, 'backup_interval_hours', 6), getattr(self.config.storage, 'backup_max_count', 7))
        if start_web:
            logger.info("Web管理平台: http://%s:%d", self.config.web.host, web_port)
        logger.info("按Ctrl+C停止")
        logger.info("=" * 50)

        # === 后台 ingestion（调度器 + 轮询器）仅 worker/both 启动 ===
        if start_ingestion:
            self.doc_sync_scheduler.start()
            self._start_backup_scheduler()

            # 启动记忆清理调度器
            memory_cleanup_thread = self._start_memory_cleanup_scheduler()
            memory_cleanup_thread.start()
            self._bg_threads.append(memory_cleanup_thread)
            logger.info("记忆清理调度器已启动（每7天执行一次）")

            # 启动对话摘要调度器
            conversation_summary_thread = self._start_conversation_summary_scheduler()
            conversation_summary_thread.start()
            self._bg_threads.append(conversation_summary_thread)
            logger.info("对话摘要调度器已启动（每24小时执行一次）")

            # 启动决策记录清理调度器
            decision_cleanup_thread = self._start_decision_cleanup_scheduler()
            decision_cleanup_thread.start()
            self._bg_threads.append(decision_cleanup_thread)
            logger.info("决策记录清理调度器已启动（每24小时执行一次，保留%d天）",
                        self.config.storage.decisions_retention_days)

            # 启动消息记录清理调度器
            messages_cleanup_thread = self._start_messages_cleanup_scheduler()
            messages_cleanup_thread.start()
            self._bg_threads.append(messages_cleanup_thread)
            logger.info("消息记录清理调度器已启动（每24小时执行一次）")

        try:
            if start_ingestion:
                # 【Phase 3 多平台】每个启用的平台在独立线程运行各自的轮询器，按平台上下文
                # 隔离 store/dws/llm_agent。主线程等待关闭信号（signal 或 shutdown_event）。
                poller_threads: list[threading.Thread] = []
                for pid, ctx in self.platforms.items():
                    if not ctx.enabled or ctx.poller is None:
                        continue
                    th = threading.Thread(
                        target=ctx.poller.run_loop,
                        args=(self._make_platform_callback(pid),),
                        name=f"poller-{pid}",
                        daemon=True,
                    )
                    th.start()
                    poller_threads.append(th)
                    logger.info("[多平台] 已启动 %s(%s) 轮询器线程", pid, ctx.display_name)

                if not poller_threads:
                    logger.warning("没有启用任何平台的轮询器，bot 将仅响应 Web 手动操作（重放等）")

            # web-only 模式下仍要响应关闭信号，等待退出
            while self._running:
                if self._shutdown_event.wait(1):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
            # 关闭所有平台的存储
            for ctx in self.platforms.values():
                try:
                    if ctx.store is not None:
                        ctx.store.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("关闭平台 %s 存储出错（可忽略）: %s", getattr(ctx, "id", "?"), e)
            logger.info("灵桥(Linkora)已停止 [模式=%s]", mode)

def _start_dev_watcher(pid_file: str) -> None:
    """启动文件变更监听线程，检测到代码文件变更时自动重启进程。

    --dev 模式下使用：监视 src/、web/、config.yaml、main.py 等核心文件，
    按 mtime 轮询，变更后移除 pid 文件并 os.execv 热重启。
    """
    watch_paths = [
        "src", "web", "config.yaml", "main.py",
    ]

    def _signature() -> float:
        m = 0.0
        for p in watch_paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    dirs[:] = [d for d in dirs
                               if d not in ("__pycache__", ".git", "node_modules", "data", "logs", ".trash")]
                    for f in files:
                        ext = os.path.splitext(f)[1]
                        if ext in (".py", ".yaml", ".yml", ".js", ".css", ".html"):
                            try:
                                mt = os.path.getmtime(os.path.join(root, f))
                                if mt > m:
                                    m = mt
                            except OSError as e:
                                logger.debug("[DEV] 文件 mtime 检查失败: %s", e)
            elif os.path.isfile(p):
                try:
                    mt = os.path.getmtime(p)
                    if mt > m:
                        m = mt
                except OSError as e:
                    logger.debug("[DEV] 文件 mtime 检查失败: %s", e)
        return m

    last = _signature()

    def _loop():
        nonlocal last
        while True:
            time.sleep(1.5)
            try:
                cur = _signature()
            except Exception:
                logger.warning("[resilience] silent exception in _loop", exc_info=True)
                continue
            if cur != last:
                # 【合抖】检测到变更后等 3s 确认无后续变更再重启，
                # 避免批量保存 / git checkout 触发连续多次重启
                time.sleep(3)
                try:
                    cur2 = _signature()
                except Exception:
                    logger.warning("[resilience] silent exception in _loop", exc_info=True)
                    cur2 = cur
                if cur2 != last:
                    last = cur2  # 3s 内又有新变更，再等下一轮
                    continue
                last = cur2 or cur
                logger.info("[DEV] 检测到文件变更，热重启进程...")
                try:
                    if pid_file and os.path.exists(pid_file):
                        os.remove(pid_file)
                except OSError as e:
                    logger.debug("[DEV] 清理旧 PID 文件失败: %s", e)
                os.execv(sys.executable, [sys.executable] + sys.argv)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info("[DEV] 文件监听已启动，监视: %s", ", ".join(watch_paths))

def main(root: str | None = None):
    # 延迟导入：LinkoraEngine 定义在 core.py，core 继承本 mixin，模块级导入会循环依赖
    from .core import LinkoraEngine
    from src.paths import (
        get_config_path,
        get_pid_file,
        ensure_runtime_dirs,
        set_data_dir,
        set_config_path,
    )

    # === 单例锁：防止多实例同时运行导致重复回复 ===
    # PID 文件锚定到项目根 data/，与配置中 ./data/*.db 的约定一致。
    # refactor 后 __file__ 变为 src/platform/lifecycle.py，不能再基于 __file__ 推导，
    # 否则 PID 会写到 src/platform/data/ 造成与 DB 路径（基于 cwd 根）不一致。
    if root is None:
        # 兜底：从本文件推导仓库根（src/platform/lifecycle.py -> parents[2]）
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    data_dir_override = None
    config_override = None
    web_port = 0
    test_rule_text = None
    dev_mode = False
    mode = "both"

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--web":
            web_port = int(sys.argv[i+1]) if i+1 < len(sys.argv) else 8000
            i += 2
        elif sys.argv[i].startswith("--web="):
            web_port = int(sys.argv[i].split("=")[1])
            i += 1
        elif sys.argv[i] == "--mode":
            mode = sys.argv[i+1] if i+1 < len(sys.argv) else "both"
            i += 2
        elif sys.argv[i].startswith("--mode="):
            mode = sys.argv[i].split("=", 1)[1]
            i += 1
        elif sys.argv[i] == "--test-rule":
            if i + 1 < len(sys.argv):
                test_rule_text = sys.argv[i + 1]
                i += 2
            else:
                print("错误: --test-rule 需要提供测试文本")
                sys.exit(1)
        elif sys.argv[i].startswith("--test-rule="):
            test_rule_text = sys.argv[i].split("=", 1)[1]
            i += 1
        elif sys.argv[i] == "--dev":
            dev_mode = True
            i += 1
        elif sys.argv[i] == "--data-dir":
            data_dir_override = sys.argv[i+1] if i+1 < len(sys.argv) else None
            i += 2
        elif sys.argv[i].startswith("--data-dir="):
            data_dir_override = sys.argv[i].split("=", 1)[1]
            i += 1
        elif sys.argv[i] == "--config":
            config_override = sys.argv[i+1] if i+1 < len(sys.argv) else None
            i += 2
        elif sys.argv[i].startswith("--config="):
            config_override = sys.argv[i].split("=", 1)[1]
            i += 1
        else:
            # 位置参数：config.yaml 路径
            config_override = sys.argv[i]
            i += 1

    # 校验 mode（A1 进程分离）
    if mode not in ("both", "web", "worker"):
        print(f"错误: --mode 取值必须为 both/web/worker，收到 {mode!r}")
        sys.exit(1)

    # —— 路径可重定位（P0）：注入 --data-dir / --config 覆盖，确保可写目录已建 ——
    if data_dir_override:
        set_data_dir(data_dir_override)
    if config_override:
        set_config_path(config_override)
    ensure_runtime_dirs()
    config_path = str(get_config_path())

    # === 单例锁：防止多实例同时运行导致重复回复 ===
    # PID 文件锚定到数据目录（可重定位）。A1 进程分离后按模式分文件：
    #   - both   -> <data>/linkora.pid        （向后兼容，test_platform_lifecycle 断言此文件名）
    #   - web    -> <data>/linkora.web.pid
    #   - worker -> <data>/linkora.worker.pid
    # 三者独立，web 重启不影响 worker（反之亦然），实现「改 Web 代码只重启 web 进程」。
    _PID_SUFFIX = "" if mode == "both" else f".{mode}"
    _PID_FILE = str(get_pid_file(mode))
    os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # 检测进程是否存在
            print(f"⚠️  发现已有实例运行中 (PID {old_pid}, 模式={mode})，请先杀掉旧进程:")
            print(f"   kill {old_pid}")
            sys.exit(1)
        except (ValueError, ProcessLookupError):
            logger.debug("[启动] 旧 PID 文件残留，旧进程已不存在，清理后继续")
        except OSError as e:
            # 跨平台兼容：Windows 上 os.kill(pid, 0) 对不存在/外来的 PID
            # 会抛裸 OSError(WinError 87, 参数错误) 而非 ProcessLookupError，
            # 同样视为残留清理后继续；确属无权限(EPERM/WinError 5)才告警退出。
            if isinstance(e, PermissionError):
                print(f"⚠️  PID 文件存在但无权限检测进程 {_PID_FILE}")
                sys.exit(1)
            logger.debug("[启动] 旧 PID 文件残留（os.kill 抛 OSError），清理后继续")
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        config = load_config(config_path)
    except (ValidationError, ValueError) as e:
        # fail-closed：不安全配置（如开启认证但密码为空）拒绝启动
        print(f"[FATAL] 配置校验失败，进程拒绝启动（安全默认）：{e}", file=sys.stderr)
        sys.exit(1)

    if web_port == 0:
        web_port = config.web.port

    # 规则测试模式：不启动服务，仅测试规则命中情况
    if test_rule_text is not None:
        from src.models import Message
        from datetime import datetime

        app = LinkoraEngine(config_path)
        test_msg = Message(
            msg_id="test-msg-id",
            chat_id="test-chat",
            chat_type="single",
            chat_name="测试用户",
            sender_id="test-sender",
            sender_name="测试用户",
            content=test_rule_text,
            msg_type="text",
            timestamp=datetime.now(),
            raw={},
        )
        result = app.rule_engine.check(test_msg)
        print(f"\n{'='*60}")
        print("规则测试结果")
        print(f"{'='*60}")
        print(f"输入文本: {test_rule_text}")
        print(f"匹配动作: {result.action}")
        print(f"匹配原因: {result.reason}")
        if result.reply_text:
            print(f"预设回复: {result.reply_text}")
        if result.rule_id:
            print(f"规则ID: {result.rule_id}")
        if result.match_type:
            print(f"匹配类型: {result.match_type}")
        print(f"{'='*60}\n")
        return

    app = LinkoraEngine(config_path)

    # 启动 CLI 版本自检 + 后台异步更新（lark-cli / dws / 企微 CLI）。
    # 在 daemon 线程执行，不阻塞主流程；任何异常仅记日志，不影响启动。
    try:
        from src.utils.cli_version_checker import start_cli_version_check
        start_cli_version_check()
    except Exception:  # noqa: BLE001
        logger.debug("[启动] CLI 版本自检启动失败（可忽略）", exc_info=True)

    if dev_mode:
        _start_dev_watcher(_PID_FILE)
    app.run(web_port, mode)
