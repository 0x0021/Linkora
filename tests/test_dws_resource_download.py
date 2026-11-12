"""资源下载迁 dws Shortcut（``chat +messages-resource-download``）的回归测试。

背景（2026-10-22）：Linkora 原先使用原子命令 ``chat message download-media
--output <绝对路径>``，存在两个问题：
1. ``--output`` 接受任意绝对路径 —— 媒体 id / 文件名一旦被污染
   （``content`` 里塞 ``mediaId=...&text=`` 之类），就可能写到任意位置；
2. 失败时可能残留 0 字节文件，被下游当成「下载到空图」。

dws v1.0.63 的 Shortcut ``chat +messages-resource-download`` 补齐了三项安全属性
（2026-10-22 对真实群实测逐条验证）：
- 路径沙箱：``--output`` 只接受**工作目录内相对路径**，拒绝绝对路径与 ``..`` 逃逸；
- 原子落盘：整文件临时落盘后原子发布；
- 默认不覆盖：目标已存在时拒绝写入（需显式 ``--overwrite``）。

Shortcut 要求相对路径，故适配器把子进程 cwd 切到目标目录、只传文件名，
并显式加 ``--overwrite`` 以保持 Linkora 原有的「同名文件可重新下载」语义。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, "src")

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError


class _FakeDws(DwsAdapter):
    """记录命令与 cwd；可模拟「产物未生成」以验证失败路径。"""

    def __init__(self, produce: bool = True):
        self.cli_path = "dws"
        self.dry_run = False
        self.profile = ""
        self.timeout = 30
        self.ai_tag_default = True
        self._calls: list[tuple[list[str], str | None]] = []
        self._produce = produce

    def _run_download(self, args, output_path, *, timeout=None, retries=None,
                      cwd=None, locate_from_dir=False):
        self._calls.append((list(args), cwd))
        if not self._produce:
            raise IMAdapterError("dws exit 1: download failed")
        # 模拟 shortcut 在 cwd 内落盘
        assert cwd is not None
        with open(os.path.join(cwd, os.path.basename(output_path)), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        return output_path


# ============ 1. 命令形态 ============

def test_uses_shortcut_command():
    """使用 Shortcut 命令而非原子 download-media。"""
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        a.download_media(
            media_id="@media1", message_id="msg1",
            conversation_id="cid1",
            output_path=os.path.join(d, "a.png"),
        )
    args, _ = a._calls[0]
    assert args[:2] == ["chat", "+messages-resource-download"]
    assert "download-media" not in args


def test_output_is_relative_filename_with_cwd():
    """--output 传相对文件名，cwd 切到目标目录（满足 Shortcut 的路径沙箱要求）。

    dws v1.0.68+ 会按媒体类型补/规范化扩展名，故 --output 传期望文件名即可，
    产物名差异由 _run_download 的 locate_from_dir 兜底。
    """
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        target_dir = os.path.join(d, "nested", "chatdir")
        a.download_media(
            media_id="@media1", message_id="msg1", conversation_id="cid1",
            output_path=os.path.join(target_dir, "ocr_1.png"),
        )
    args, cwd = a._calls[0]
    assert args[args.index("--output") + 1] == "ocr_1.png"
    # 不得是绝对路径（Shortcut 会直接拒绝）
    assert not os.path.isabs(args[args.index("--output") + 1])
    assert cwd == os.path.abspath(target_dir)


def test_overwrite_flag_present():
    """必须带 --overwrite：Linkora 调用方允许同名文件被重新下载。"""
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        a.download_media(
            media_id="@media1", message_id="msg1", conversation_id="cid1",
            output_path=os.path.join(d, "a.png"),
        )
    args, _ = a._calls[0]
    assert "--overwrite" in args


def test_resource_identity_args_forwarded():
    """资源标识三件套原样透传（mediaId / messageId / openConversationId）。"""
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        a.download_media(
            media_id="@mediaX", message_id="msgY", conversation_id="cidZ",
            output_path=os.path.join(d, "a.png"),
        )
    args, _ = a._calls[0]
    assert args[args.index("--resource-id") + 1] == "@mediaX"
    assert args[args.index("--message-id") + 1] == "msgY"
    assert args[args.index("--open-conversation-id") + 1] == "cidZ"
    assert args[args.index("--type") + 1] == "mediaId"


# ============ 2. 参数校验与目录创建 ============

def test_missing_identifiers_raise():
    """缺 media_id / message_id / conversation_id 直接报错（不发起无意义调用）。"""
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        for kwargs in (
            {"media_id": "", "message_id": "m", "conversation_id": "c"},
            {"media_id": "x", "message_id": "", "conversation_id": "c"},
            {"media_id": "x", "message_id": "m", "conversation_id": ""},
        ):
            try:
                a.download_media(output_path=os.path.join(d, "a.png"), **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError("应当抛出 ValueError")
    assert a._calls == []


def test_target_directory_created():
    """目标目录不存在时自动创建（Shortcut 自身不建目录）。"""
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "deep", "nested", "img.png")
        a.download_media(
            media_id="@m", message_id="msg", conversation_id="cid",
            output_path=target,
        )
        assert os.path.isfile(target)


def test_returns_absolute_path():
    """返回落盘的绝对路径（调用方按此读文件）。"""
    a = _FakeDws()
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "img.png")
        p = a.download_media(
            media_id="@m", message_id="msg", conversation_id="cid",
            output_path=target,
        )
        assert p == os.path.abspath(target)
        assert os.path.isfile(p)


def test_download_failure_propagates():
    """下载失败时异常上抛，由调用方做容错降级。"""
    a = _FakeDws(produce=False)
    with tempfile.TemporaryDirectory() as d:
        try:
            a.download_media(
                media_id="@m", message_id="msg", conversation_id="cid",
                output_path=os.path.join(d, "img.png"),
            )
        except IMAdapterError:
            pass
        else:
            raise AssertionError("应当抛出 IMAdapterError")


# ============ 3. 真实 _run_download 行为（v1.0.68 目录语义 + 错误检测） ============

import json  # noqa: E402
import subprocess  # noqa: E402


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_download_locates_and_renames_from_dir(monkeypatch):
    """dws v1.0.68 把文件以自动命名写到 cwd 内：定位后重命名到期望名并补扩展名。"""
    adapter = DwsAdapter()

    def fake_run(cmd, **kw):
        cwd = kw.get("cwd")
        assert cwd, "cwd 必须切到目标目录"
        # dws 以自动命名（含扩展名）落盘，而非 output_path 期望名
        with open(os.path.join(cwd, "auto_abc123.mp4"), "wb") as f:
            f.write(b"\x00\x01" * 1024)
        return _Res(0, "{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with tempfile.TemporaryDirectory() as d:
        # 期望名无扩展名（如消息 fileName=video 的情况）
        out = os.path.join(d, "video")
        result = adapter.download_media(
            media_id="@m", message_id="msg", conversation_id="cid", output_path=out,
        )
        assert result == os.path.join(d, "video.mp4"), result
        assert os.path.isfile(result)
        # 自动命名的临时文件已被重命名走
        assert not os.path.exists(os.path.join(d, "auto_abc123.mp4"))


def test_download_surfaces_api_error_on_exit0(monkeypatch):
    """dws 即便 API 失败也返回 0，把错误塞进 stdout JSON：必须上抛真实错误而非『未生成有效文件』。"""
    adapter = DwsAdapter()
    err_json = json.dumps({
        "error": {
            "message": "invalid openMsgId: msgX",
            "code": 1,
            "category": "api",
            "reason": "invalid_request",
        }
    })
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(0, err_json))
    with tempfile.TemporaryDirectory() as d:
        try:
            adapter.download_media(
                media_id="@m", message_id="msg", conversation_id="cid",
                output_path=os.path.join(d, "x.mp4"),
            )
        except IMAdapterError as e:
            # 真实错误原因必须出现在异常串里，而不是笼统的「未生成有效文件」
            assert "未生成有效文件" not in str(e)
            assert "invalid openMsgId" in str(e) or "invalid_request" in str(e)
        else:
            raise AssertionError("应当抛出 IMAdapterError")
