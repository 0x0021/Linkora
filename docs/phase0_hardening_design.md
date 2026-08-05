# Linkora（灵桥）Phase 0 安全与稳定性加固 — 增量设计

> 文档性质：**只读代码实读 + 增量设计**，不改任何源码。
> 项目根：`/Users/user/Documents/Linkora`｜Python 3.14｜虚拟环境 `.venv/`
> 基准审计报告：`docs/audit/project_audit_2026-07-14.md`（以下简称「审计报告」）

---

## 0. 重要前置结论（先读）

**实读发现：审计报告（2026-07-14）已显著落后于代码现状。** ①/②/③ 三项 P0 在代码层面**均已落地**，④ 的锁文件已存在、`pip-audit` 已在 CI，⑤ 的「重量对象单例 + per-thread 连接」也已落地。

因此本设计文档的定位不是「从零实现这 5 项」，而是：

1. **对齐审计**：逐条标注「代码已满足 / 部分满足 / 仍有缺口」。
2. **补全真实缺口**：审计未覆盖、但实际代码里仍存在的安全/稳定性隐患（如下文「★真实缺口」）。
3. **给出严谨落地方案**：以 `file:line` 证据为准，明确改哪里、改什么、零行为变更约束。

下表为五项的实读结论速览（细节见各节）：

| 项 | 审计描述 | 实读结论 | 仍需做 |
|----|---------|---------|--------|
| ① auth 加固 | 默认关 / 非恒定时间 / 无限流 | **默认已 True**、**已恒定时间(hmac)**、限流**已存在(仅 IP)** | ★空密码无启动强校验；限流未含账号维度；死代码 `_AUTH_BLOCK_SECONDS` |
| ② 绑定 127.0.0.1 | 绑定 0.0.0.0 | **默认已 127.0.0.1**、`web.host` 可显式设 0.0.0.0 | 仅补充 0.0.0.0+auth 关时的告警日志（可选） |
| ③ SSRF 防护 | import-url 重定向未重校验 | **已逐跳重校验** + **Playwright 路由拦截** | is_ssrf_safe 未拒 `is_unspecified/is_reserved`（0.0.0.0 放行） |
| ④ 依赖锁 + pip-audit | 全 >= 无锁 | **requirements.lock 已存在**、**CI 已跑 pip-audit** | ★CI 装的是 `requirements.txt`(未锁)、pip-audit 扫的是未锁文件 → 锁未在 CI 生效 |
| ⑤ 消除每请求重建 | 每请求 new EmbeddingClient/Store/faiss | **embedding 单例**、**get_store per-thread 缓存**、**faiss `_with_index_lock`** 均已完成 | ★`async def` 路由内同步 SQLite 仍阻塞事件循环，需 `run_in_threadpool` |

---

## 1. 审计报告对齐（5 项原始描述 vs 实读）

### ① auth 加固（审计：Critical — `auth_enabled=False` / 非恒定时间 / 无限流）
- 审计原文：`src/config.py:423 auth_enabled=False`、`api.py:122` 非恒定时间、`hmac.compare_digest` + IP 失败计数限流（建议）。
- **实读一致/出入**：
  - 默认 `auth_enabled` 当前为 **`True`**（`src/config.py:597`），与审计「默认 False」**有出入**——此项已修复。
  - 恒定时间比对**已实现**，位于 `web/api.py:222-227`，但用的是 `hmac.compare_digest`（审计建议 `secrets.compare_digest`；两者等价，均恒定时间，见「待明确事项」）。
  - 登录限流**已实现**（`web/api.py:154-219`），内存滑动窗口，阈值 `5 次 / 300s`。但**只按 IP 限流，未含账号维度**（用户要求「同 IP/账号」）。且 `_AUTH_BLOCK_SECONDS=300.0`（`web/api.py:160`）**已定义但从未被引用**（死代码）——当前封锁仅作用于「窗口内剩余时间」，无独立封锁惩罚时长。
  - **★真实缺口（审计未提）**：`config.py:599-601` 注释声称「`auth_enabled=True` 且密码为空则启动强制报错」，但**代码无此校验**。后果：`auth_password=""` 时 `_auth_check` 比较 `compare_digest("", "")==True`，任意请求用空密码即可通过认证 → 裸奔。需在 `load_config` 或 `main.py` 启动路径补强制校验。

