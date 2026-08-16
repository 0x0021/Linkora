# main.py 拆分设计方案（2026-07-26）

> 状态：**已完成并验证**（2026-07-26）。实现与本文档略作调整：
> 原计划的 `context.py/throttle.py/title.py` 三个叶子模块并入 `base.py`（统一放置
> `PlatformContext` / `BackgroundLLMThrottle` / `extract_card_title` / `_active_platform_ctx`
> 及原 main.py 全部模块级 import），并新增 `core.py`（组合类）与 `__init__.py`（测试导出）。
> 验证：全量测试 **2173 passed, 2 skipped, 0 failed**（见文末「验证」）。

## 问题

`main.py` 单文件 3338 行，其中 `LinkoraEngine` 类占 161–3336 行（~3175 行）。
单文件导致：

1. 编辑冲突率高（多人/多分支并行时频繁 clash）。
2. `uvicorn --reload` 重启慢（每次改动重载整文件 + 重新 parse 3K 行）。
3. 可读性差——类职责混杂初始化、平台回调、回复逻辑、消息主循环、调度器、关闭。
4. 测试导入 `from main import LinkoraEngine` 形成隐性耦合。

## 约束

- **不能破坏现有测试**：14+ 个 test_*.py 直接 `from main import LinkoraEngine / PlatformContext / extract_card_title / BackgroundLLMThrottle / _active_platform_ctx`。
- **不能破坏 uvicorn --reload**：`--dev` 模式监视 `main.py` 及 `src/`、`web/`，拆后仍需触发重载。
- **不能引入循环依赖**：`PlatformContext` / `LinkoraEngine` 互相引用，需抽到无依赖的叶子模块。

## 目标结构（最终实现）

```
main.py                      # 兼容门面（71 行）：re-export 所有公开符号 + main() 入口 + stdlib 别名
                             #   （测试通过 main.signal / main.os / main.shutil 等 monkeypatch）
src/platform/
  __init__.py                # 导出 LinkoraEngine / PlatformContext / BackgroundLLMThrottle /
                             #   extract_card_title / _active_platform_ctx 供测试
  base.py                    # 原 main.py 全部模块级 import + PlatformContext /
                             #   BackgroundLLMThrottle / extract_card_title / _active_platform_ctx
  core.py                    # LinkoraEngine 组合类（仅 class 声明，继承 5 个 mixin）
  primary.py                 # PrimaryMixin: __init__ + _init_* 初始化链（13 法）
  runtime.py                 # RuntimeMixin: 平台上下文/属性/配置热重载/回复发送/风格画像/
                             #   草稿/引用/限流/死信（44 法）
  message_loop.py            # MessageLoopMixin: 消息主循环/debounce/backpressure/轮询状态（11 法）
  memory.py                  # MemoryMixin: 自动记忆保存 + 4 类清理调度器（5 法）
  lifecycle.py               # LifecycleMixin: shutdown / run + 模块级 _start_dev_watcher / main（2 法）
```

> 拆分方式：基于 AST 精确提取每个方法源码区间（含装饰器），71 个原类顶层方法
> 与拆分后完全 1:1 对齐（无丢失、无重复、缩进正确）。所有 mixin 通过
> `from .base import *` 共享原 main.py 的模块级符号。
>
> **star-import 注意**：因 mixin 用 `from .base import *`，符号在各自模块形成副本；
> 测试若需 mock 这些依赖（如 SQLiteStore/LLMAgent/MessagePoller/load_config），
> 必须 `patch.object(src.platform.primary / runtime, ...)` 而非 `patch.object(main, ...)`。
> 已修正 `tests/test_platform_runtime.py` 的 3 处 patch 目标。

if __name__ == "__main__":
    main()
