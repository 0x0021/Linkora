"""向量嵌入客户端单元测试 — 覆盖 EmbeddingClient 全部路径。

注：EmbeddingClient._get_optimal_device() 内部会 `import torch` 并探测
MPS/CUDA 设备。在 macOS 上真实初始化 torch 的 MPS 后端会触发 libomp
重复注册崩溃（Segmentation fault），故所有涉及本地模型初始化的测试
统一用 ``_mock_torch`` fixture 把 torch 替换为 mock（is_available=False），
让设备选择稳定回落 CPU，同时保持对选择逻辑本身的单测覆盖。
"""
from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.config import EmbeddingConfig
from src.memory.embedding import (
    EmbeddingClient,
    _ProgressTracker,
    _init_load_status,
)


@pytest.fixture(autouse=True)
def _mock_torch(monkeypatch):
    """把 torch 换成 mock，避免 macOS 上真实 import torch 触发 libomp segfault。

    仅当 torch 尚未被真实加载时才注入（若其他测试已 import 真实 torch，
    此处覆盖 sys.modules 会破坏其引用，故跳过）。
    """
    if "torch" in sys.modules:
        yield
        return
    mock_torch = MagicMock()
    mock_torch.backends.mps.is_available.return_value = False
    mock_torch.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    yield


def _cfg(**kw):
    """构造 EmbeddingConfig，默认 disabled。"""
    defaults = {"enabled": False, "provider": "local", "model": "test-model"}
    defaults.update(kw)
    return EmbeddingConfig(**defaults)