### ② Web 绑定 127.0.0.1（审计：Critical — `host="0.0.0.0"`）
- 审计原文：`web/api.py:3898 host="0.0.0.0"`，建议默认 127.0.0.1。
- **实读出入**：当前 `WebConfig.host` 默认 **`"127.0.0.1"`**（`src/config.py:594`）；`run_web` 优先级为「显式参数 > `WEB_HOST` 环境变量 > `config.web.host` > 回退 `127.0.0.1`」（`web/api.py:682-714`）。**已加固**。配置项 `web.host` 在 `config.yaml.example:387` 有明确注释。`0.0.0.0` 仍可作为显式值（符合用户「保留开关」要求）。

### ③ SSRF 防护（审计：High — import-url 重定向绕过 / Playwright 无校验）
- 审计原文：`api.py:1620` 跟随重定向、`api.py:1658` Playwright `page.goto` 无校验。
- **实读一致（已修复）**：
  - 统一校验函数 `is_ssrf_safe` 位于 `web/security.py:12-39`（协议白名单 + 解析所有 IP 禁私网/回环/链路本地）。
  - import-url 入口校验 `web/routers/kb.py:201`；`allow_redirects=False` 后**逐跳重校验** `web/routers/kb.py:222-235`（最多 5 跳）。
  - Playwright 路由拦截 `web/routers/kb.py:273-278`，对内网/保留地址 `route.abort()`。
  - 另：skillhub 安装源白名单 + SHA256 钉值（`web/dependencies.py:217-238`），供应链 SSRF/RCE 已防护。
  - **★小缺口**：`is_ssrf_safe` 仅拒 `is_private/is_loopback/is_link_local`，**未拒 `is_unspecified`（0.0.0.0 / ::）、`is_reserved`、`is_multicast`**。实测 `ipaddress.ip_address("0.0.0.0").is_unspecified==True` 但不会被上述三条件拦截 → `http://0.0.0.0:xxx` 可通过校验。需补拒。

### ④ 依赖锁 + pip-audit（审计：Med — 全 >= 无锁）
- 审计原文：`requirements.txt` 全 `>=`；建议 `pip freeze > requirements.lock` + CI `pip-audit`。
- **实读**：
  - `requirements.lock` **已存在**（精确版本，如 `httpx==0.28.1`、`urllib3==2.7.0`、`huggingface_hub==1.23.0`）。
  - CI（`.github/workflows/ci.yml:51-72`）：安装用 `pip install -r requirements.txt`（**未锁版**），`pip-audit -r requirements.txt`（**报告型、不阻断**）。
  - **★真实缺口**：锁文件已生成却**未在 CI 生效**——CI 装的是未锁的 `requirements.txt`，`pip-audit` 扫的也是未锁文件。供应链可复现性与漏洞扫描准确性未达标。需改 CI 装 `requirements.lock`、扫 `requirements.lock`。

### ⑤ 消除每请求重建重量对象（审计：High — 每请求 new EmbeddingClient/Store/faiss + 同步阻塞事件循环）
- 审计原文：`api.py:1488/1780...` 每请求 new `EmbeddingClient`；`get_store()` 每请求 new Store + faiss 全量重读；`async def` 内直接 `cur.execute` 阻塞事件循环。
- **实读（大量已修复）**：
  - `EmbeddingClient` **已是单例**：`_get_embedding_client` 按 `(model, provider, offline)` 缓存（`web/api.py:526-543`）。
  - `get_store` **已是 per-thread 缓存**：`web/dependencies.py:65-126`，`threading.local()` 存各平台 Store 实例，`init_db` 仅一次，向量索引随 DB 变化失效（廉价 COUNT 校验）。
  - `SQLiteStore.conn` **已是 per-thread 连接**：`src/memory/sqlite_store.py:505-559`，`self._conns[tid]` 懒创建 + WAL + `busy_timeout` + `init_db` 在锁外执行防死锁。
  - faiss 索引 **已用 `_with_index_lock`**（模块级 RLock 装饰器，`src/memory/index_lock.py:15`），`@_with_index_lock` 包裹 faiss 读写（`sqlite_store.py:316, 885` 等）。
  - **★真实缺口（仍存）**：**`async def` 路由内直接跑同步 SQLite（`store.conn.cursor()`/`cur.execute`）会阻塞事件循环**。确认案例：`web/routers/status.py:52` `async def status()` 内 `store.conn.cursor()`（`:56`）。同步 `def` 路由由 Starlette 自动丢进线程池（不阻塞），故问题集中在 `async def` 路由。需对 `async def` 路由内的阻塞 DB/faiss 调用包 `await run_in_threadpool(...)`。
  - 次要：`get_dws()` 每次 new `DwsAdapter`（`web/api.py:572-578`）——轻量 CLI 包装，风险低，建议按平台缓存（可选）。

