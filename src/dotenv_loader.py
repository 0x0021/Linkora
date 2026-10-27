""".env 加载入口（P2-1 步骤 3 / dotenv 搬迁专项）。

背景
----
``.env`` 的加载此前是 ``src/platform/base.py`` 的**导入副作用**
（``load_dotenv()`` 写在模块顶层）。这带来三个问题：

1. **语义错位**：``base.py`` 是被 18 处引用的底层模块，不是「项目入口」，
   但代码注释却写「在项目入口统一加载 .env」—— 实际加载时机取决于
   「谁先 import base.py」，是**隐式**的。
2. **Web 进程不加载**：``import web.api`` 不经过 ``src.platform.base``，
   因此 Web 侧**拿不到** ``.env`` 里的 ``LLM_API_KEY``（实测确认）。
3. **难以推理**：新增任何在导入期读环境变量的模块，都必须记得
   「base.py 会不会已经被导入过」。

本模块把加载动作显式化：

- ``main()`` **首行**显式调用 :func:`load_env` —— 入口语义正确；
- ``src/platform/base.py`` 仍保留一次**幂等兜底**调用 —— 因为
  ``src/memory/account_identity.py:38`` 等 3 处仍在**模块级**读环境变量
  （``DWS_BIN`` / ``LINKORA_AUDIT_LOG`` / ``ENABLE_SEARXNG``），
  这些模块可能在 ``main()`` 之前被导入。

为何保留兜底而非彻底移除
----------------------
``python-dotenv`` 的 ``load_dotenv()`` 本身**幂等**（不覆盖已存在的环境变量、
重复调用无副作用），故重复调用零成本、零行为差异。彻底移除会让
「在 ``main()`` 之前被导入的模块」拿不到注入后的环境变量 —— 这正是
``account_identity.py`` 读 ``DWS_BIN`` 的场景（一旦失效，钉钉账号隔离会
静默退化为 ``dingtalk:unknown``，per-account 隔离被破坏）。

为何不把 Web 进程也纳入
----------------------
Web 进程（``web/api.py``）当前**不加载** ``.env``，这是既有行为；
改变它会改变 Web 侧行为（可能让 Web 拿到本机密钥），属独立的决策，
不在本次搬迁范围内。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def load_env() -> bool:
    """加载项目根的 ``.env``（若存在）。

    幂等：``python-dotenv`` 的 ``load_dotenv()`` 不覆盖已存在的环境变量，
    重复调用无副作用。

    Returns:
        True = 成功执行（``.env`` 存在与否都算成功）；False = 加载过程出错
        （已记录日志，不影响启动 —— 密钥缺失会在后续使用时报更准确的错）。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
        return True
    except (OSError, ValueError) as exc:
        # .env 读取/解析失败属可预期故障（文件权限、编码、格式）。
        # **不**裸捕 Exception —— 宽异常门禁只减不增，且裸捕会掩盖真实 bug。
        logger.warning("[dotenv] .env 加载失败（已忽略）: %s", exc)
        return False


__all__ = ["load_env"]