# ============================================================================
# __init__
# ============================================================================
class TestInit:
    def test_disabled(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.enabled is False
        assert c.is_enabled is False

    def test_local_init_success(self):
        """mock sentence_transformers 模块，模拟成功加载。"""
        mock_st = MagicMock()
        mock_st.SentenceTransformer.return_value.get_embedding_dimension.return_value = 384
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c = EmbeddingClient(_cfg(enabled=True, provider="local", model="m"))
            assert c._provider == "local"
            assert c.enabled is True
            assert c._load_status["state"] == "ready"
            # 默认 offline=False -> 允许按需下载（本地已缓存则直接加载）；
            # device 由 _get_optimal_device 决定（mock torch 下为 cpu）
            mock_st.SentenceTransformer.assert_called_once_with(
                "m", local_files_only=False, device="cpu"
            )

    def test_local_init_offline_uses_local_files_only(self):
        """offline=True 时必须 local_files_only=True。"""
        mock_st = MagicMock()
        mock_st.SentenceTransformer.return_value.get_embedding_dimension.return_value = 384
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c = EmbeddingClient(_cfg(enabled=True, provider="local", model="m", offline=True))
            assert c.enabled is True
            mock_st.SentenceTransformer.assert_called_once_with(
                "m", local_files_only=True, device="cpu"
            )

    def test_local_init_import_error(self):
        """sentence_transformers 未安装 → enabled=False。"""
        import builtins
        orig_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "sentence_transformers":
                raise ImportError("No module named 'sentence_transformers'")
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            c = EmbeddingClient(_cfg(enabled=True, provider="local", model="m"))
            assert c.enabled is False

    def test_local_init_generic_error(self):
        """加载模型时运行时异常 → enabled=False。"""
        mock_st = MagicMock()
        mock_st.SentenceTransformer.side_effect = RuntimeError("OOM")
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c = EmbeddingClient(_cfg(enabled=True, provider="local"))
            assert c.enabled is False

    def test_local_init_with_hf_token(self):
        mock_st = MagicMock()
        mock_st.SentenceTransformer.return_value.get_embedding_dimension.return_value = 384
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c = EmbeddingClient(_cfg(enabled=True, provider="local", hf_token="hf_xxx"))
            assert c.enabled is True


# ============================================================================
# _get_optimal_device / _is_local_model_path
# ============================================================================
class TestOptimalDevice:
    def test_returns_cpu_when_no_accel(self, monkeypatch):
        """无 MPS/CUDA 时回落 CPU。"""
        import platform

        fake_torch = MagicMock()
        fake_torch.backends.mps.is_available.return_value = False
        fake_torch.cuda.is_available.return_value = False
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setattr(platform, "system", lambda: "Darwin")  # 模拟 macOS 但 MPS 不可用
        assert EmbeddingClient._get_optimal_device() == "cpu"

    def test_prefers_mps_on_macos(self, monkeypatch):
        """Apple Silicon 且 MPS 可用 → mps。"""
        import platform

        fake_torch = MagicMock()
        fake_torch.backends.mps.is_available.return_value = True
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        assert EmbeddingClient._get_optimal_device() == "mps"

    def test_prefers_cuda_when_available(self, monkeypatch):
        """非 macOS（或 MPS 不可用）且 CUDA 可用 → cuda。"""
        import platform

        fake_torch = MagicMock()
        fake_torch.backends.mps.is_available.return_value = False
        fake_torch.cuda.is_available.return_value = True
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert EmbeddingClient._get_optimal_device() == "cuda"


class TestIsLocalModelPath:
    def test_local_path_variants(self):
        assert EmbeddingClient._is_local_model_path("./models/bge")
        assert EmbeddingClient._is_local_model_path("../models/bge")
        assert EmbeddingClient._is_local_model_path("~/models/bge")
        assert EmbeddingClient._is_local_model_path("/abs/path/bge")
        assert EmbeddingClient._is_local_model_path("models/bge") is False  # 相对无 ./ 前缀视为 repo_id

    def test_real_dir_considered_local(self, tmp_path):
        d = tmp_path / "model_dir"
        d.mkdir()
        assert EmbeddingClient._is_local_model_path(str(d)) is True


# ============================================================================
# _init_api
# ============================================================================
class TestInitApi:
    def test_api_init_success(self):
        mock_openai = MagicMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            with patch.dict("os.environ", {"EMBEDDING_API_KEY": "sk-test"}):
                c = EmbeddingClient(_cfg(enabled=True, provider="api", base_url="https://x/v1", model="m"))
                assert c._provider == "api"
                assert c.enabled is True
                mock_openai.OpenAI.assert_called_once()

    def test_api_no_key(self):
        mock_openai = MagicMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            with patch.dict("os.environ", {}, clear=True):
                c = EmbeddingClient(_cfg(enabled=True, provider="api", base_url="https://x/v1"))
                assert c.enabled is False

    def test_api_fallback_llm_key(self):
        mock_openai = MagicMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            with patch.dict("os.environ", {"LLM_API_KEY": "sk-llm"}):
                c = EmbeddingClient(_cfg(enabled=True, provider="api", base_url="https://x/v1"))
                assert c.enabled is True

    def test_api_missing_openai(self):
        """移除 openai 模块 → ImportError → logger.error + disabled。"""
        import builtins
        orig_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "openai":
                raise ImportError("No module named 'openai'")
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            with patch("src.memory.embedding.logger") as mock_log:
                c = EmbeddingClient(_cfg(enabled=True, provider="api", base_url="https://x/v1"))
                assert c.enabled is False
                assert mock_log.error.called


# ============================================================================
# 属性 & reload
# ============================================================================
class TestProperties:
    def test_enabled_property(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.enabled is False
        c._enabled = True
        assert c.enabled is True

    def test_is_enabled_property(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.is_enabled is False
        c._enabled = True
        assert c.is_enabled is True

    def test_reload_disabled(self):
        mock_openai = MagicMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            with patch.dict("os.environ", {"EMBEDDING_API_KEY": "sk-test"}):
                cfg = _cfg(enabled=True, provider="api", base_url="https://x/v1")
                c = EmbeddingClient(cfg)
                cfg.enabled = False
                c.reload()
                assert c.enabled is False

    def test_reload_enabled_to_local(self):
        cfg = _cfg(enabled=False)
        c = EmbeddingClient(cfg)
        cfg.enabled = True
        cfg.provider = "local"
        mock_st = MagicMock()
        mock_st.SentenceTransformer.return_value.get_embedding_dimension.return_value = 384
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c.reload()
            assert c.enabled is True

    def test_reload_enabled_to_api(self):
        cfg = _cfg(enabled=False)
        c = EmbeddingClient(cfg)
        cfg.enabled = True
        cfg.provider = "api"
        mock_openai = MagicMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            with patch.dict("os.environ", {"EMBEDDING_API_KEY": "sk-test"}):
                c.reload()
                assert c.enabled is True


# ============================================================================
# embed
# ============================================================================
class TestEmbed:
    def test_embed_disabled_returns_empty(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.embed("test") == []

    def test_embed_local(self):
        mock_st = MagicMock()
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1, 0.2, 0.3]])
        mock_st.SentenceTransformer.return_value = mock_model
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c = EmbeddingClient(_cfg(enabled=True, provider="local", model="m"))
            mock_model.get_embedding_dimension.return_value = 384
            c._model = mock_model
            result = c.embed("hello")
            assert result == [0.1, 0.2, 0.3]
            mock_model.encode.assert_called_once()

    def test_embed_api(self):
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
        mock_client.embeddings.create.return_value = mock_resp
        mock_openai.OpenAI.return_value = mock_client
        with patch.dict(sys.modules, {"openai": mock_openai}):
            with patch.dict("os.environ", {"EMBEDDING_API_KEY": "sk-test"}):
                c = EmbeddingClient(_cfg(enabled=True, provider="api", base_url="https://x/v1", model="m"))
                result = c.embed("hello")
                assert result == [0.1, 0.2]


# ============================================================================
# embed_with_retry / embed_batch / warmup
# ============================================================================
class TestEmbedWithRetry:
    def test_disabled_returns_empty_without_retry(self):
        """禁用状态是稳定态：直接返回 []，不做无谓重试。"""
        c = EmbeddingClient(_cfg(enabled=False))
        with patch.object(c, "embed", wraps=c.embed) as spy:
            assert c.embed_with_retry("x") == []
            spy.assert_not_called()

    def test_retries_until_success(self):
        """冷启动返回空 → 重试后成功。"""
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"
        calls = {"n": 0}

        def _flaky(text):
            calls["n"] += 1
            return [] if calls["n"] < 2 else [0.5, 0.6]

        c.embed = _flaky
        assert c.embed_with_retry("x", retries=3, backoff=0.001) == [0.5, 0.6]
        assert calls["n"] == 2

    def test_gives_up_after_retries(self):
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"
        c.embed = lambda text: []
        assert c.embed_with_retry("x", retries=3, backoff=0.001) == []


class TestEmbedBatch:
    def test_empty_input(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.embed_batch([]) == []

    def test_disabled_returns_placeholder_vectors(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.embed_batch(["a", "b"]) == [[], []]

    def test_local_batch(self):
        mock_st = MagicMock()
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1], [0.2]])
        mock_model.get_embedding_dimension.return_value = 384
        mock_st.SentenceTransformer.return_value = mock_model
        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            c = EmbeddingClient(_cfg(enabled=True, provider="local", model="m"))
            c._model = mock_model
            result = c.embed_batch(["a", "b"])
            assert result == [[0.1], [0.2]]
            # 批量必须走一次 encode（show_progress_bar=True 已由 embed_batch 传入）
            mock_model.encode.assert_called_once()

    def test_local_model_missing_returns_placeholders(self):
        c = EmbeddingClient.__new__(EmbeddingClient)
        c.config = _cfg(enabled=True, provider="local")
        c._enabled = True
        c._provider = "local"
        c._model = None
        c._api_client = None
        c._lock = threading.Lock()
        assert c.embed_batch(["a"]) == [[]]

    def test_api_batch_loops(self):
        c = EmbeddingClient.__new__(EmbeddingClient)
        c.config = _cfg(enabled=True, provider="api", model="m")
        c._enabled = True
        c._provider = "api"
        c._model = None
        c._api_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.9])]
        c._api_client.embeddings.create.return_value = mock_resp
        c._lock = threading.Lock()
        assert c.embed_batch(["a", "b"]) == [[0.9], [0.9]]