---

## 2. 各项现状实读（file:line + 片段）＋ 改动方案

### ① auth 加固
**现状证据**
- 默认值：`src/config.py:597` `auth_enabled: bool = True`
- 恒定时间：`web/api.py:222-227`
  ```python
  def _auth_check(username, password, cfg):
      expected_u = (cfg.web.auth_username or "").encode("utf-8")
      expected_p = (cfg.web.auth_password or "").encode("utf-8")
      return hmac.compare_digest(username.encode("utf-8"), expected_u) and \
             hmac.compare_digest(password.encode("utf-8"), expected_p)
  ```
- 限流（IP 维度）：`web/api.py:154-219`（`_AUTH_FAILS` / `_auth_rate_allowed` / `_auth_record_fail`）；阈值 `_AUTH_MAX_FAILS=5`、`_AUTH_FAIL_WINDOW=300.0`、`_AUTH_BLOCK_SECONDS=300.0`（死代码，未被引用）。
- 空密码强校验缺失：注释在 `src/config.py:599-601`，但 `load_config`（`src/config.py:806-851`）与 `main.py` 均无对应 `raise`；`_apply_env_overrides` / `validate_config_keys` 也未校验密码非空。

**改动方案**
1. **补空密码启动强校验（★必须）**
   - 在 `src/config.py` 的 `WebConfig` 上加 `model_validator(mode="after")`（或 `load_config` 末尾校验）：若 `auth_enabled is True and not auth_password.strip()` → `raise ValueError("auth_enabled=True 但 auth_password 为空，拒绝启动（安全默认）")`。
   - 同步在 `main.py` 启动路径 `load_config()` 调用处捕获该异常并 `sys.exit(1)` 打印明确提示。
   - 零行为变更：仅当「开认证且密码为空」这一**不安全配置**才报错；正常部署无影响。
   - 注意与既有「config API 写入时空密码保留旧值」逻辑（`web/routers/config.py:349-355`）不冲突：写接口禁止清空密码，启动校验禁止本就为空，二者互补。
2. **限流加账号维度（推荐）**：在 `_AUTH_FAILS` 之外，以 `(ip, username)` 组合计数（用户名取自解析出的 creds）。复用现有滑动窗口逻辑，阈值不变。
3. **激活 `_AUTH_BLOCK_SECONDS`（推荐）**：超过阈值后，除窗口内封锁外，额外将封锁截止时间设为 `now + _AUTH_BLOCK_SECONDS`，存入 `_AUTH_FAILS[ip]` 的标记；下次 `_auth_rate_allowed` 比对 `now < block_until`。避免「窗口刚过即可立即再爆破」。
4. `hmac.compare_digest` vs `secrets.compare_digest`：两者均恒定时间。保持 `hmac` 即可（见待明确事项）。

### ② Web 绑定 127.0.0.1
**现状证据**：`src/config.py:594` 默认 `"127.0.0.1"`；`web/api.py:682-714` 优先级 `参数 > WEB_HOST > config.web.host > 127.0.0.1`；`config.yaml.example:387` 文档化。
**改动方案**：**默认与开关已满足，无需改功能**。可选增强：`run_web` 在最终 `host=="0.0.0.0"` 且 `auth_enabled is False` 时打印一行 `WARNING` 日志（提示公网裸奔风险）。不阻断启动。

### ③ SSRF 防护
**现状证据**：`web/security.py:12-39` `is_ssrf_safe`；`web/routers/kb.py:201` 入口、`222-235` 逐跳、`273-278` Playwright 拦截。
**改动方案**
1. **扩展 `is_ssrf_safe` 拒绝范围（★推荐）**：在 `web/security.py:37` 的拒绝条件增加 `ip.is_unspecified or ip.is_reserved or ip.is_multicast`。覆盖 `0.0.0.0`、`::`、保留段、组播，堵住当前放行漏洞。
2. **DNS 重绑定（TOCTOU）残留风险（记录，非阻塞）**：`is_ssrf_safe` 先解析 DNS 校验，随后 `requests.get` 连接时再次解析，存在时间窗。Phase 0 建议：在 import-url 流程里，校验通过后**用已校验的 IP 直接连接**（自定义 `socket`/HTTPAdapter 钉 IP），或至少保留现有「先解析再请求」的双解析且请求侧用 `requests` 默认。鉴于改造面与行为变更风险，建议作为 P1 跟进，Phase 0 先补范围拒绝 + 文档标注残留风险。
3. 其余 URL 入口复核：`web/dependencies.py:241-249` `_download_to_file` 仅用于 skillhub 白名单主机（已安全）；未发现其他任意 URL 拉取。维持现状。

