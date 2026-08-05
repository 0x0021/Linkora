"""手动同步历史消息：在进程内后台线程执行的 worker（替代旧独立子进程方案）。

为什么改成线程：
- 旧实现 ``scripts/sync_history_worker.py`` 通过 ``subprocess.Popen(..., start_new_session=True)``
  启动独立子进程。该方案在 PyInstaller 冻结态下完全不可用：
  (1) ``scripts/sync_history_worker.py`` 不会被 spec 打包（spec 只收 ``src/`` 与 ``web/``），
      ``__file__`` 推导的 worker_script 路径在冻结态落到 ``_MEIPASS`` 根、找不到脚本；
  (2) ``sys.executable`` 在冻结态是二进制本身、不是 python，无法 ``python script.py`` 跑 worker。
- 线程方案在 dev 态、frozen 态都通用：worker 代码就是项目内 importable 模块，无需单独打包；
  线程里创建独立的 ``SQLiteStore`` 连接（WAL + busy_timeout=5000 与主 poller 并发安全）；
  取消机制沿用 ``CANCEL_FILE``（线程每个时间窗前读取，无需精确 kill）。

权衡（相对旧子进程方案）：
- 失去「Web 重启不影响已启动的同步」特性 — 单用户场景下收益不抵复杂度，需要时再回退到子进程即可。

文件落点（已 P0 化）：
- 状态文件：``data/sync_history_status.json``（可重定位，``data_path``）
- worker 日志：``logs/sync_history_worker.log``（可重定位，``get_log_dir``）
- 取消信号：``data/sync_history_cancel.json``（可重定位，``data_path``）
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from typing import Callable, Optional

from src.paths import data_path, get_config_path, get_log_dir

# 状态文件：web/routers/sync.py 的 _read_sync_status 也读这个
STATUS_FILE = str(data_path("sync_history_status.json"))
# worker 日志文件：web/routers/sync.py 的 sync-history/log 端点读尾部
LOG_FILE = str(get_log_dir() / "sync_history_worker.log")
# 取消信号：cancel 端点写入，run_sync_history 每个时间窗前读取
CANCEL_FILE = str(data_path("sync_history_cancel.json"))

# 模块 logger；configure_logging() 配好 handler 后，INFO 落到 LOG_FILE
logger = logging.getLogger("linkora.sync_history")


# ---------- 共享工具（被 web/routers/sync.py 与 CLI 入口共用）----------

def cancel_requested() -> bool:
    """取消信号文件是否存在。线程 worker 每个时间窗前调用一次。"""
    try:
        return os.path.exists(CANCEL_FILE)
    except Exception:
        logger.warning("检查取消信号文件失败: %s", CANCEL_FILE)
        return False


def write_status(d: dict) -> None:
    """把状态写盘。线程模式下不写 pid（避免 cancel 端点误杀 Web 进程），
    仅在显式传入 pid 时写入（保留给未来回退到子进程模式时使用）。"""
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        # 状态文件写失败不应影响主流程，但需记录以便排查
        logger.warning("写入同步状态文件失败: %s", STATUS_FILE)


def configure_logging() -> None:
    """把 INFO 级别日志重定向到 LOG_FILE（终端看不到，前端用 /sync-history/log 读尾部）。

    注意：handler 重复加会重复输出，先清掉本 logger 上已有的 handler 再挂 file handler。
    """
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(fh)
    except Exception:
        # 配日志失败不至于影响主流程，但需记录
        logger.warning("配置同步日志文件失败: %s", LOG_FILE)


def make_progress_cb(
    job_id: str, platform: str, days: int, scope: str, range_label: str,
) -> Callable[[int, int, int, int], None]:
    """返回 progress_cb(window_index, total_windows, saved, fetched)。
    每个时间窗处理完后更新状态文件，让前端显示分窗进度（不覆盖 job_id 等元信息）。
    同一窗去重，避免成功/失败分支重复写盘。
    """
    seen: dict[int, bool] = {}

    def cb(window_index: int, total_windows: int, saved: int, fetched: int) -> None:
        done = window_index + 1
        if seen.get(done):
            return
        seen[done] = True
        pct = int(done / max(1, total_windows) * 100)
        logger.info(
            "[进度] 第 %d/%d 窗完成，已存 %d 条（共拉取 %d，%d%%）",
            done, total_windows, saved, fetched, pct,
        )
        write_status({
            "job_id": job_id,
            "status": "running",
            "platform": platform,
            "days": days,
            "scope": scope,
            "range": range_label,
            "progress": f"第 {done}/{total_windows} 窗，已存 {saved} 条（共拉取 {fetched}）",
            "windows_done": done,
            "windows_total": total_windows,
            "percent": pct,
            "result": None,
            "error": None,
        })

    return cb


def resolve_current_user(dws, config) -> tuple[dict, str]:
    """复刻 primary._load_current_user + _resolve_own_open_dingtalk_id 的精简版。

    Returns:
        (user_info, open_dingtalk_id)
    """
    base: dict = {"userId": "", "userName": "个人用户", "orgName": "", "dept": "", "title": ""}
    try:
        profile = dws._get_current_profile_local()
        if profile and profile.get("userId"):
            base = {
                "userId": profile.get("userId", ""),
                "userName": profile.get("userName", ""),
                "orgName": profile.get("corpName", ""),
                "dept": "",
                "title": "",
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("读取本地 profile 失败（忽略）: %s", e)

    open_dingtalk_id = ""
    try:
        info = dws.contact_user_get_self(timeout=30)
        open_dingtalk_id = info.get("openDingTalkId", "") or ""
        emp = info.get("orgEmployeeModel", {})
        if emp:
            depts = emp.get("depts") or []
            dept = (depts[0] if depts else {}).get("deptName", "")
            title = (
                emp.get("title") or emp.get("position")
                or emp.get("jobTitle") or emp.get("jobName") or ""
            )
            if not base.get("dept"):
                base["dept"] = dept
            if not base.get("title"):
                base["title"] = title
    except Exception as e:  # noqa: BLE001
        logger.warning("补拉企业用户信息失败（忽略）: %s", e)

    return base, open_dingtalk_id


# ---------- 主入口 ----------

def run_sync_history(
    *,
    days: int = 7,
    platform: str = "dingtalk",
    job_id: str = "",
    scope: str = "global",
    range_label: str = "",
    full: bool = False,
    conversation_id: str = "",
    chat_types: Optional[list[str]] = None,
    config_path: Optional[str] = None,
) -> int:
    """线程 worker 主体：bootstrap dws / store / poller，调 poller.sync_history()，写状态/日志。

    Args:
        config_path: 默认 None → 用 ``get_config_path()``（可重定位，dev/frozen 都对）。
        其它字段与 ``web/routers/sync.py`` 的 ``SyncHistoryRequest`` 一一对应。

    Returns:
        0 = 成功 / cancelled；1 = 异常。
    """
    # 延迟 import 避免 web 进程启动时无谓加载（dws / sqlite / poller 都重）
    from src.config import load_config
    from src.dws_adapter import DwsAdapter
    from src.memory.sqlite_store import SQLiteStore
    from src.memory.platform_context import with_platform
    from src.poller import MessagePoller
    from src.poller_core_history import _SyncCancelled
    from src.rule_engine import RuleEngine

    job_id = job_id or f"sync_{int(time.time() * 1000)}"
    conv_id = (conversation_id or "").strip()
    chat_types = chat_types or None
    if not range_label:
        range_label = "全部历史" if full else f"最近{days}天"

    # 启动即清掉遗留的取消标记（上一次可能被取消但文件没清干净）
    # 清理失败不阻断同步，但必须留下痕迹：残留的取消文件会让本次同步在第一个时间窗就被误取消
    try:
        if os.path.exists(CANCEL_FILE):
            os.remove(CANCEL_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[sync-worker] 启动时清理遗留取消标记失败（继续执行，本次同步可能被误取消） "
            "job_id=%s platform=%s file=%s error=%s: %s",
            job_id, platform, CANCEL_FILE, type(e).__name__, e,
        )

    configure_logging()
    logger.info(
        "[sync-worker] 启动 job_id=%s platform=%s scope=%s range=%s chat_types=%s conv_id=%s",
        job_id, platform, scope, range_label,
        ",".join(chat_types) if chat_types else "全部", conv_id or "-",
    )

    write_status({
        "job_id": job_id, "status": "starting", "platform": platform,
        "days": days, "scope": scope, "range": range_label,
        "progress": "初始化中", "result": None,
        "error": None, "started_at": time.time(),
    })

    # 默认用可重定位的 config 路径（dev/frozen 都对）；调用方可显式覆盖
    cfg_path = config_path or str(get_config_path())

    try:
        config = load_config(cfg_path)

        dws = DwsAdapter(
            cli_path=config.dws.cli_path,
            timeout=config.dws.timeout,
            retries=config.dws.retries,
            dry_run=config.dws.dry_run,
            profile=config.dws.profile,
            ai_tag_default=config.poller.ai_tag_enabled,
        )

        store = SQLiteStore(os.path.abspath(config.storage.path))
        store.init_db()

        user_info, open_dingtalk_id = resolve_current_user(dws, config)
        current_user_id = open_dingtalk_id or user_info.get("userId", "")
        rule_engine = RuleEngine(config.rules, db_store=store)

        poller = MessagePoller(
            config=config.poller,
            dws=dws,
            store=store,
            current_user_id=current_user_id,
            current_user_name=user_info.get("userName", ""),
            current_user_user_id=user_info.get("userId", ""),
            rule_engine=rule_engine,
            platform_id=platform,
            # 历史回填优化：不预载全量已处理 ID、跳过图片 OCR/下载管线
            load_processed_ids=False,
            skip_ocr=True,
        )

        progress_cb = make_progress_cb(job_id, platform, days, scope, range_label)

        write_status({
            "job_id": job_id, "status": "running", "platform": platform,
            "days": days, "scope": scope, "range": range_label,
            "progress": "正在拉取历史消息", "result": None,
            "error": None, "started_at": time.time(),
        })

        with with_platform(platform):
            stats = poller.sync_history(
                days_back=days,
                conversation_id=conv_id or None,
                full=full,
                chat_types=chat_types,
                progress_cb=progress_cb,
                cancel_check=cancel_requested,
            )

        write_status({
            "job_id": job_id, "status": "done", "platform": platform,
            "days": days, "scope": scope, "range": range_label,
            "progress": "完成", "result": stats,
            "windows_total": stats.get("windows", 0),
            "percent": 100,
            "error": None, "finished_at": time.time(),
        })
        logger.info(
            "[sync-worker] 完成 job_id=%s 模式=%s 拉取=%d 新存=%d 去重=%d 修复=%d 窗=%d",
            job_id, stats.get("mode"), stats.get("total", 0), stats.get("saved", 0),
            stats.get("skipped_dup", 0), stats.get("fixed_direction", 0), stats.get("windows", 0),
        )
        try:
            if os.path.exists(CANCEL_FILE):
                os.remove(CANCEL_FILE)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[sync-worker] 同步完成后清理取消标记失败（不影响本次结果，可能影响下次同步） "
                "job_id=%s platform=%s file=%s error=%s: %s",
                job_id, platform, CANCEL_FILE, type(e).__name__, e,
            )
        return 0
    except _SyncCancelled as e:  # noqa: BLE001
        write_status({
            "job_id": job_id, "status": "cancelled", "platform": platform,
            "days": days, "scope": scope, "range": range_label,
            "progress": "已取消", "result": None,
            "error": None, "finished_at": time.time(),
        })
        logger.info("[sync-worker] 同步已取消 job_id=%s: %s", job_id, e)
        try:
            if os.path.exists(CANCEL_FILE):
                os.remove(CANCEL_FILE)
        except Exception as cleanup_err:  # noqa: BLE001
            logger.warning(
                "[sync-worker] 取消后清理取消标记失败（残留文件可能导致下次同步被误取消） "
                "job_id=%s platform=%s file=%s error=%s: %s",
                job_id, platform, CANCEL_FILE, type(cleanup_err).__name__, cleanup_err,
            )
        return 0
    except Exception as e:  # noqa: BLE001
        write_status({
            "job_id": job_id, "status": "error", "platform": platform,
            "days": days, "scope": scope, "range": range_label,
            "progress": "", "result": None,
            "error": str(e), "traceback": traceback.format_exc(),
            "finished_at": time.time(),
        })
        logger.error("[sync-worker] 同步失败 job_id=%s: %s", job_id, e)
        logger.error(traceback.format_exc())
        try:
            if os.path.exists(CANCEL_FILE):
                os.remove(CANCEL_FILE)
        except Exception as cleanup_err:  # noqa: BLE001
            logger.warning(
                "[sync-worker] 同步失败后清理取消标记失败（残留文件可能导致下次同步被误取消） "
                "job_id=%s platform=%s file=%s error=%s: %s",
                job_id, platform, CANCEL_FILE, type(cleanup_err).__name__, cleanup_err,
            )
        return 1