class TestWarmup:
    def test_disabled_returns_none(self):
        c = EmbeddingClient(_cfg(enabled=False))
        assert c.warmup() is None

    def test_model_not_ready_returns_none(self):
        """模型永不就绪 → 等待超时后返回 None（mock 时钟避免真实 120s 空转）。"""
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = None
        now = [100.0]

        def _fake_now():
            now[0] += 0.4  # 每次迭代推进 0.4s，快速越过 120s 窗口
            return now[0]

        with patch("time.time", side_effect=_fake_now), \
             patch("time.sleep", lambda s: None), \
             patch.object(c, "embed", side_effect=AssertionError("不应调用")):
            assert c.warmup() is None

    def test_success_returns_cost(self):
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"
        with patch.object(c, "embed", return_value=[0.1]):
            cost = c.warmup()
            assert cost is not None and cost >= 0


# ============================================================================
# 心跳保活
# ============================================================================
class TestHeartbeat:
    def test_start_stop_lifecycle(self):
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"
        c.start_heartbeat(interval=30.0)
        assert c._heartbeat_running is True
        c.stop_heartbeat()
        assert c._heartbeat_running is False

    def test_start_idempotent(self):
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"
        c.start_heartbeat(interval=30.0)
        first_stop = c._heartbeat_stop
        c.start_heartbeat(interval=30.0)  # 已运行 → 幂等返回
        assert c._heartbeat_stop is first_stop
        c.stop_heartbeat()

    def test_disabled_does_not_start(self):
        c = EmbeddingClient(_cfg(enabled=False))
        c.start_heartbeat(interval=30.0)
        assert c._heartbeat_running is False

    def test_ensure_heartbeat_starts_when_enabled(self):
        """reload 后 _ensure_heartbeat 幂等启动心跳。"""
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"
        c._ensure_heartbeat()
        assert c._heartbeat_running is True
        c.stop_heartbeat()

    def test_ensure_heartbeat_noop_when_disabled(self):
        c = EmbeddingClient(_cfg(enabled=False))
        c._ensure_heartbeat()
        assert c._heartbeat_running is False

    def test_loop_stops_on_event(self):
        """心跳循环在 stop_event 置位后立即退出，且异常被吞。"""
        c = EmbeddingClient(_cfg(enabled=False))
        c._enabled = True
        c._model = MagicMock()
        c._provider = "local"

        stop = threading.Event()
        t = threading.Thread(
            target=c._heartbeat_loop, args=(0.01, stop), daemon=True
        )
        t.start()
        time.sleep(0.05)  # 让循环至少跑几轮（含 embed 调用）
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), "心跳循环应随 stop_event 退出"
        assert c._heartbeat_running is False