### ④ 依赖锁 + pip-audit
**现状证据**：`requirements.lock` 存在（精确版本）；`.github/workflows/ci.yml:51-72` 安装 `requirements.txt`、扫描 `requirements.txt`。
**改动方案**
1. **CI 改装锁文件（★必须）**：`ci.yml:54` 改为 `pip install -r requirements.lock`；`ci.yml:72` 改为 `pip-audit -r requirements.lock`（仍 `continue-on-error: true`）。保证 CI 复现与扫描基于锁定版本。
2. **锁文件维护流程**：新增脚本 `scripts/lock_deps.sh`（或 Makefile target）：`python -m pip install -U pip && pip install -r requirements.txt && pip freeze > requirements.lock`。要求「改 `requirements.txt` 后必须重新生成 `requirements.lock` 并提交」写入 `DEV_GUIDE.md`/`CONTRIBUTING`。
3. 不引入新运行期依赖；`pip-audit` 仅 CI/dev 用。
4. 可选：将 `huggingface_hub==1.23.0` 等已知敏感版本在 `requirements.lock` 锁定（审计已建议），当前 `requirements.lock` 已锁 `1.23.0`，维持。

### ⑤ 消除每请求重建 + 事件循环阻塞
**现状证据（已落地，勿破坏）**
- Embedding 单例：`web/api.py:526-543`
- Store per-thread 缓存：`web/dependencies.py:65-126`
- `SQLiteStore.conn` per-thread：`src/memory/sqlite_store.py:505-559`
- faiss `_with_index_lock`：`src/memory/index_lock.py:15` + `sqlite_store.py` 多处 `@_with_index_lock`
- **阻塞残留**：`web/routers/status.py:52-72` `async def status(): store.conn.cursor()...`（确认案例）；`conversations.py`/`keywords.py`/`stats.py`/`memories.py`/`routing_quality.py`/`decisions.py`/`health.py`/`metrics.py`/`persona.py` 中亦有 `async def` 路由内 `store.conn.cursor()`/`cur.execute`（需逐文件确认是否 async）。

**改动方案（核心：不破坏 per-thread 连接 / faiss RLock）**
1. **识别**：对所有 `web/routers/*.py` 与 `web/api.py`，列出 `async def` 路由中直接调用 `store.conn` / `cur.execute` / faiss 检索 / `EmbeddingClient` 推理的语句（grep 模式：`async def` 与 `store\.conn|cur\.execute|\.execute\(|_vector_index|_get_embedding_client` 同文件）。同步 `def` 路由由 Starlette 自动线程池化，**不用改**。
2. **包裹**：将上述阻塞调用包成 `await run_in_threadpool(...)`（从 `fastapi.concurrency` 导入，底层 anyio 线程池）。示例（status.py）：
   ```python
   from fastapi.concurrency import run_in_threadpool
   @router.get("/api/status")
   async def status():
       store = _api.get_store()
       def _work():
           cur = store.conn.cursor()
           cur.execute("SELECT COUNT(*) FROM messages")
           return cur.fetchone()[0]
       msg_count = await run_in_threadpool(_work)
       ...
   ```