```

测试 `from main import LinkoraEngine` 仍可用（门面 re-export）。

## 实际执行方式（2026-07-26 已完成）

采用**一次性 AST 精确切分**脚本（`python3` 基于 `ast` 提取每个方法源码区间，
含装饰器与 docstring，缩进自动正确），而非文档原计划的逐文件手工小 commit。
原因：手工切分易因缩进/装饰器推断错误产生语法破坏（已踩坑一次，已改为 AST 法）。

执行结果：71 个原类顶层方法 1:1 落入 5 个 mixin（primary 13 / runtime 44 /
message_loop 11 / memory 5 / lifecycle 2），类级属性（`_metrics_lock` /
`_INCOMPLETE_STRUCT_RE` / `_INCOMPLETE_REQUEST_VERBS`）随 primary；模块级函数
（`_start_dev_watcher` / `main`）入 lifecycle。

## 验证记录（2026-07-26）

- 切分脚本 AST 校验：原类 71 顶层方法 vs 拆分后 71，MISSING=none / EXTRA=none。
- import 实测：`from main import LinkoraEngine, PlatformContext, BackgroundLLMThrottle,
  extract_card_title, _active_platform_ctx` 全部可用；MRO 正确
  `[LinkoraEngine → PrimaryMixin → RuntimeMixin → MessageLoopMixin → MemoryMixin → LifecycleMixin → object]`。
- 全量测试：**2173 passed, 2 skipped, 0 failed**（耗时 ~227s）。
- 修复的测试回归：`tests/test_platform_runtime.py` 中 3 处
  `patch.object(main_mod, X)` 因 star-import 副本失效，已改为
  `patch.object(src.platform.primary / runtime, X)`。
- `python main.py --dev` 启动路径保留（门面 `if __name__ == "__main__": main()`）。

## 风险与对策

| 风险 | 对策 |
|---|---|
| 切分引入循环依赖 | 叶子模块先抽；跨模块引用用 TYPE_CHECKING + 字符串注解 |
| uvicorn --reload 不触发 | 保留 `main.py` 在 watch_paths；拆分后 main.py 仍 import 改动模块 |
| 测试 import 断 | 门面 re-export 全部公开符号；每步跑 pytest 验证 |
| 行为漂移（非纯移动） | 仅做「物理移动」，不改任何逻辑/顺序；diff 应只有 import 行变化 |

## 验收

- ~~`pytest` 全绿（2173 passed 基线）~~ **已完成：2173 passed, 2 skipped, 0 failed。**
- ~~`python main.py --dev` 正常启动~~ **已完成：真实环境 `--dev` 运行验证通过**
  （PID 32449 实例 :8080 返回 200，三平台 poller 正常工作，基于拆分后门面代码）。
- `ruff check .` 无新增 E/F 级错误（待跑，CI 已有语法门）。
- 单文件行数：main.py 71，runtime.py 1591（最大），其余均 < 500。

## 拆分后修复的关联 bug

### 1. 隐藏循环依赖（commit 9154c37，已 merge）
`lifecycle.main()` 与 `runtime._is_style_profile_stale` 引用 `LinkoraEngine` 自身类名，
但 `LinkoraEngine` 定义在 `core.py`、`core` 继承 `LifecycleMixin`，模块级 import 会循环。
- `runtime.py`：`_is_style_profile_stale` `@staticmethod` → `@classmethod`，`LinkoraEngine.xxx` → `cls.xxx`
- `lifecycle.py`：`main()` 内延迟 `from .core import LinkoraEngine`

### 2. PID 文件路径漂移（commit 见下）
`lifecycle.main()` 用 `os.path.join(os.path.dirname(__file__), "data", "linkora.pid")` 定位 PID 文件。
refactor 前 `__file__` 是仓库根 `main.py`，拆分后变成 `src/platform/lifecycle.py`，
导致 PID 文件被写到 `src/platform/data/linkora.pid`，与配置中 `./data/*.db`（基于 cwd 根）
**锚点不一致**，且旧实例（根目录启动）写根的 `./data/linkora.pid`、新代码写 `src/platform/data/`，
造成 PID 锁失效 / 双实例风险。
- 修复：`main.py` 门面传入 `PROJECT_ROOT`，`lifecycle.main(root=None)` 将 PID 锚定到 `root/data/linkora.pid`
  （与 DB 约定一致）；兜底用 `parents[2]` 推导仓库根。已清理漂移的 `src/platform/data/linkora.pid`。

> **同类风险提醒**：配置里 `./data/*.db` 基于进程 cwd 解析，未随 `__file__` 漂移（DB 用 cwd、
> PID 现用 root，二者一致前提是「从仓库根启动 `--dev`」）。若未来有人从非根 cwd 启动，
> DB 仍会写到该 cwd 的 `./data`，属历史设计约定，本次不扩大改动范围。

## 不做（本次范围外）

- 不重命名 LinkoraEngine（保留类名，避免测试大改）。
- 不抽 web/ routers（已有 31 个独立 router，结构健康）。
- 不重构 src/ 内部模块（本方案只动 main.py 扁平巨石）。