# ============================================================================
# _embed_local
# ============================================================================
class TestEmbedLocal:
    def _make_local_client(self):
        """构造一个已初始化好的 local client。"""
        c = EmbeddingClient.__new__(EmbeddingClient)
        c.config = _cfg(enabled=True, provider="local")
        c._enabled = True
        c._provider = "local"
        c._model = MagicMock()
        return c

    def test_str_input(self):
        c = self._make_local_client()
        c._model.encode.return_value = np.array([[0.1, 0.2]])
        r = c._embed_local("hello")
        assert r == [0.1, 0.2]

    def test_list_input(self):
        c = self._make_local_client()
        c._model.encode.return_value = np.array([[0.1, 0.2], [0.3, 0.4]])
        r = c._embed_local(["a", "b"])
        assert r == [[0.1, 0.2], [0.3, 0.4]]

    def test_exception_returns_empty(self):
        c = self._make_local_client()
        c._model.encode.side_effect = RuntimeError("OOM")
        assert c._embed_local("hello") == []


# ============================================================================
# _embed_api
# ============================================================================
class TestEmbedApi:
    def _make_api_client(self):
        c = EmbeddingClient.__new__(EmbeddingClient)
        c.config = _cfg(enabled=True, provider="api", model="m")
        c._enabled = True
        c._provider = "api"
        c._api_client = MagicMock()
        return c

    def test_success(self):
        c = self._make_api_client()
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.5, 0.6])]
        c._api_client.embeddings.create.return_value = mock_resp
        r = c._embed_api("test")
        assert r == [0.5, 0.6]

    def test_exception_returns_empty(self):
        c = self._make_api_client()
        c._api_client.embeddings.create.side_effect = RuntimeError("API error")
        r = c._embed_api("test")
        assert r == []


