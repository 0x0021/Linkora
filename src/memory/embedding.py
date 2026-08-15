from __future__ import annotations

import logging
import os
import threading
import time
from typing import cast

import numpy as np

from huggingface_hub import snapshot_download
from tqdm import tqdm as _TqdmBase

from src.config import EmbeddingConfig

logger = logging.getLogger(__name__)

# 压制 httpx/huggingface_hub 的 INFO 日志（避免离线模式下出现 404 探测日志）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)


def _init_load_status(state: str = "pending") -> dict:
    return {
        "state": state,        # pending | downloading | loading | ready | error | disabled
        "progress": 0.0,       # 0~100
        "downloaded": 0,       # 已下载字节
        "total": 0,            # 总字节
        "message": "",         # 进度描述 / 错误信息
    }


class _ProgressTracker:
    """跨文件累加 huggingface_hub 的下载进度，写入共享的 status dict。

    huggingface_hub 1.21.0 的 snapshot_download 不支持 progress_callback，
    改用自定义 tqdm_class：每个文件下载会创建一个 tqdm 实例，其 ``total`` 为该
    文件字节数，``update(n)`` 为已下载字节增量。这里跨文件累加得到总进度。
    """

    def __init__(self, status: dict):
        self._status = status
        self._files_total: dict[str, int] = {}
        self._files_done: dict[str, int] = {}
        self._lock = threading.Lock()

    def register_file(self, name: str, total: int) -> None:
        with self._lock:
            if name and (name not in self._files_total or (total or 0) > self._files_total[name]):
                self._files_total[name] = total or 0
            self._recompute()

    def add_downloaded(self, name: str, n: int) -> None:
        with self._lock:
            if name:
                self._files_done[name] = self._files_done.get(name, 0) + (n or 0)
            self._recompute()

    def set_message(self, msg: str) -> None:
        with self._lock:
            self._status["message"] = msg

    def _recompute(self) -> None:
        total = sum(v for v in self._files_total.values() if v)
        downloaded = sum(self._files_done.values())
        self._status["total"] = total
        self._status["downloaded"] = downloaded
        self._status["progress"] = round(downloaded / total * 100.0, 1) if total > 0 else 0.0


