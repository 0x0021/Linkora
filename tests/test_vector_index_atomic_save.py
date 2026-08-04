"""faiss 索引保存应原子写（临时文件 + os.replace），避免多进程互相截断。

主进程与 web 进程都会 save 同一 .faiss，非原子覆盖可能在并发/崩溃时损坏索引文件。
这里验证 save 先写临时文件再 os.replace 原子替换，且临时文件不留残留。
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("faiss")

from src.memory.vector_index import VectorIndex


def test_save_atomic_replace(tmp_path: Path):
    idx = VectorIndex(dim=4, index_path=str(tmp_path / "x.faiss"))
    idx.add(1, [0.1, 0.2, 0.3, 0.4])

    replaced_targets = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced_targets.append(dst)
        return real_replace(src, dst)

    with mock.patch.object(os, "replace", side_effect=spy_replace):
        idx.save()

    # 必须调用 os.replace 把临时索引原子替换到最终路径
    assert str(tmp_path / "x.faiss") in replaced_targets, "应原子替换索引文件"
    assert str(tmp_path / "x.faiss.map.json") in replaced_targets, "应原子替换映射文件"
    assert (tmp_path / "x.faiss").exists()
    assert (tmp_path / "x.faiss.map.json").exists()

    # 临时文件应已被 replace 消费，无残留
    leftovers = list(tmp_path.glob("*.faiss.tmp*"))
    assert not leftovers, f"残留临时文件: {leftovers}"
