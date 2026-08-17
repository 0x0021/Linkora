"""DWS 个人事件长连接消费器（v1.0.59+ / Stream 实时推送）。

替代 ``MessagePoller`` 的 5s 轮询：通过 ``dws event +listen-im`` 建立钉钉个人
Stream 长连接，把收到的 IM 消息事件以 NDJSON 实时推送到 stdout，本模块读行解析后
回调 ``on_message``，延迟从 ~5s 降到 <1s，API 调用量降 90%+。

设计原则：
- 默认**不接入** poller 主循环（风险隔离），由调用方显式 ``start()``；
- 单一 monitor 线程串行管理各 kind 进程（避免失败时的重连风暴）；
- 优雅停：SIGTERM（dws 会取消本次新建的个人订阅）；超时降级到 SIGKILL 兜底；
- 自动重连：进程异常退出后按 kind 独立指数退避重连，带健康上限；
- 零浏览器弹窗：复用 ``_NO_BROWSER_ENV``。

⚠️ 个人消息事件 data payload 的字段命名存在多套变体，解析采用容错探测，
不硬编码单一命名；解析失败仅记 debug 并跳过，不影响其他事件。

⚠️ ``+listen-im`` 不支持 ``--flatten``（仅 ``event consume`` 支持），其 NDJSON 输出为
transport envelope（含 ``data`` JSON 字符串），``_parse_event`` 已对其做 fromjson。
"""
from __future__ import annotations

import json
import logging
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from src.dws_adapter.core import _NO_BROWSER_ENV

logger = logging.getLogger(__name__)

# listen-im kind → event_key（来自 dws event schema，仅作注释参考）
#   all-direct  → user_im_message_receive_o2o_all
#   all-group   → user_im_message_receive_group_all
#   at-me       → (仅 @我 消息，最省资源)
DEFAULT_KINDS = ["all-direct", "all-group"]
DEFAULT_EVENTS = ["message"]


