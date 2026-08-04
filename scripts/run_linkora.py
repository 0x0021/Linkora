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

# 跨进程去重：web/worker 启动期会各自打印一遍相同的初始化序列。按「消息正文」
# 去重（忽略 ANSI 着色、时间戳、request_id 差异）——同一行若另一进程已打印过，
# 则折叠为单条 [web+worker]，避免启动日志看起来像双份。进程独有行保留各自前缀。
_dedup_lock = threading.Lock()
_seen_by: dict[str, set[str]] = {}
_merged: set[str] = set()
# 正文归一化：去掉 ANSI 转义、行首时间戳(HH:MM:SS[.mmm])、[rid=xxxx]，仅比对实质内容
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TS_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?\s*")
_RID_RE = re.compile(r"\[rid=[^\]]*\]\s*")


def _normalize(body: str) -> str:
    s = _ANSI_RE.sub("", body)
    s = _RID_RE.sub("", s)
    s = _TS_RE.sub("", s)
    return s.strip()


def _classify(prefix: str, body: str) -> tuple[bool, str]:
    """返回 (是否打印, 实际前缀标签)。

    - 首次出现                  → (True, prefix)
    - 另一进程已打印过同正文     → 折叠为 'web+worker'，仅首次折叠时打印
    - 已折叠过的正文再次到达     → (False, '') 后续完全抑制
    同进程内重复行始终照常打印（不折叠）。
    """
    key = _normalize(body)
    if not key:
        return True, prefix
    with _dedup_lock:
        if key in _merged:
            return False, ""
        if key in _seen_by and prefix not in _seen_by[key]:
            _seen_by[key].add(prefix)
            _merged.add(key)
            return True, "web+worker"
        _seen_by.setdefault(key, set()).add(prefix)
        # 防止内存无限增长（极少触发）
        if len(_seen_by) > 4000:
            _seen_by.clear()
            _merged.clear()
        return True, prefix


def _pump(prefix: str, color: str, stream, dedup: bool = True) -> None:
    """读取子进程管道（文本模式），加前缀后转发到父进程 stdout。

    dedup=True 时对消息正文做跨进程去重，重复行折叠为 [web+worker]。
    """
    try:
        for line in iter(stream.readline, ""):
            text = line.rstrip("\n")
            if not text:
                continue
            if dedup:
                ok, label = _classify(prefix, text)
                if not ok:
                    continue
                if label == "web+worker":
                    print(f"{_GRAY}[{label:<{_CTX_WIDTH - 2}}]{_RESET} {text}", flush=True)
                    continue
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