3. **与 per-thread 连接的交互（关键不变量）**：`run_in_threadpool` 把 `_work` 丢到 anyio 线程池线程执行；该线程访问 `store.conn` 时走 `SQLiteStore.conn` 的 `self._conns[tid]`（threading.local）→ **拿到的是该池线程自己的连接**，与事件循环线程/其他请求的连接互不共享，无跨线程 sqlite3.Connection 共享（满足硬约束）。Store 实例本身跨线程共享（单例级），但内部连接 per-thread，安全。
4. **连接泄漏边界**：`get_store` 在线程池线程首次调用会新建 per-thread 连接并缓存于 `_store_local`（threading.local）；线程池线程复用时连接复用，线程退出时连接随线程 GC；`SQLiteStore` 另有 `_max_conns` 淘汰（`sqlite_store.py:542-549`）防 FD 泄漏。无需额外清理。
5. **faiss 单例 + `_with_index_lock` 配合**：多个线程池线程并发触发 faiss 检索/写入时，`@_with_index_lock`（模块级 RLock）串行化，避免 faiss 索引并发读写崩溃；单例 Store 共享同一 faiss 索引对象，RLock 保证线程安全。无需改。
6. **`get_dws()` 缓存（可选）**：`web/api.py:572-578` 改为按 platform 缓存 `DwsAdapter` 实例（字典 + 锁），与 `get_store` 对齐。低风险增强。
7. **测试保护**：1900+ 测试为安全网；包裹 `run_in_threadpool` 不改变返回类型/语义，仅换执行线程，测试应无感。需确保测试中直接 `get_store()` 仍可用（不变）。

---

## 3. 文件列表（新增/修改，相对项目根）

**修改**
- `src/config.py` — `WebConfig` 加空密码 `model_validator`（①）；`is_ssrf_safe` 不在此文件。
- `web/api.py` — 激活 `_AUTH_BLOCK_SECONDS` 封锁逻辑（①）；`get_dws()` 缓存（⑤可选）；`0.0.0.0` 告警日志（②可选）。
- `web/security.py` — `is_ssrf_safe` 扩大拒绝范围（③）。
- `web/routers/kb.py` — 无功能改动（确认已满足）；如需钉 IP 防 DNS 重绑定则在此（③ P1）。
- `web/routers/status.py`、`conversations.py`、`keywords.py`、`stats.py`、`memories.py`、`routing_quality.py`、`decisions.py`、`health.py`、`metrics.py`、`persona.py` — `async def` 路由内阻塞调用包 `run_in_threadpool`（⑤）。
- `main.py` — 启动捕获空密码校验异常并退出（①）。
- `.github/workflows/ci.yml` — 装 `requirements.lock`、扫 `requirements.lock`（④）。
- `config.yaml.example` — 已含 `web.host/auth_*` 文档，无需改（②）。

**新增**
- `scripts/lock_deps.sh`（或 Makefile target）— 生成 `requirements.lock`（④）。
- （可选）`tests/test_phase0_security.py` — 补空密码启动校验、`is_ssrf_safe` 对 `0.0.0.0` 拒绝、限流账号维度的单测。

**不修改**：`src/memory/sqlite_store.py`（per-thread 连接已正确）、`src/memory/index_lock.py`（已正确）。

---

## 4. 接口 / 中间件 / 配置字段改动点

- **配置字段（已存在，零新增）**：`web.auth_enabled`(默认 True)、`web.auth_username`(admin)、`web.auth_password`(空→启动报错)、`web.host`(默认 127.0.0.1)。无需新增字段。
- **中间件**：`web_auth_middleware`（`web/api.py:277`）逻辑不变；仅内部限流状态机增强（账号维度 + block 时长）。
- **接口行为**：所有接口**输入输出不变**；唯一外部可见变化：① 空密码+开认证时进程启动失败（错误日志）；③ `http://0.0.0.0:port` 类 URL 在 import-url 被拒。
- **环境变量**：`WEB_HOST`（已存在，②）；`SKILLHUB_INSTALL_SHA256`（已存在，③供应链）。
- **新增脚本接口**：`scripts/lock_deps.sh`（开发者用，非运行时）。

---

## 5. 关键流程

### 5.1 ⑤ 单例 + run_in_threadpool + per-thread 连接 的竞态边界
```
请求 → 事件循环线程
  └─ async def 路由
       └─ await run_in_threadpool(_work)   # anyio 线程池（线程 Tp）
            └─ _work():
                 store = get_store()        # 共享 Store 实例（单例级）
                 conn  = store.conn         # ★ 走 SQLiteStore.conn 的 _conns[get_ident()]
                                             #   → 线程 Tp 自己的连接（threading.local）
                 cur.execute(...)           # 在该连接上同步执行
                 # faiss 检索若触发：@_with_index_lock（模块级 RLock）串行化
            # _work 返回，Tp 连接缓存于 _store_local，复用
```
- **不变量**：Store 实例共享，但 `sqlite3.Connection` 绝不跨线程（threading.local）；`run_in_threadpool` 的池线程各自持独立连接，零跨线程 Connection 共享竞态。
- **无泄漏**：池线程复用连接；`SQLiteStore._max_conns` 淘汰最旧连接防 FD 增长。
- **faiss**：单例索引 + 模块级 RLock，多池线程并发安全。
- **边界**：不要在 `run_in_threadpool` 外持有 `store.conn` 跨 `await`；当前代码均符合（连接仅在同步 `_work` 内使用）。