def _make_tqdm_class(tracker: _ProgressTracker):
    """返回一个绑定到 tracker 的 tqdm 子类，供 snapshot_download 注入。"""

    class _EmbeddingTqdm(_TqdmBase):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._tracker = tracker
            name = kwargs.get("desc") or ""
            self._tracker.register_file(name, self.total or 0)
            if name:
                self._tracker.set_message(name)

        def update(self, n=1):
            super().update(n)
            self._tracker.add_downloaded(self.desc or "", int(n or 0))

    return _EmbeddingTqdm


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig, background: bool = False):
        self.config = config
        self._enabled = config.enabled
        self._model = None
        self._provider = None
        self._api_client = None
        self._lock = threading.Lock()
        self._load_status = _init_load_status(
            "disabled" if not config.enabled else "pending"
        )
        # 心跳保活状态（#7）：标记线程是否运行 + 停止信号
        # 使用独立锁，避免与 reload() 持有的 self._lock 形成重入死锁
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_running = False
        self._heartbeat_stop: threading.Event | None = None

        if not self._enabled:
            logger.info("向量嵌入已禁用")
            return

        if config.provider == "local":
            offline = bool(getattr(config, "offline", False))
            if background and not offline:
                # 后台下载：不阻塞启动，Web 先起，进度可经 get_load_status() 轮询
                self._load_status["state"] = "downloading"
                threading.Thread(
                    target=self._init_local, args=(config, True), daemon=True
                ).start()
            else:
                # 同步直载（离线 / 单测 / 热重载同步分支）
                self._init_local(config, False)
        else:
            self._init_api(config)

    # ------------------------------------------------------------------
    # 本地模型加载
    # ------------------------------------------------------------------
    @staticmethod
    def _is_local_model_path(model: str) -> bool:
        """判断 model 是否为本地已存在的路径而非 HuggingFace repo_id。"""
        return (
            os.path.isdir(model)
            or os.path.isfile(model)
            or model.startswith("./")
            or model.startswith("../")
            or model.startswith("~")
            or model.startswith("/")
        )

    @staticmethod
    def _get_optimal_device() -> str:
        """自动选择最优推理设备。

        - macOS（Apple Silicon）：mps
        - 其他平台 CUDA 可用：cuda
        - 否则：cpu
        """
        import platform

        import torch

        if platform.system() == "Darwin" and torch.backends.mps.is_available():
            logger.info("检测到 Apple Silicon，使用 MPS 设备")
            return "mps"
        if torch.cuda.is_available():
            logger.info("检测到 CUDA，使用 GPU 设备")
            return "cuda"
        logger.info("未检测到加速设备，使用 CPU")
        return "cpu"

    def _init_local(self, config: EmbeddingConfig, download_with_progress: bool = False) -> None:
        try:
            from sentence_transformers import SentenceTransformer

            hf_token = getattr(config, "hf_token", "") or os.environ.get("HF_TOKEN", "")
            if hf_token:
                os.environ["HF_TOKEN"] = hf_token

            offline = bool(getattr(config, "offline", False))
            is_local = self._is_local_model_path(config.model)
            self._load_status["state"] = "loading"

            device = self._get_optimal_device()

            if offline or is_local:
                # 纯离线 / 本地路径：直接从本地加载模型，不联网
                os.environ["HF_HUB_OFFLINE"] = "1"
                logger.info("正在加载本地向量模型: %s（本地路径/离线模式）", config.model)
                self._model = SentenceTransformer(
                    config.model, local_files_only=True, device=device
                )
            elif download_with_progress:
                # 在线：先带进度（tqdm_class 钩子）下载到 HF 缓存，再从本地加载
                logger.info("正在下载向量模型: %s（带进度）", config.model)
                self._load_status["state"] = "downloading"
                self._load_status["message"] = "准备下载模型文件…"
                tracker = _ProgressTracker(self._load_status)
                tqdm_cls = _make_tqdm_class(tracker)
                local_path = snapshot_download(
                    repo_id=config.model,
                    tqdm_class=tqdm_cls,  # type: ignore[arg-type]
                    token=hf_token or None,
                    max_workers=4,
                )  # type: ignore[call-overload]
                self._load_status["state"] = "loading"
                self._load_status["message"] = "模型文件下载完成，正在加载…"
                self._model = SentenceTransformer(local_path, device=device)
            else:
                # 同步直载：保持 local_files_only=offline 以兼容既有行为/测试
                self._model = SentenceTransformer(
                    config.model, local_files_only=offline, device=device
                )

            # MPS/CUDA 自动启用 FP16 加速推理
            if device in ("mps", "cuda"):
                self._model.half()

            # 可配置最大序列长度
            max_seq_length = getattr(config, "max_seq_length", 384)
            self._model.max_seq_length = max_seq_length

            self._provider = "local"
            self._load_status["state"] = "ready"
            self._load_status["progress"] = 100.0

            dim = self._model.get_embedding_dimension()
            param_dtype = next(self._model.parameters()).dtype
            logger.info(
                "向量模型加载完成: device=%s, dtype=%s, max_seq_length=%d, 维度=%d",
                device, param_dtype, max_seq_length, dim,
            )
        except Exception as e:
            self._enabled = False
            self._model = None
            self._load_status["state"] = "error"
            self._load_status["message"] = str(e)
            logger.error("加载本地向量模型失败: %s", e)

    def _init_api(self, config: EmbeddingConfig) -> None:
        try:
            from openai import OpenAI
        except ImportError:
            logger.error("未安装 openai")
            self._enabled = False
            self._load_status["state"] = "error"
            self._load_status["message"] = "未安装 openai 库"
            return

        api_key = os.environ.get("EMBEDDING_API_KEY") or config.api_key
        if not api_key:
            api_key = os.environ.get("LLM_API_KEY")
        if not api_key:
            logger.warning("向量嵌入 API 密钥未设置")
            self._enabled = False
            self._load_status["state"] = "error"
            self._load_status["message"] = "API 密钥未设置"
            return

        self._api_client = OpenAI(
            base_url=config.base_url,
            api_key=api_key,
            timeout=60,
        )
        self._provider = "api"
        self._load_status["state"] = "ready"
        self._load_status["progress"] = 100.0
        logger.info("向量嵌入 API 已初始化: %s @ %s", config.model, config.base_url)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_load_status(self) -> dict:
        """返回当前加载状态（可被 Web 轮询）。"""
        with self._lock:
            return dict(self._load_status)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def reload(self) -> None:
        """重新加载配置，支持热更新。

        【H8修复】reload 失败时保留旧模型，避免服务中断。
        先在临时变量中加载新模型，成功后再替换 self._model；
        失败则保留旧模型继续服务，仅更新状态为 error。
        """
        with self._lock:
            self._enabled = self.config.enabled
            if not self._enabled:
                self._model = None
                self._load_status = _init_load_status("disabled")
                logger.info("向量嵌入已在重新加载后禁用")
                return

            # 保存旧模型引用，加载失败时回退
            old_model = self._model
            old_provider = self._provider

            if self.config.provider == "local":
                offline = bool(getattr(self.config, "offline", False))
                if offline:
                    # 同步重新加载：先清空，失败则回退
                    self._model = None
                    self._init_local(self.config, False)
                    # 如果加载失败（_init_local 内部设 _enabled=False），回退旧模型
                    if not self._enabled and old_model is not None:
                        self._model = old_model
                        self._provider = old_provider
                        self._enabled = True
                        self._load_status = _init_load_status("ready")
                        self._load_status["progress"] = 100.0
                        logger.warning("[reload] 本地模型重载失败，已回退到旧模型继续服务")
                else:
                    # 在线：后台重新下载（带进度）
                    self._load_status = _init_load_status("downloading")
                    threading.Thread(
                        target=self._init_local, args=(self.config, True), daemon=True
                    ).start()
            else:
                self._model = None
                self._init_api(self.config)
                if not self._enabled and old_model is not None:
                    self._model = old_model
                    self._provider = old_provider
                    self._enabled = True
                    self._load_status = _init_load_status("ready")
                    self._load_status["progress"] = 100.0
                    logger.warning("[reload] API 模型重载失败，已回退到旧模型继续服务")

        # 热重载后若模型变为启用（如 disabled->enabled），确保心跳保活随之启动
        self._ensure_heartbeat()

    # ------------------------------------------------------------------
    # 向量化
    # ------------------------------------------------------------------
    def embed(self, text: str) -> list[float]:
        with self._lock:
            if not self._enabled:
                return []
            provider = self._provider
            model = self._model
            api_client = self._api_client

        if provider == "local":
            # 后台下载完成前 _model 为 None -> 优雅降级返回 []
            if model is None:
                return []
            return self._embed_local(text)
        else:
            if api_client is None:
                return []
            return self._embed_api(text)

    def embed_with_retry(self, text: str, retries: int = 3,
                         backoff: float = 0.3) -> list[float]:
        """向量化，遇瞬时失败（返回空列表）自动重试。

        【根因修复】原 ``embed()`` 在模型冷启动 / 偶发异常时返回 ``[]``，调用方
        普遍用 ``if emb:`` 静默吞掉，导致部分 RAG 文档的 chunk 无 embedding、
        检索不到（需手动重存才修复）。这里对空结果做有限重试，覆盖冷启动与抖动；
        仍失败则如实返回 ``[]``，由调用方记录失败数。
        """
        last: list[float] = []
        # 【修复#4】模型禁用或尚未加载完成属稳定状态，非瞬时异常，
        # 直接返回空，避免做无谓的 3 次重试（退避远短于模型后台加载耗时）并掩盖问题。
        if not self.is_enabled:
            return []
        # local 模式依赖本地模型实例；api 模式依赖 OpenAI 客户端
        if self._provider == "local" and self._model is None:
            return []
        if self._provider == "api" and self._api_client is None:
            return []
        for attempt in range(max(1, retries)):
            try:
                last = self.embed(text)
            except Exception as e:  # 防御：embed 内部未捕获的异常同样重试
                logger.warning("向量嵌入异常（第 %d 次）: %s", attempt + 1, e)
                last = []
            if last:
                return last
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
        return last

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，提高建库速度。

        一次 encode 调用处理多条文本，相比逐条 embed() 可大幅减少
        Python 调用开销和 GPU 同步次数，适合建库等批量场景。
        """
        if not texts:
            return []
        with self._lock:
            if not self._enabled:
                return [[] for _ in texts]
            provider = self._provider
            model = self._model
            api_client = self._api_client

        if provider == "local":
            if model is None:
                return [[] for _ in texts]
            return cast(list[list[float]], self._embed_local(texts, show_progress_bar=True))
        else:
            if api_client is None:
                return [[] for _ in texts]
            result: list[list[float]] = []
            for text in texts:
                result.append(self._embed_api(text))
            return result

    def warmup(self, dummy: str = "预热向量模型") -> float | None:
        """预热：等待模型就绪后做一次 dummy 推理，缩短首请求冷启动，返回耗时（秒）。

        设计为在后台线程调用（不阻塞启动）；模型就绪前轮询等待，超时则放弃并返回 None。
        对本地模型，首次 encode 常含惰性初始化开销，预热可让首个真实请求直接命中热模型。
        """
        if not self.is_enabled:
            return None
        # local 模式等待后台模型加载完成；api 模式无需等待模型文件
        if self._provider == "local":
            deadline = time.time() + 120
            while self._model is None and time.time() < deadline:
                time.sleep(0.3)
            if self._model is None:
                logger.warning("[嵌入] 预热跳过：模型在等待时间内未就绪")
                return None
        elif self._api_client is None:
            logger.warning("[嵌入] 预热跳过：API 客户端未初始化")
            return None
        t0 = time.time()
        try:
            self.embed(dummy)
        except Exception as e:
            logger.warning("[嵌入] 预热 dummy 推理异常: %s", e)
            return None
        cost = time.time() - t0
        logger.info("[嵌入] 预热完成，首次推理耗时 %.2fs", cost)
        return cost

    def start_heartbeat(self, interval: float = 300.0) -> None:
        """心跳保活（#7）：后台守护线程周期性做 dummy 推理，防止模型被释放/卸载。

        ``warmup()`` 仅做一次推理；长时间无真实请求时模型仍可能被释放
        （vRAM 回收、空闲卸载策略等）。心跳以固定间隔触发 ``embed()``，
        使模型保持常驻热状态，确保首个真实请求仍是热推理。

        行为约定：

        - 线程为 daemon，进程退出自动结束；``stop_heartbeat()`` 可主动停止。
        - 已运行时幂等（不重复起线程）。
        - 心跳期间模型被 ``reload()`` 替换属正常：``embed()`` 每次重新读取
          ``self._model``，不会因引用陈旧失效；加载间隙返回 ``[]`` 不影响循环。
        - 间隔下限 30s，避免过于频繁占用算力。
        """
        if not self.is_enabled:
            return
        with self._heartbeat_lock:
            if self._heartbeat_running:
                return
            self._heartbeat_running = True
            self._heartbeat_stop = threading.Event()
            stop_event = self._heartbeat_stop
        interval = max(30.0, float(interval))
        threading.Thread(
            target=self._heartbeat_loop, args=(interval, stop_event), daemon=True
        ).start()
        logger.info("[嵌入] 心跳保活已启动，间隔 %.0fs", interval)

    def _heartbeat_loop(self, interval: float, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                # 先 wait 一个间隔（stop 可立即唤醒），到点再做一次推理
                if stop_event.wait(timeout=interval):
                    break
                if not self.is_enabled or self._model is None:
                    continue
                try:
                    self.embed("向量模型心跳保活")
                except Exception as e:  # 心跳异常绝不抛出，避免线程崩溃
                    logger.warning("[嵌入] 心跳推理异常（已忽略）: %s", e)
        finally:
            with self._heartbeat_lock:
                self._heartbeat_running = False

    def stop_heartbeat(self) -> None:
        """停止心跳保活线程（如进程关闭 / 测试清理）。"""
        with self._heartbeat_lock:
            stop = self._heartbeat_stop
            self._heartbeat_stop = None
            self._heartbeat_running = False
        if stop is not None:
            stop.set()
        logger.info("[嵌入] 心跳保活已停止")

    def _ensure_heartbeat(self) -> None:
        """若已启用且心跳未运行，则启动之（幂等）。供 reload 后调用。"""
        if self.is_enabled:
            self.start_heartbeat(getattr(self.config, "heartbeat_interval", 300.0))

    def _embed_local(self, text, batch_size=None, convert_to_numpy=True,
                     show_progress_bar=False) -> list[float]:
        """接受 str 或 list[str]，单输入时返回 1D list[float]。"""
        try:
            texts = [text] if isinstance(text, str) else text
            if batch_size is None:
                batch_size = getattr(self.config, "batch_size", 128)
            assert self._model is not None
            vector = self._model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=batch_size,
                convert_to_numpy=convert_to_numpy,
                show_progress_bar=show_progress_bar,
            )
            if len(texts) == 1:
                return vector[0].tolist()
            return vector.tolist()
        except Exception as e:
            logger.error("本地向量嵌入失败: %s", e, exc_info=True)
            return []

    def _embed_api(self, text: str) -> list[float]:
        try:
            assert self._api_client is not None
            response = self._api_client.embeddings.create(
                input=text,
                model=self.config.model,
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error("API 向量嵌入失败: %s", e, exc_info=True)
            return []

    @staticmethod
    def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
        if not vec1 or not vec2:
            return 0.0
        try:
            a = np.array(vec1)
            b = np.array(vec2)
            na = np.linalg.norm(a)
            nb = np.linalg.norm(b)
            if na == 0 or nb == 0:
                return 0.0
            return float(np.dot(a, b) / (na * nb))
        except Exception as e:
            logger.debug("cosine similarity failed: %s", e)
            return 0.0
