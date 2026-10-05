"""写路径闸门收口的**结构性**回归测试（P0 2026-10-05 事故第二轮）。

## 事故经过（这是本文档存在的理由）

第一轮修复只把 ``conversation_repo`` 的 3 个高频写方法包进了 ``write_with_retry``，
**漏掉了 20 个写方法中的 17 个**。坤哥手动重启为单进程后，日志立刻又报：

    10:52:11 [消息清理] 平台 dingtalk 定时清理失败: database is locked
    10:52:56 [SQLite] 写入闸门等待 60s 超时（写堆积过多），本次放行

即「已加闸门」不等于「写路径都走了闸门」。真正的教训是**收口要结构性完成**，
而不是挑几个高频点手改——手改必然漏。

## 本组用例的作用

用 AST 静态扫描**全仓写方法**，断言每个含 INSERT/UPDATE/DELETE 的方法都满足
「经过 ``_write`` 或 ``store._lock`` 之一」。这样新增写方法若绕过闸门，
测试立刻失败，而不是等生产事故。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须收口的 repo 文件（会话库写入层）
REPO_FILES = (
    "src/memory/message_repo.py",
    "src/memory/conversation_repo.py",
    # 【P0 2026-10-05 第二轮】blacklist/external_friend 属**会话数据**
    # （blocked_conversations / external_friends 与 messages 同库），
    # 此前 7 处写入全部裸写绕过闸门 → 闸门显示「无持有者」但写库仍
    # database is locked（这是排障时最关键的证据，见 _write_gate_current_holder）。
    "src/memory/blacklist_repo.py",
    "src/memory/external_friend_repo.py",
)

WRITE_SQL = ("INSERT ", "UPDATE ", "DELETE ")


def _iter_write_methods(path: Path):
    """产出 (行号, 方法名, 源码段)。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = ast.get_source_segment(src, node) or ""
        if not any(kw in body for kw in WRITE_SQL):
            continue
        # 纯只读误报过滤：需真的出现写关键字
        if not any(body.count(kw) > 0 for kw in WRITE_SQL):
            continue
        yield node.lineno, node.name, body


def _is_gated(body: str) -> bool:
    """该写方法是否经过闸门。

    四种合规写法都算过闸：
      - ``self._write(_do_write)``         —— repo 的统一入口（首选）
      - ``self._write_result(_do_write)`` —— 同上，但返回写入结果
      - ``self.store.write_with_retry(...)`` —— 直接调 store 层
      - ``with self.store._lock:``         —— 复合锁已内嵌全局闸门
    """
    return (
        "self._write(" in body
        or "self._write_result(" in body
        or "self.store.write_with_retry" in body
        or "store._lock" in body
    )


@pytest.mark.parametrize("rel", REPO_FILES)
def test_all_write_methods_are_gated(rel):
    """结构性断言：所有写方法都必须经过闸门。"""
    path = ROOT / rel
    assert path.exists(), f"文件不存在: {rel}"
    offenders: list[str] = []
    total = 0
    for lineno, name, body in _iter_write_methods(path):
        # 内部辅助函数（下划线开头）由外层方法包住闸门，不单独要求
        if name.startswith("_") and "self._write" not in body:
            continue
        total += 1
        if not _is_gated(body):
            offenders.append(f"  L{lineno} {name}")
    assert not offenders, (
        f"{rel} 有 {len(offenders)}/{total} 个写方法未经过写闸门：\n"
        + "\n".join(offenders)
        + "\n修复：把方法体抽成 _do_write(conn) 并用 self._write(_do_write) 包裹；"
        " 或确认它已在 store._lock 块内。"
    )


