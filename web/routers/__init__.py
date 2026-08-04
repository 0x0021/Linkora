"""Web API 路由子模块包。

将原本集中在 `web/api.py`（4400+ 行 / 92 路由）的“神模块”按业务域拆分到本包下的
独立 `APIRouter` 模块，降低单文件复杂度、提升可维护性。

挂载方式（避免循环导入）：
- 各 router 模块通过 `from web.api import <共享符号>` 复用 api.py 内的资源访问器
  （get_store / get_dws / get_app_instance / _get_cfg 等）与 pydantic 模型；
- `web/api.py` 在模块**末尾**（所有定义就绪后）`include_router(...)` 挂载本包子路由，
  此时 `web.api` 已部分加载且所需符号均已存在，不会触发循环导入。
"""
