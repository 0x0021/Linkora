## 2026-10-26 (未发布)

> 自 v0.7.0 以来的变更

### 修复：管理台「用着用着突然报错」

登录令牌（JWT）有效期 24 小时，到期后页面不会自己发现——直到你点某个写操作按钮
（例如「死信队列 → 丢弃」）才突然失败，而失败提示是后端原样返回的英文代码
`unauthorized` / `not_found`，既看不出是登录过期还是记录已不存在。

- **令牌自动续期**：新增 `POST /api/auth/refresh`。前端在令牌剩余有效期不足 30 分钟时
  静默换新令牌；真的撞上 401 也会先尝试续期一次并重放原请求，续期不成才回到登录页。
  长时间开着管理台不再需要手动刷新页面。
- **失败提示说人话**：登录过期 → 「登录已过期，请重新登录后重试」；
  记录已不存在 → 「该记录已不存在（可能已被处理），请刷新列表」。
- **批量丢弃计数修正**：此前只看响应里有没有 `error` 字段，而 404 返回的是
  `{"detail":"not_found"}`（无 `error` 字段），会被**误计为成功**——出现过
  「完成 5/5」但实际一条都没删的情况。现改为判 `success`，并回报最后一条失败原因。
- **服务端补日志**：丢弃时未命中记录此前完全静默，现在会记录 id 与平台，便于事后定位。

### 修复：死信队列「丢弃 / 重放」点删除仍报 422 (Unprocessable Content)

上一轮以为根因是登录过期（401），但用户复现的是 **422**。真正原因在**前端打包产物**：

死信列表主循环变量 `item` 被 esbuild minify 重命名为 `u`，但重放 / 丢弃两个按钮
`data-args` 字符串里的 `item.id` **没被一起重命名**（esbuild 对 `\'` 转义单引号字符串
内的标识符有已知缺陷）。于是 `item` 在循环作用域里是 `undefined`，渲染出的 HTML 是
`data-args='["undefined", "@el"]'`，浏览器实际请求
`POST /api/dead-letters/undefined/discard`，FastAPI 按 `int` 校验路径参数失败 → 422。

- **前端**：两个按钮的 `data-args` 改用模板字面量 `${item.id}`（与上一轮修复 tabs 的
  `${st}` 同源，esbuild 会一并重命名）。重建 bundle 后验证 `item.` 全包计数为 0、
  按钮已用 `u.id`。
- **后端**：`discard` / `replay` 两个端点的 `dl_id` 由 `int` 改为 `str` 并显式 `int()`
  解析，非法 id 一律归为 `404 not_found`（前端翻译为「记录已不存在，请刷新列表」），
  彻底消除「看不懂的 422」——即便将来再有边角情况把非法 id 传进来也不会再暴露 422。
- **测试**：`tests/test_web_routers_dead_letters.py` 新增 `TestDiscardMalformedId`
  4 例，锁住 `undefined` / 文本 id → 404（非 422）回归，以及合法 id 以 int 传入 repo。

### 修复：CodeQL 中危告警「Information exposure through an exception」(#727)

`POST /api/auth/refresh` 在角色非法时把 `ValueError` 原文
（`detail=f"Invalid role: {e}"`）直接写回 401 响应，构成**异常信息泄露**。同一反模式
在令牌校验 `verify_token` 也存在（`detail=f"Token verification failed: {exc}"`）。

- **续期端点**（`web/api.py:802`）：捕获 `ValueError` 后仅把 `username/role/err` 记入
  服务端日志（经 `sanitize_log_message` 脱敏），响应改为固定中文文案
  「登录状态异常，请重新登录」，不再回显异常。
- **令牌校验**（`web/auth_middleware.py:141`）：`InvalidTokenError` 不再拼进 detail，
  改为固定英文文案 `Token verification failed`，异常仅 `logger.warning` 留痕。
- **未改动的同类点**：`dependencies.py` / `skills_marketplace.py` 里几处 `500` 的
  `detail=f"…{err}"` 看似同类，但全局 `http_exception_handler` 的 5xx 分支已把 detail
  替换为安全常量（`api.py:252`），真实异常只进日志、不进响应——运行期本就不泄露，
  故保留原样（改掉反而会丢掉服务端排查所需的错误文案）。
- **测试**：`tests/test_auth_refresh_endpoint.py` 新增 2 例回归，分别锁住「角色非法不回显
  异常原文」与「令牌校验失败不回显 pyjwt 异常类名」。