class EventStreamConsumer:
    """常驻消费 ``dws event +listen-im`` 的 NDJSON 事件流。"""

    def __init__(self, *, cli_path: str = "dws",
                 kinds: list[str] | None = None,
                 events: list[str] | None = None,
                 profile: str = "",
                 on_message: Callable[[dict], None] | None = None,
                 on_status: Callable[[str, Any], None] | None = None,
                 max_backoff: float = 30.0,
                 health_timeout: float = 90.0,
                 stop_timeout: float = 10.0):
        """
        Args:
            cli_path: dws 可执行路径
            kinds: listen-im 监听意图列表，默认 [all-direct, all-group]（覆盖单聊+群，
                等效替代轮询）；也可只传 [at-me]（最省，仅 @我 触发）
            events: 事件种类，默认 [message]
            profile: 指定组织/账号（--profile），多账号隔离时必填
            on_message: 收到解析后消息 dict 的回调
            on_status: 状态回调 (status, payload)，status ∈ {ready, exited, error, reconnect}
            max_backoff: 重连最大退避秒数
            health_timeout: 启动后多久未收到 ready 视为失败
            stop_timeout: SIGTERM 后等待进程退出的秒数
        """
        self.cli_path = cli_path
        self.kinds = kinds or list(DEFAULT_KINDS)
        self.events = events or list(DEFAULT_EVENTS)
        self.profile = profile
        self.on_message = on_message
        self.on_status = on_status
        self.max_backoff = max_backoff
        self.health_timeout = health_timeout
        self.stop_timeout = stop_timeout

        self._running = False
        self._procs: dict[str, subprocess.Popen] = {}   # kind -> 当前进程
        self._backoffs: dict[str, float] = {k: 1.0 for k in self.kinds}
        self._next_retry_at: dict[str, float] = {k: 0.0 for k in self.kinds}
        self._monitor_thread: threading.Thread | None = None
        self._reader_threads: list[threading.Thread] = []
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._recv_count = 0
        self._last_event_at: float | None = None

    # === 公共控制 ===

    def start(self) -> None:
        """启动 monitor 线程（非阻塞），由 monitor 负责拉起各 kind 消费者。"""
        if self._running:
            logger.warning("[Stream] 已在运行，忽略重复 start")
            return
        self._running = True
        self._ready.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="dws-stream-monitor", daemon=True)
        self._monitor_thread.start()
        logger.info("[Stream] monitor 已启动，监听 kinds=%s", self.kinds)

    def stop(self) -> None:
        """优雅停止：置 _running=False → 终止所有进程 → 等待线程退出。"""
        if not self._running:
            return
        self._running = False
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for p in procs:
            self._terminate(p)
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self.stop_timeout + 2)
        for t in self._reader_threads:
            if t.is_alive():
                t.join(timeout=self.stop_timeout + 2)
        self._reader_threads.clear()
        logger.info("[Stream] 已停止（共接收 %d 条事件）", self._recv_count)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def received_count(self) -> int:
        return self._recv_count

    # === monitor：串行管理各 kind 进程 ===

    def _monitor_loop(self) -> None:
        while self._running:
            now = time.time()
            for kind in self.kinds:
                with self._lock:
                    proc = self._procs.get(kind)
                if proc is not None and proc.poll() is None:
                    continue  # 进程存活，跳过
                # 进程不存在或已退出：到退避时间则重启
                if now < self._next_retry_at.get(kind, 0.0):
                    continue
                self._spawn(kind)
                # 退避递增（下次重启更慢），成功常驻由 _on_proc_alive 在 ready 后重置
                self._backoffs[kind] = min(
                    self._backoffs[kind] * 2, self.max_backoff)
                self._next_retry_at[kind] = now + self._backoffs[kind]
            time.sleep(1.0)

    def _on_ready(self, kind: str) -> None:
        """某 kind 就绪：重置其退避，标记整体 ready。"""
        self._backoffs[kind] = 1.0
        self._next_retry_at[kind] = 0.0
        self._ready.set()
        self._emit_status("ready", {"kind": kind})

    # === 子进程管理 ===

    def _build_args(self, kind: str) -> list[str]:
        # +listen-im 不支持 --flatten（仅 event consume 支持）；其 NDJSON 为 transport
        # envelope（含 data JSON 字符串），_parse_event 已处理 data 的 fromjson。
        args = [
            self.cli_path, "event", "+listen-im",
            "--kind", kind,
            "--events", ",".join(self.events),
            "-f", "ndjson",
        ]
        if self.profile:
            args.extend(["--profile", self.profile])
        args.append("-y")
        return args

    def _spawn(self, kind: str) -> None:
        args = self._build_args(kind)
        logger.info("[Stream] 启动 consumer kind=%s: %s", kind, " ".join(args))
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_NO_BROWSER_ENV,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError) as e:
            logger.error("[Stream] 无法启动 dws（%s）：%s", self.cli_path, e)
            self._emit_status("error", {"kind": kind, "error": str(e)})
            return
        with self._lock:
            self._procs[kind] = proc
        t_err = threading.Thread(
            target=self._read_stderr, args=(proc, kind), daemon=True,
            name=f"dws-stream-err-{kind}")
        t_out = threading.Thread(
            target=self._read_stdout, args=(proc, kind), daemon=True,
            name=f"dws-stream-out-{kind}")
        t_err.start()
        t_out.start()
        self._reader_threads.extend([t_err, t_out])

    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)  # dws 会取消本次新建的个人订阅
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("[Stream] SIGTERM 超时，降级 SIGKILL（可能泄漏服务端订阅）")
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # === 读线程 ===

    def _read_stderr(self, proc: subprocess.Popen, kind: str) -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                if "[event] ready" in line:
                    self._on_ready(kind)
        except (ValueError, OSError):
            pass

    def _read_stdout(self, proc: subprocess.Popen, kind: str) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                msg = self._parse_event(line)
                if msg is None:
                    continue
                self._recv_count += 1
                self._last_event_at = time.time()
                if self.on_message:
                    try:
                        self.on_message(msg)
                    except Exception as e:  # 回调异常不应杀死读线程
                        logger.exception("[Stream] on_message 回调异常: %s", e)
        except (ValueError, OSError):
            pass
        # 进程 stdout 关闭：monitor 会按退避重启；此处不自行重连（避免风暴）
        if self._running:
            logger.warning("[Stream] kind=%s 进程退出，monitor 将重连", kind)

    # === 事件解析 ===

    def _parse_event(self, line: str) -> dict | None:
        """解析一行 NDJSON 为归一化消息 dict；无法解析返回 None。"""
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            return None
        data = env.get("data")
        if isinstance(data, str):
            try:
                inner = json.loads(data)
            except json.JSONDecodeError:
                inner = None
        elif isinstance(data, dict):
            inner = data
        else:
            inner = None
        if not isinstance(inner, dict):
            return None
        return self._extract_message(env, inner)

    @staticmethod
    def _extract_message(env: dict, inner: dict) -> dict | None:
        """从事件内层 payload 容错提取消息字段（兼容多套命名）。"""
        msg_id = (
            inner.get("msgId") or inner.get("messageId")
            or inner.get("openMessageId") or env.get("event_id")
        )
        if not msg_id:
            return None
        sender_open = (
            inner.get("senderOpenId") or inner.get("senderId")
            or inner.get("senderUserId")
        )
        conversation_id = (
            inner.get("conversationId") or inner.get("openConversationId")
        )
        text, msg_type = EventStreamConsumer._parse_content(inner.get("content"))
        ts_raw = (
            inner.get("createAt") or inner.get("timestamp")
            or env.get("event_born_time") or env.get("received_at_unix_ms")
        )
        try:
            ts = float(ts_raw) if ts_raw is not None else None
        except (TypeError, ValueError):
            ts = None
        return {
            "message_id": str(msg_id),
            "sender_open_dingtalk_id": sender_open,
            "conversation_id": conversation_id,
            "text": text,
            "msg_type": msg_type,
            "timestamp": ts,
            "event_type": env.get("event_type"),
            "raw": inner,
        }

    @staticmethod
    def _parse_content(content: Any) -> tuple[str, str]:
        """钉钉 content 可能是嵌套 JSON 字符串（{"msgtype":..,"text":{"content":..}}）或纯文本。"""
        if content is None:
            return "", "unknown"
        if isinstance(content, dict):
            cj = content
        elif isinstance(content, str):
            s = content.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    cj = json.loads(s)
                except json.JSONDecodeError:
                    return s, "text"
            else:
                return s, "text"
        else:
            return str(content), "text"
        msg_type = cj.get("msgtype", "text")
        text = (
            (cj.get("text") or {}).get("content")
            if isinstance(cj.get("text"), dict) else cj.get("text")
        ) or cj.get("content") or ""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        return text, msg_type

    # === 状态回调 ===

    def _emit_status(self, status: str, payload: Any) -> None:
        if self.on_status:
            try:
                self.on_status(status, payload)
            except Exception as e:  # noqa: BLE001
                logger.debug("[Stream] on_status 回调异常: %s", e)