### 5.2 ③ 重定向逐跳重校验流程
```
import_kb_from_url(url)
  ├─ is_ssrf_safe(url)? 否→400          (kb.py:201)
  ├─ requests.get(allow_redirects=False) (kb.py:219)
  └─ while resp.is_redirect and redirects<5:
       next_url = urljoin(url, Location)
       is_ssrf_safe(next_url)? 否→400    (kb.py:229)  ★ 每跳重校验
       requests.get(next_url, allow_redirects=False)
  └─ 正文过少 → Playwright:
       page.route("**/*", _abort_internal_route)  (kb.py:283)
         └─ is_ssrf_safe(route.request.url)? 否→route.abort()  (kb.py:276)
```
- 增强：将 `is_ssrf_safe` 的拒绝集扩到 `is_unspecified/is_reserved/is_multicast`，使 `0.0.0.0/::` 在入口与每跳均被拒。

---

## 6. 任务列表（有序 + 依赖，低风险先、高风险后）

> 原则：**①②③④ 低风险先落地；⑤ 高风险最后**（涉及事件循环/线程模型，需测试兜底）。

- **T1（①空密码启动强校验）** — 改 `src/config.py` + `main.py`。无依赖。阻断裸奔，最高价值。
- **T2（③ is_ssrf_safe 扩大拒绝范围）** — 改 `web/security.py`。无依赖。几行，低风险高价值。
- **T3（①限流账号维度 + 激活 _AUTH_BLOCK_SECONDS）** — 改 `web/api.py` 限流状态机。依赖 T1（同模块）。低风险。
- **T4（④ CI 改装/扫 requirements.lock + 新增 lock_deps 脚本）** — 改 `ci.yml`、新增 `scripts/lock_deps.sh`、更新 `DEV_GUIDE.md`。无代码依赖。低风险。
- **T5（②可选 0.0.0.0 告警日志）** — 改 `web/api.py:run_web`。可选，极低风险。
- **T6（⑤ async 路由阻塞调用包 run_in_threadpool）** — 改 `web/routers/*.py`（status/conversations/keywords/stats/memories/routing_quality/decisions/health/metrics/persona）。依赖：确认现有测试基线通过。**最高风险，放最后**；逐项改 + 跑对应测试。
- **T7（⑤可选 get_dws 缓存）** — 改 `web/api.py`。低依赖。
- **T8（③ P1 DNS 重绑定钉 IP）** — 改 `kb.py`。建议下一阶段，Phase 0 仅记录。

依赖图：`T1 → T3`；`T6` 独立但需最后且配测试；其余基本独立。

---

## 7. 依赖包（是否新增）

- **运行期**：无新增。所有加固用标准库（`hmac`/`secrets`/`ipaddress`/`threading`）与既有 `fastapi.concurrency.run_in_threadpool`（FastAPI 已带）。
- **开发/CI**：`pip-audit`（CI 已装，报告型）；`requirements.lock` 由现有 `pip freeze` 生成，无新工具。
- 不引入 `aiosqlite`（避免在 Phase 0 做大规模异步化改造，风险高）；用 `run_in_threadpool` 就地包裹，行为变更最小。

---

## 8. 共享知识（跨线程一致性约定）

- **per-thread SQLite 连接**：`SQLiteStore.conn` 用 `self._conns[tid]`（threading.local 语义），任何线程（事件循环线程 / anyio 池线程 / poller 线程）访问都拿自身连接，绝不共享 `sqlite3.Connection`。
- **faiss 索引**：单例 + 模块级 RLock（`_with_index_lock`），跨线程串行读写。
- **跨线程可变状态**：`agent._tl` / `SkillRouter._tl` 用 `threading.local()`；web 层平台上下文用 `ContextVar`（`web/dependencies.py:_platform_ctx`），请求级隔离。
- **配置真源**：`_get_cfg()` 优先 `shared_state.get_config()`（进程单例），回退磁盘 mtime 缓存（`web/api.py:76-89`）；`get_store` 按平台解析 DB 路径。
- **加固约束**：任何 `run_in_threadpool` 包裹的阻塞代码，不得跨 `await` 持有 `store.conn`；faiss 操作保持经 `@_with_index_lock`；不得把单例 Store 的连接暴露给其他线程。

