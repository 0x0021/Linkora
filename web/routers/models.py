"""本地模型状态路由：重排(rerank) + 嵌入(embedding) 模型的实时状态与资源占用。

前端「模型状态」页（js/pages/models.js）轮询此端点，以卡片 + 仪表形式展示：
- embedding：模型名 / 提供方 / 离线模式 / 加载状态（含下载进度）
- rerank：配置开关 / 模型名 / 是否已 lazy-load 进内存 / 运行设备
- system：CPU / 内存 / 当前进程资源 / GPU（NVIDIA via pynvml、CUDA via torch、Apple MPS 优雅降级）

所有采集均为 best-effort：任一库缺失或异常都降级为 ``available: False``，绝不抛出异常阻断主链路。
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from web.dependencies import get_app_instance, logger

router = APIRouter()

# rerank 配置缓存（config 改动需重启才生效，60s 刷新足够）
_RERANK_CFG_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _get_rerank_cfg():
    """读取 llm.advanced 中的 rerank 配置（带 60s 缓存）。

    不能依赖 app_instance 内部结构，直接走 load_config 读 config.yaml，
    与 src/llm/agent.py 读取 ``config.llm.advanced.rerank_*`` 路径一致。
    """
    now = time.time()
    if _RERANK_CFG_CACHE["data"] is None or now - _RERANK_CFG_CACHE["ts"] > 60:
        adv = None
        try:
            from src.config import load_config
            from src.paths import get_config_path

            cfg = load_config(str(get_config_path()))
            llm = getattr(cfg, "llm", None)
            adv = getattr(llm, "advanced", None) if llm is not None else None
        except Exception as e:  # noqa: BLE001 - 配置缺失时降级为默认值
            logger.debug("读取 rerank 配置失败，使用默认值: %s", e)
            adv = None
        _RERANK_CFG_CACHE["data"] = adv
        _RERANK_CFG_CACHE["ts"] = now
    return _RERANK_CFG_CACHE["data"]


def _collect_embedding() -> dict[str, Any]:
    """嵌入模型状态（复用 /api/embedding-status 的取数逻辑）。"""
    try:
        app_instance = get_app_instance()
        client = getattr(app_instance, "embedding_client", None) if app_instance else None
        if client is None:
            return {
                "available": False,
                "enabled": False,
                "model": None,
                "provider": None,
                "offline": None,
                "state": "unknown",
                "progress": 0.0,
                "downloaded": 0,
                "total": 0,
                "message": "嵌入客户端未初始化",
            }
        status = client.get_load_status()
        status["available"] = True
        status["enabled"] = client.enabled
        status["model"] = getattr(client.config, "model", None)
        status["provider"] = getattr(client.config, "provider", None)
        status["offline"] = bool(getattr(client.config, "offline", False))
        return status
    except Exception as e:  # noqa: BLE001
        logger.debug("收集 embedding 状态失败: %s", e)
        return {
            "available": False,
            "enabled": False,
            "model": None,
            "provider": None,
            "offline": None,
            "state": "error",
            "progress": 0.0,
            "downloaded": 0,
            "total": 0,
            "message": str(e),
        }


def _collect_rerank() -> dict[str, Any]:
    """重排模型状态：配置开关 + 是否已加载进内存 + 运行设备。"""
    try:
        from src.llm.rerank import _reranker_state

        adv = _get_rerank_cfg()
        enabled = bool(getattr(adv, "rerank_enabled", False)) if adv is not None else False
        model = getattr(adv, "rerank_model", "BAAI/bge-reranker-base") if adv is not None else "BAAI/bge-reranker-base"
        offline = bool(getattr(adv, "rerank_offline", False)) if adv is not None else False

        reranker = _reranker_state.reranker
        loaded = reranker is not None

        device = None
        if loaded:
            try:
                m = getattr(reranker, "model", None)
                if m is not None:
                    device = str(next(m.parameters()).device)
            except Exception:  # noqa: BLE001 - 设备探测失败不阻塞
                device = None

        if not enabled:
            state = "disabled"
        elif loaded:
            state = "loaded"
        else:
            state = "idle"  # 已开启但未触发首次加载（lazy-load）

        return {
            "available": True,
            "enabled": enabled,
            "model": model,
            "offline": offline,
            "loaded": loaded,
            "state": state,
            "device": device,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("收集 rerank 状态失败: %s", e)
        return {
            "available": False,
            "enabled": False,
            "model": None,
            "offline": None,
            "loaded": False,
            "state": "error",
            "device": None,
            "message": str(e),
        }


def _collect_process() -> dict[str, Any]:
    """当前 web 进程的资源占用（托管的模型与该进程同生命周期）。"""
    try:
        import psutil

        p = psutil.Process(os.getpid())
        with p.oneshot():
            return {
                "pid": p.pid,
                "name": p.name(),
                "cpu_percent": round(p.cpu_percent(interval=0.1), 1),
                "memory_rss_bytes": int(p.memory_info().rss),
                "memory_percent": round(p.memory_percent(), 1),
                "num_threads": p.num_threads(),
            }
    except Exception as e:  # noqa: BLE001
        logger.debug("收集进程资源失败: %s", e)
        return {"pid": os.getpid(), "name": "unknown", "cpu_percent": None,
                "memory_rss_bytes": None, "memory_percent": None, "num_threads": None}


def _collect_gpu() -> dict[str, Any]:
    """GPU 占用探测，按优先级尝试 NVIDIA(pynvml) → CUDA(torch) → Apple MPS，逐级降级。"""
    # 1) NVIDIA via pynvml（最完整：显存 + 利用率）
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            count = pynvml.nvmlDeviceGetCount()
            devices = []
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                util = pynvml.nvmlDeviceGetUtilizationRates(h)
                name = pynvml.nvmlDeviceGetName(h)
                devices.append({
                    "index": i,
                    "name": name,
                    "memory_used_bytes": int(mem.used),
                    "memory_total_bytes": int(mem.total),
                    "utilization_percent": int(util.gpu),
                })
            return {"available": True, "backend": "nvidia-nvml", "devices": devices}
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        pass

    # 2) CUDA via torch（显存分配量，无利用率）
    try:
        import torch

        if getattr(torch, "cuda", None) is not None and torch.cuda.is_available():
            devices = []
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                devices.append({
                    "index": i,
                    "name": props.name,
                    "memory_used_bytes": int(torch.cuda.memory_allocated(i)),
                    "memory_total_bytes": int(props.total_memory),
                    "utilization_percent": None,
                })
            return {"available": True, "backend": "cuda-torch", "devices": devices}
    except Exception:
        pass

    # 3) Apple Silicon MPS（仅能确认存在，无法读取显存/利用率）
    try:
        import torch

        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return {"available": True, "backend": "mps-apple", "devices": [{
                "index": 0,
                "name": "Apple Silicon GPU",
                "memory_used_bytes": None,
                "memory_total_bytes": None,
                "utilization_percent": None,
            }]}
    except Exception:
        pass

    return {"available": False, "backend": "none", "devices": [],
            "reason": "未检测到可用 GPU 库（pynvml / CUDA / MPS 均不可用）"}


def _collect_system() -> dict[str, Any]:
    """系统级 CPU / 内存 + 当前进程资源。"""
    import psutil

    vm = psutil.virtual_memory()
    return {
        "cpu_percent": round(psutil.cpu_percent(interval=0.2), 1),
        "cpu_count_logical": psutil.cpu_count(logical=True) or 0,
        "cpu_count_physical": psutil.cpu_count(logical=False) or 0,
        "memory": {
            "percent": round(vm.percent, 1),
            "used_bytes": int(vm.used),
            "available_bytes": int(vm.available),
            "total_bytes": int(vm.total),
        },
        "process": _collect_process(),
    }


@router.get("/api/models/status")
async def models_status():
    """本地重排 + 嵌入模型状态与实时资源占用快照。"""
    try:
        embedding = await run_in_threadpool(_collect_embedding)
        rerank = await run_in_threadpool(_collect_rerank)
        system = await run_in_threadpool(_collect_system)
        system["gpu"] = await run_in_threadpool(_collect_gpu)
        return {
            "timestamp": int(time.time() * 1000),
            "embedding": embedding,
            "rerank": rerank,
            "system": system,
        }
    except Exception as e:  # noqa: BLE001
        logger.error("模型状态API错误: %s", e, exc_info=True)
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail=str(e)) from e
