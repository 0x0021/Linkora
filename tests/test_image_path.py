"""src.image_path 工具模块单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.image_path import (
    account_id_dir,
    image_rel_path,
    image_subdir,
    is_new_image_path,
    parse_image_rel_path,
    safe_path_component,
)


def test_safe_path_component_basic():
    # 汉字保留
    assert safe_path_component("陈海艳") == "陈海艳"
    # 纯 hex chat_id 原样
    assert safe_path_component("oc_13c85f9a027902117d9063f2dc04f138") == "oc_13c85f9a027902117d9063f2dc04f138"


def test_safe_path_component_special_chars():
    # 钉钉 base64 chat_id 含 + / = 全部转 _
    assert safe_path_component("cidB75S9QNvDmZabnBuIdwWoVzEPClGZiWHpZfyYdmL5zQ=") == "cidB75S9QNvDmZabnBuIdwWoVzEPClGZiWHpZfyYdmL5zQ"
    assert safe_path_component("cid/abc") == "cid_abc"
    assert safe_path_component("cid+abc") == "cid_abc"
    assert safe_path_component("a b") == "a_b"


def test_safe_path_component_edge():
    assert safe_path_component(None) == "_"
    assert safe_path_component("") == "_"
    assert safe_path_component(".") == "_"
    assert safe_path_component("..") == "_"
    # 超长截断
    assert len(safe_path_component("x" * 200)) <= 80


def test_account_id_dir():
    assert account_id_dir("feishu:ou_abc_123") == "ou_abc_123"
    assert account_id_dir("dingtalk:corp123") == "corp123"
    assert account_id_dir("corp123") == "corp123"  # 无冒号
    assert account_id_dir("") == "_"


def test_image_subdir_structure():
    sub = image_subdir("./data/tmp_images", "dingtalk", "dingtalk:corp123",
                       "cidB75S9QNvDmZabnBuI=")
    parts = sub.parts
    assert parts[-3] == "dingtalk"
    assert parts[-2] == "corp123"
    assert parts[-1] == "cidB75S9QNvDmZabnBuI"  # = 被清掉


def test_image_rel_path_format():
    rel = image_rel_path("./data/tmp_images", "feishu", "feishu:ou_abc",
                         "oc_123", "ocr_xx.png")
    # 返回相对 image_temp_dir 的路径（不含前缀），无论入参是否绝对
    assert rel == "feishu/ou_abc/oc_123/ocr_xx.png"
    # 始终 posix 正斜杠
    assert "\\" not in rel
    # 入参为绝对路径时同样返回相对路径
    rel2 = image_rel_path("/abs/path/data/tmp_images", "feishu", "feishu:ou_abc",
                          "oc_123", "ocr_xx.png")
    assert rel2 == "feishu/ou_abc/oc_123/ocr_xx.png"


def test_is_new_image_path():
    assert is_new_image_path("feishu/ou_abc/oc_123/ocr_x.png") is True
    assert is_new_image_path("dingtalk/corp123/cid_abc/card_x.bin") is True
    # 旧 2 段结构不是新结构
    assert is_new_image_path("陈海艳/ocr_x.png") is False
    # 非已知平台前缀
    assert is_new_image_path("foo/bar/baz/qq.png") is False


def test_parse_image_rel_path():
    p = parse_image_rel_path("feishu/ou_abc/oc_123/ocr_x.png")
    assert p["platform"] == "feishu"
    assert p["account_id_dir"] == "ou_abc"
    assert p["chat_id_dir"] == "oc_123"
    assert p["filename"] == "ocr_x.png"
    # 旧结构返回 None
    assert parse_image_rel_path("陈海艳/ocr_x.png") is None


def test_image_subdir_is_absolute_resolved(tmp_path: Path):
    # 相对目录应基于传入的 image_temp_dir
    sub = image_subdir(str(tmp_path / "imgroot"), "feishu", "feishu:ou_x", "oc_y")
    assert sub.is_absolute()
    assert sub.name == "oc_y"