---

## 9. 待明确事项（需用户拍板，给推荐值 + 理由，不替用户定死）

1. **限流阈值**（T3）：当前 `5 次 / 300s` 窗口 + 建议新增 `block 300s`。
   - 推荐：维持 `5/300`，新增封锁 `300s`。理由：管理后台爆破面小，过严易误锁运维；300s 窗口与封锁对等，攻防平衡。可据日志观察调整。
   - 问：是否接受「账号维度」也计入？（推荐：是，防固定 IP 多账号轮询；用户名取自 Basic Auth creds，攻击者未知有效用户名时 username 维度意义有限，故 **IP 维度为主、账号维度为辅**）。

2. **`0.0.0.0` 开关保留**（②）：用户要求「保留可显式设 0.0.0.0」。
   - 推荐：**保留开关**（已满足），仅加 `WARNING` 日志；**不**在代码层禁止。理由：内网/容器编排场景需要；安全由「反代 + auth」负责，文档（`config.yaml.example:387`）已注明。是否要默认在 `0.0.0.0 + auth 关` 时**直接拒绝启动**？推荐否（保持灵活），仅告警。请确认。

3. **`hmac.compare_digest` vs `secrets.compare_digest`**（①）：审计建议 `secrets`，当前 `hmac`。
   - 推荐：**保持 `hmac.compare_digest`**（等价于 `secrets`，均为 C 级恒定时间，少一处 import）。若需与审计字面一致可改 `secrets`，无安全差异。请确认偏好。

4. **SSRF 是否需白名单（allow-list）**（③）：当前为「黑名单（拒内网/保留）+ 协议白名单 http/https」。
   - 推荐：**维持黑名单 + 协议白名单**，不引入目标域名白名单。理由：KB 导入需抓任意公网网页，域名白名单会严重限制可用性；黑名单已覆盖所有内网/保留段（补 `0.0.0.0` 后）。若用户场景仅导入固定可信源，可后续加可选 allow-list。请确认。

5. **DNS 重绑定（TOCTOU）是否纳入 Phase 0**（③）：当前 `is_ssrf_safe` 与 `requests.get` 两次解析。
   - 推荐：**Phase 0 先补范围拒绝（含 0.0.0.0），DNS 重绑定钉 IP 放到 P1**（改造面与行为变更风险大，且需自定义 HTTPAdapter）。请确认是否同意延后。

6. **空密码强校验的失败形态**（① T1）：启动即 `sys.exit(1)` 还是降级告警？
   - 推荐：**启动即退出 + 明确错误日志**（fail-closed，最符合安全默认）。若运维希望「仅告警不退出」需显式说明。请确认。

7. **⑤ 是否接受 `run_in_threadpool` 方案而非 `aiosqlite`**：推荐 `run_in_threadpool`（零行为变更、复用现有 per-thread 连接），不引入异步 DB 层。请确认。

---

## 附：实读证据索引（便于复核）

- `src/config.py:594` host 默认 127.0.0.1
- `src/config.py:597-601` auth_enabled=True / 空密码注释但无校验
- `src/config.py:806-851` load_config（无空密码校验）
- `web/api.py:154-219` 限流（IP 维度，_AUTH_BLOCK_SECONDS 死代码 :160）
- `web/api.py:222-227` hmac.compare_digest
- `web/api.py:277-313` web_auth_middleware
- `web/api.py:526-543` EmbeddingClient 单例
- `web/api.py:572-578` get_dws（每次 new，可缓存）
- `web/api.py:682-714` run_web 绑定优先级
- `web/security.py:12-39` is_ssrf_safe（未拒 unspecified/reserved/multicast）
- `web/dependencies.py:65-126` get_store per-thread 缓存
- `web/routers/kb.py:201,219-235,273-278` import-url SSRF 逐跳 + Playwright
- `web/routers/status.py:52-72` async def 内同步 SQLite（阻塞案例）
- `src/memory/sqlite_store.py:505-559` conn per-thread
- `src/memory/index_lock.py:15` _with_index_lock（RLock）
- `.github/workflows/ci.yml:51-72` 装 requirements.txt / 扫 requirements.txt
- `requirements.lock` 已存在（精确版本）
- `config.yaml.example:385-390` web 段文档（host/auth_*）