# ============================================================================
# cosine_similarity
# ============================================================================
class TestCosineSimilarity:
    def test_normal(self):
        assert pytest.approx(
            EmbeddingClient.cosine_similarity([1, 0, 0], [0, 1, 0]), abs=1e-6
        ) == 0.0

    def test_identical(self):
        assert pytest.approx(
            EmbeddingClient.cosine_similarity([1, 2, 3], [1, 2, 3]), abs=1e-6
        ) == 1.0

    def test_empty_vec(self):
        assert EmbeddingClient.cosine_similarity([], [1, 2]) == 0.0
        assert EmbeddingClient.cosine_similarity([1, 2], []) == 0.0

    def test_zero_norm(self):
        """零向量不应产生 nan（修复 0/0 除零污染检索排序），统一返回 0.0。"""
        assert EmbeddingClient.cosine_similarity([0, 0], [0, 0]) == 0.0
        assert EmbeddingClient.cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0
        assert EmbeddingClient.cosine_similarity([1, 2, 3], [0, 0, 0]) == 0.0

    def test_numpy_exception_fallback(self):
        """numpy 运算异常时返回 0.0。"""
        with patch("numpy.linalg.norm", side_effect=ValueError("broken")):
            result = EmbeddingClient.cosine_similarity([1, 2], [3, 4])
            assert result == 0.0


# ============================================================================
# _ProgressTracker（跨文件下载进度累加）
# ============================================================================
class TestProgressTracker:
    def _tracker(self):
        status = _init_load_status("downloading")
        return _ProgressTracker(status), status

    def test_accumulates_across_files(self):
        t, s = self._tracker()
        t.register_file("a.bin", 100)
        t.register_file("b.bin", 100)
        t.add_downloaded("a.bin", 50)
        assert s["total"] == 200
        assert s["downloaded"] == 50
        assert pytest.approx(s["progress"], abs=0.1) == 25.0
        t.add_downloaded("a.bin", 50)
        t.add_downloaded("b.bin", 100)
        assert s["downloaded"] == 200
        assert s["progress"] == 100.0

    def test_zero_total_no_divide_by_zero(self):
        t, s = self._tracker()
        t.register_file("a.bin", 0)
        t.add_downloaded("a.bin", 0)
        assert s["progress"] == 0.0

    def test_largest_total_wins_on_reregister(self):
        t, s = self._tracker()
        t.register_file("a.bin", 50)
        t.register_file("a.bin", 200)
        assert s["total"] == 200

    def test_set_message(self):
        t, s = self._tracker()
        t.set_message("下载 file.bin")
        assert s["message"] == "下载 file.bin"


# ============================================================================
# 后台下载（background=True）与 tqdm_class 进度钩子
# ============================================================================
class TestBackgroundDownload:
    def test_background_download_passes_tqdm_class(self):
        """background=True 且非离线时，应触发后台下载并注入 tqdm_class。"""
        mock_st = MagicMock()
        mock_st.SentenceTransformer.return_value.get_embedding_dimension.return_value = 384
        captured = {}

        def fake_snapshot(repo_id=None, tqdm_class=None, token=None, max_workers=None, **kw):
            captured["tqdm_class"] = tqdm_class
            # 模拟两个文件的下载，驱动进度回调
            t1 = tqdm_class(total=100, desc="a.bin")
            t1.update(100)
            t1.close()
            t2 = tqdm_class(total=100, desc="b.bin")
            t2.update(100)
            t2.close()
            return "/tmp/fake_model"

        with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
            with patch("src.memory.embedding.snapshot_download", side_effect=fake_snapshot):
                c = EmbeddingClient(
                    _cfg(enabled=True, provider="local", model="m"), background=True
                )
                # __init__ 同步设置 downloading 后才起线程
                assert c.get_load_status()["state"] != "pending"
                # 等待后台线程完成
                status = c.get_load_status()
                for _ in range(100):
                    status = c.get_load_status()
                    if status["state"] in ("ready", "error"):
                        break
                    time.sleep(0.02)
                assert status["state"] == "ready"
                assert captured["tqdm_class"] is not None
                assert c.get_load_status()["progress"] == 100.0