@pytest.mark.parametrize("rel", REPO_FILES)
def test_repos_have_write_helper(rel):
    """两个 repo 都必须提供 _write 统一入口。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "def _write(self" in src, f"{rel} 缺少 _write 统一入口"


def test_store_lock_is_gated():
    """store._lock 必须是复合锁（实例 RLock + 进程内全局闸门）。

    ⚠️ 这条是本次事故的核心修复：全仓 15 处写路径用 ``with store._lock:``，
    逐个改极易漏（第一轮就漏了 17 个）。把闸门叠在锁本身，一处改动覆盖全部。
    """
    from src.memory.sqlite_store import _GatedWriteLock

    assert _GatedWriteLock is not None
    # 复合锁必须暴露 RLock 兼容接口
    for attr in ("__enter__", "__exit__", "acquire", "release"):
        assert hasattr(_GatedWriteLock, attr), f"_GatedWriteLock 缺少 {attr}"


def test_gated_lock_behaves_like_rlock():
    """复合锁必须可重入（write_with_retry 内部会再取闸门）。"""
    from src.memory.sqlite_store import _GatedWriteLock

    lock = _GatedWriteLock()
    with lock:
        with lock:
            pass
    # 退出后可再次获取
    with lock:
        pass


def test_gated_lock_releases_on_exception():
    """异常时必须释放两把锁，否则整个服务写路径永久阻塞。"""
    from src.memory.sqlite_store import _GatedWriteLock

    lock = _GatedWriteLock()
    with pytest.raises(ValueError, match="boom"):
        with lock:
            raise ValueError("boom")
    with lock:  # 能立即进入即说明已释放
        pass


def test_gated_lock_serializes_threads():
    """复合锁必须跨线程串行（SQLite 同时只允许一个写者）。

    实现要点：计数器用**闸门内**递增（保证不被并发写坏），读最大值也在
    闸门内取。用「临界区内 +1/-1」判定是否交叉，比记录 order 列表更简洁，
    也避免测试自身的锁顺序问题（曾经用外部 guard 锁导致测试假死 60s）。
    """
    import threading as _th
    import time

    from src.memory.sqlite_store import _GatedWriteLock

    lock = _GatedWriteLock()
    state = {"cur": 0, "max": 0}

    def writer(_i: int) -> None:
        for _ in range(5):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
                time.sleep(0.005)
                state["cur"] -= 1

    threads = [_th.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "写闸门卡死（线程未能退出）——存在死锁"

    assert state["cur"] == 0, "临界区计数未归零"
    assert state["max"] == 1, f"闸门未串行化，最大并发={state['max']}"


def test_gated_lock_shared_across_instances():
    """两个 store 实例的锁必须共享同一把全局闸门（否则跨实例仍会撞锁）。"""
    import threading as _th
    import time

    from src.memory.sqlite_store import _GatedWriteLock

    a, b = _GatedWriteLock(), _GatedWriteLock()
    state = {"cur": 0, "max": 0}

    def writer(i: int) -> None:
        lock = a if i % 2 else b
        for _ in range(5):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
                time.sleep(0.005)
                state["cur"] -= 1

    threads = [_th.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "跨实例写闸门卡死"

    assert state["max"] == 1, f"跨实例未共享闸门，最大并发={state['max']}"


class TestNoDiskIoInsideTransaction:
    """磁盘 IO 绝不能发生在 SQLite 写事务内（会长时间霸占写锁）。

    现场：``cleanup_old_messages`` 在 ``DELETE`` + ``commit`` **之前**调用
    ``purge_orphan_images``（扫描并 unlink 文件），整个磁盘操作期间写锁一直持有；
    ``delete_message`` 同样如此。两者都曾直接抛 database is locked。
    """

    @pytest.mark.parametrize("method", ["cleanup_old_messages", "delete_message"])
    def test_purge_orphan_images_not_inside_lock_block(self, method):
        src = (ROOT / "src/memory/message_repo.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == method:
                target = node
                break
        assert target is not None, f"未找到 {method}"

        # 找出所有 `with self.store._lock:` 块，检查块内是否调用了 purge_orphan_images
        for inner in ast.walk(target):
            if not isinstance(inner, (ast.With, ast.AsyncWith)):
                continue
            for item in inner.items:
                ctx = ast.unparse(item.context_expr)
                if "_lock" not in ctx:
                    continue
                block_src = ast.unparse(inner)
                assert "purge_orphan_images" not in block_src, (
                    f"{method}: purge_orphan_images 在 `with {ctx}` 块内执行 —— "
                    "磁盘 IO 必须在 commit 且释放写锁之后进行"
                )
