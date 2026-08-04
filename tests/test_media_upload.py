"""图片/媒体上传工具单元测试。

分层覆盖：
- 适配器 DwsAdapter.media_upload（mock 其底层 run，捕获最终命令行参数）：
  * 真实命令形态：chat media upload --file <path> --type <type>
  * mediaId 提取兼容多种返回结构（mediaId / media_id / result / data / 嵌套）
  * 鉴权/业务失败（{"error": {...}}）抛出清晰错误
  * 文件不存在抛 ValueError
- 工具 ImageUploadTool.execute（mock 整个 dws）：
  * 成功返回 media_id
  * 参数校验：file_path 必填 / 文件须存在 / media_type 须合法
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.dws_adapter import DwsAdapter
from src.tools.media import ImageUploadTool


# ---------- 适配器层 ----------

def _adapter():
    dws = DwsAdapter.__new__(DwsAdapter)
    dws.dry_run = False
    dws.ai_tag_default = True
    dws.run = MagicMock(return_value={"mediaId": "MID123"})
    return dws


def _sent_args(dws):
    return dws.run.call_args[0][0]


def test_media_upload_command_shape(tmp_path):
    dws = _adapter()
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG")
    mid = dws.media_upload(str(f), "image")
    a = _sent_args(dws)
    assert a[:3] == ["chat", "media", "upload"]
    assert "--file" in a and a[a.index("--file") + 1] == str(f)
    assert "--type" in a and a[a.index("--type") + 1] == "image"
    assert mid == "MID123"


def test_media_upload_missing_file():
    dws = _adapter()
    try:
        dws.media_upload("/nonexistent/x.png")
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "文件不存在" in str(e)


def test_media_upload_error_shape(tmp_path):
    dws = _adapter()
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG")
    dws.run.return_value = {"error": {"category": "auth", "message": "缺少应用凭证"}}
    try:
        dws.media_upload(str(f))
        assert False, "应抛 RuntimeError"
    except RuntimeError as e:
        assert "缺少应用凭证" in str(e)


def test_extract_media_id_variants():
    dws = _adapter()
    assert dws._extract_media_id({"mediaId": "A"}) == "A"
    assert dws._extract_media_id({"media_id": "B"}) == "B"
    assert dws._extract_media_id({"result": {"mediaId": "C"}}) == "C"
    assert dws._extract_media_id({"data": {"media_id": "D"}}) == "D"
    assert dws._extract_media_id({"media": {"mediaId": "E"}}) == "E"
    assert dws._extract_media_id({"x": {"media_id": "F"}}) == "F"
    assert dws._extract_media_id({}) == ""
    assert dws._extract_media_id({"mediaId": ""}) == ""


# ---------- 工具层 ----------

def _make_tool():
    dws = MagicMock()
    dws.media_upload.return_value = "MID999"
    return ImageUploadTool(dws=dws), dws


def test_upload_tool_success(tmp_path):
    tool, dws = _make_tool()
    f = tmp_path / "p.jpg"
    f.write_bytes(b"data")
    r = tool.execute({"file_path": str(f), "media_type": "image"})
    assert r.get("success") is True and r.get("media_id") == "MID999"
    dws.media_upload.assert_called_once_with(str(f), "image")


def test_upload_tool_missing_file(tmp_path):
    tool, dws = _make_tool()
    r = tool.execute({"file_path": str(tmp_path / "nope.png")})
    assert "error" in r
    dws.media_upload.assert_not_called()


def test_upload_tool_missing_path():
    tool, dws = _make_tool()
    r = tool.execute({"media_type": "image"})
    assert "error" in r


def test_upload_tool_bad_type(tmp_path):
    tool, dws = _make_tool()
    f = tmp_path / "p.png"
    f.write_bytes(b"x")
    r = tool.execute({"file_path": str(f), "media_type": "pdf"})
    assert "error" in r and "media_type" in r["error"]


def test_upload_tool_default_type(tmp_path):
    tool, dws = _make_tool()
    f = tmp_path / "p.png"
    f.write_bytes(b"x")
    tool.execute({"file_path": str(f)})
    dws.media_upload.assert_called_once_with(str(f), "image")


def test_upload_tool_dws_exception(tmp_path):
    """media_upload 抛出异常时返回 error 而非崩溃。"""
    tool, dws = _make_tool()
    dws.media_upload.side_effect = RuntimeError("网络超时")
    f = tmp_path / "p.png"
    f.write_bytes(b"x")
    r = tool.execute({"file_path": str(f)})
    assert "error" in r
    assert "上传失败" in r["error"]
