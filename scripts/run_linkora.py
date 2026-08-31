#!/usr/bin/env python3
"""Linkora 多进程启动器（A1 进程分离）。

用法:
    scripts/run_linkora.py                      # 同时拉起 web(默认 8080) + worker
    scripts/run_linkora.py --web-port 9000      # 指定 Web 端口
    scripts/run_linkora.py --no-worker          # 只跑 web（调试 Web 时常用）
    scripts/run_linkora.py --worker-only        # 只跑 worker（纯 ingestion）
    scripts/run_linkora.py --dev                # dev 模式（文件变更热重启，both 模式）
    scripts/run_linkora.py --no-dedup           # 关闭跨进程日志去重，web/worker 逐行双显

进程职责:
    - web    : 仅 Web 管理平台。改 Web 代码后只重启本进程，不打断后台 ingestion。
    - worker : 仅后台轮询器 + 调度器（共享同一 SQLite/WAL，写入 data/*.db）。

两个子进程各自持独立 PID 锁（data/linkora.web.pid / data/linkora.worker.pid），
互不冲突；Ctrl+C / SIGTERM 时优雅转发给两者。

日志: 两个子进程共享同一终端，初始化阶段会各自打印一遍相同的启动序列，
原本看起来像"双份"。本启动器在转发时对「消息正文」做跨进程去重——同一行若
另一个进程已经打印过（忽略时间戳/ANSI 着色差异），则折叠为单条 [web+worker]，
避免重复刷屏；仅某一进程独有的行（如 web 监听端口、worker 调度器启动）保留各自
前缀。可用 --no-dedup 关闭折叠、恢复逐行双显（调试两进程差异时有用）。
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
MAIN = os.path.join(ROOT, "main.py")

# ANSI 着色：web=青，worker=黄，合并行=灰；便于肉眼区分两路输出
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GRAY = "\033[90m"
_RESET = "\033[0m"

# 上下文标签固定宽度：以最长标签 [web+worker]（含括号共 13 字符）为基准，
# 短标签右补空格，保证后续所有列（时间戳/级别/模块名/消息）纵向对齐。
_CTX_WIDTH = 13  # len("[web+worker]")

# 跨进程去重：web/worker 会各自打印一遍相同的初始化/调度序列。按「消息正文」去重
# （忽略 ANSI 着色、时间戳、[rid=xxxx] 差异）——同一正文若另一进程也打印，折叠为
# 【单条】[web+worker]，杜绝「[worker] + [web+worker]」双行；进程独有行保留各自前缀，
# 同进程重复行仍照常打印（保留频次可见性）。
# 实现：首条到达先缓冲一个极短窗口（_DEDUP_WINDOW），窗口内另一进程发来同正文 →
# 取消防冲、合并打印一次 [web+worker]；窗口内无伙伴 → 按原前缀打印一次。
_dedup_lock = threading.Lock()
_print_lock = threading.Lock()                       # 保护 stdout，避免多线程 print 交错
_monotonic = time.monotonic                         # 复用，避免重复调用属性查找
_seen_done: dict[str, set[str]] = {}                 # 已最终输出的正文 key → 已参与打印的进程集合
_last_emit: dict[str, float] = {}                    # 正文 key → 上次实际输出的 monotonic 时间
_pending: dict[str, tuple[threading.Timer, str]] = {}  # 等待伙伴中的 key → (定时器, 首到进程)
_DEDUP_WINDOW = 0.15                                 # 秒：等待伙伴的最大延迟
_DEDUP_RATE_WINDOW = 2.0                             # 秒：同正文短时重复（如单进程内重复初始化）也折叠，避免刷屏
# 正文归一化：去掉 ANSI 转义、行首时间戳(HH:MM:SS[.mmm])、[rid=xxxx]，仅比对实质内容
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TS_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?\s*")
_RID_RE = re.compile(r"\[rid=[^\]]*\]\s*")


def _normalize(body: str) -> str:
    s = _ANSI_RE.sub("", body)
    s = _RID_RE.sub("", s)
    s = _TS_RE.sub("", s)
    return s.strip()


def _emit(label: str, text: str) -> None:
    """按 label 选色输出一行（线程安全）。

    label=web+worker → 灰（合并行）；web → 青；worker → 黄。
    """
    color = _GRAY if label == "web+worker" else (_CYAN if label == "web" else _YELLOW)
    with _print_lock:
        print(f"{color}[{label:<{_CTX_WIDTH - 2}}]{_RESET} {text}", flush=True)


def _flush_single(key: str, prefix: str, text: str) -> None:
    """窗口超时仍无伙伴 → 视为单进程独有行，按原 prefix 打印一次。"""
    with _dedup_lock:
        _pending.pop(key, None)
        _seen_done[key] = {prefix}
        _last_emit[key] = _monotonic()
        if len(_seen_done) > 8000:        # 防内存无限增长，清空后旧行失去去重能力（可接受）
            _seen_done.clear()
            _last_emit.clear()
    _emit(prefix, text)


def _classify(prefix: str, text: str) -> None:
    """跨进程去重调度（无返回值，结果通过 _emit 输出）。

    - key 已最终输出过
        · 距上次 < _DEDUP_RATE_WINDOW（短时重复，如单进程内重复初始化）→ 抑制，避免双份
        · 距上次较久的真实重复 → 照常打印（两进程都见过则合并为 [web+worker]）
    - key 正在等待伙伴（另一进程先到）→ 取消防冲定时器，合并打印 [web+worker] 一次
    - key 首次出现                   → 缓冲并启动窗口定时器，等待伙伴
    """
    key = _normalize(text)
    if not key:
        _emit(prefix, text)               # 空/纯控制行：直接原样输出
        return
    now = _monotonic()
    with _dedup_lock:
        if key in _pending:
            timer, first_prefix = _pending.pop(key)
            timer.cancel()
            _seen_done[key] = {first_prefix, prefix}
            _last_emit[key] = now
            _emit("web+worker", text)
            return
        if key in _seen_done:
            if now - _last_emit.get(key, 0.0) < _DEDUP_RATE_WINDOW:
                return                      # 短时重复 → 抑制
            _seen_done[key].add(prefix)
            _last_emit[key] = now
            label = "web+worker" if len(_seen_done[key]) > 1 else prefix
            _emit(label, text)
            return
        timer = threading.Timer(_DEDUP_WINDOW, _flush_single, args=(key, prefix, text))
        timer.daemon = True
        _pending[key] = (timer, prefix)
        timer.start()


def _pump(prefix: str, color: str, stream, dedup: bool = True) -> None:
    """读取子进程管道（文本模式），加前缀后转发到父进程 stdout。

    dedup=True 时正文做跨进程去重（见 _classify，最终每行仅输出一次）；
    dedup=False 时逐行原样加前缀输出（调试两进程差异用 --no-dedup）。
    """
    try:
        for line in iter(stream.readline, ""):
            text = line.rstrip("\n")
            if not text:
                continue
            if dedup:
                _classify(prefix, text)
            else:
                with _print_lock:
                    print(f"{color}[{prefix:<{_CTX_WIDTH - 2}}]{_RESET} {text}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def spawn(mode: str, web_port: int, extra: list[str]) -> subprocess.Popen:
    cmd = [PY, MAIN, "--mode", mode]
    if web_port:
        cmd += ["--web", str(web_port)]
    cmd += extra
    print(f"{_YELLOW if mode == 'worker' else _CYAN}[run_linkora]{_RESET} "
          f"启动 {mode} 进程: {' '.join(cmd)}", flush=True)
    # 通过 PIPE 捕获子进程 stdout/stderr，由 _pump 加前缀后输出，
    # 避免 web/worker 两进程日志在终端混在一起难分辨。
    return subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Linkora 多进程启动器（A1 进程分离）")
    ap.add_argument("--web-port", type=int, default=8080, help="Web 进程监听端口（默认 8080）")
    ap.add_argument("--no-worker", action="store_true", help="只启动 web 进程")
    ap.add_argument("--worker-only", action="store_true", help="只启动 worker 进程")
    ap.add_argument("--dev", action="store_true", help="dev 模式（文件变更热重启，both 模式）")
    ap.add_argument("--no-dedup", action="store_true",
                    help="关闭跨进程日志去重，恢复 web/worker 逐行双显（调试两进程差异用）")
    args = ap.parse_args()

    extra = ["--dev"] if args.dev else []

    procs: list[tuple[str, subprocess.Popen]] = []
    pumps: list[threading.Thread] = []
    dedup = not args.no_dedup
    if not args.worker_only:
        p = spawn("web", args.web_port, extra)
        procs.append(("web", p))
        pumps.append(threading.Thread(target=_pump, args=("web", _CYAN, p.stdout, dedup), daemon=True))
    if not args.no_worker:
        p = spawn("worker", 0, extra)
        procs.append(("worker", p))
        pumps.append(threading.Thread(target=_pump, args=("worker", _YELLOW, p.stdout, dedup), daemon=True))

    for t in pumps:
        t.start()

    stop = {"v": False}

    def forward(signum, _frame):
        print(f"\n{_YELLOW}[run_linkora]{_RESET} 收到信号 {signum}，优雅关闭子进程...", flush=True)
        for _name, p in procs:
            if p.poll() is None:
                try:
                    p.send_signal(signum)
                except Exception:  # noqa: BLE001
                    pass
        stop["v"] = True

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)

    try:
        while not stop["v"]:
            time.sleep(0.5)
            dead = [(name, p) for name, p in procs if p.poll() is not None]
            if dead:
                for name, p in dead:
                    print(f"{_YELLOW}[run_linkora]{_RESET} {name} 进程已退出 (code={p.returncode})", flush=True)
                # 任一子进程意外退出 → 拉起其余一起退出，避免半死不活状态
                for _name, p in procs:
                    if p.poll() is None:
                        try:
                            p.send_signal(signal.SIGTERM)
                        except Exception:  # noqa: BLE001
                            pass
                break
    finally:
        for _name, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"{_YELLOW}[run_linkora]{_RESET} 子进程未在 10s 内退出，强制 kill", flush=True)
                p.kill()


if __name__ == "__main__":
    main()
