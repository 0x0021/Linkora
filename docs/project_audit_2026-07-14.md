# 灵桥 (Linkora) 项目 — 全面体检与优化建议

> 审计日期：2026-07-14 | 审计范围：src/ (16,415 行) + web/ + tests/ (79 文件, 18,584 行)
> 方法：多维并行静态分析 + 关键安全隐患实机验证（file:line 已核对）

---

## 📊 项目体量快照

| 指标 | 数值 |
|------|------|
| 后端源码 | 16,415 行 / ~95 个 .py 模块 |
| 最大模块 | `sqlite_store.py` 2612 行, `poller.py` 2451, `web/api.py` 4167(含前端), `agent.py` 981 |
| 测试 | 79 文件 / 18,584 行（测试代码量 ≈ 源码 1.13×，覆盖度较好） |
| 依赖 | requirements.txt 全 `>=` 无锁版本；核心库版本偏新（fastapi 0.139 / pydantic 2.13 / st 5.6 / numpy 2.5） |
| 关键风险 | **认证默认关闭 + 监听 0.0.0.0**、dws_adapter 跨线程状态、每请求重建重量对象 |

---

## 1. 代码质量

**现状**：整体可读，无 TODO/FIXME 垃圾，无可变默认参数。但存在典型"神模块"和异常吞噬。

| 等级 | 问题 | 位置 | 建议 |
|------|------|------|------|
| 🔴 High | `web/api.py` 4167 行 / 91 路由，认证+配置+统计+静态+业务全混 | `web/api.py` | 拆 `auth.py / config_api.py / stats_api.py / bot_status.py`（用 APIRouter） |
| 🔴 High | `poller.py` 2451 行，轮询+处理+防抖+记忆抽取+后台调度全堆一处 | `src/poller.py` | 拆 `PollerScheduler / ReplyDispatcher / BackgroundScheduler` |
| 🔴 High | `sqlite_store.py` 2612 行，消息/会话/关键词/决策/记忆/归档单类管 | `src/memory/sqlite_store.py` | 按职责拆 Repository（MessageRepo/KeywordRepo/DecisionRepo） |
| 🟠 Med | `except Exception` 40+ 处（poller 最重），吞错无日志 | `poller.py:94,99,116…` | 捕具体异常 + `logger.exception` |
| 🟠 Med | 裸 `except:` 静默吞错 | `data/skills/.../search_v1_backup.py:286,908` | 改为 `except Exception: log` |
| 🟠 Med | `print()` 替代日志（CLI 段 + 多个 skill 脚本） | `main.py:1809-1882`, `minutes_extract_todos.py:38` | 统一 `logging` |
| 🟠 Med | 硬编码 DB 路径重复 3 次 `./data/dingtalk-ai.db` | `config.py:186` / `sqlite_store.py:167` / `api.py:168` | 单一 Config 真源 |
| 🟡 Low | 重复/备份脚本（带 " 2" 副本、search_v2_backup.py） | `data/skills/...` | 删备份留唯一实现 |
| 🟡 Low | 命名缩写 `cfg/db/js` | `api.py` 多处 | 展开语义名 |

---

## 2. 性能

**现状**：最严重的问题是"**每请求重建重量级对象**"+"**同步 SQLite 阻塞事件循环**"。

| 等级 | 问题 | 位置 | 建议 |
|------|------|------|------|
| 🔴 High | 每次 KB 请求都 `EmbeddingClient(config.embedding)` 新建→同步加载 1.2GB 模型 | `api.py:1488/1780/1811/1849/1878` | 复用 `app_instance.embedding_client` 单例 |
| 🔴 High | 所有 `async def` 端点内直接跑同步 `store.xxx()`/`cur.execute`，阻塞事件循环 | `api.py:469-483` 等 | 包 `run_in_threadpool` 或换 `aiosqlite` |
| 🔴 High | `/api/stats/messages` 全表 GROUP BY + 每 30s 跑 `jieba.lcut` 词频 | `api.py:2509-2722` + `app.js:3737` | 词频 TTL 缓存(5min) + 时间窗预聚合 |
| 🟠 Med | `get_store()` 每请求 new SQLiteStore → faiss 索引每次全量重读 | `api.py:392-395`, `sqlite_store.py:1542` | 应用级单例复用 |
| 🟠 Med | `/api/conversations` N+1（每行额外 2 查询，最坏 ~100 次） | `api.py:596-623` | `LEFT JOIN + GROUP BY` 一次性取 |
| 🟠 Med | `/api/messages` 逐条遍历目录找图 + 构造响应时写库 | `api.py:684-749` | 落库时回填路径；响应只读 |
| 🟠 Med | `get_store()`+`init_db()` 每请求跑全部 DDL | `api.py:392`, `sqlite_store.py:255` | 启动时建表一次 |
| 🟡 Low | 前端 5s 全量拉消息、`/api/intents` 每次重建 `IntentRegistry` | `app.js:3515`, `api.py:2879` | 增量拉取 / 缓存 registry |

---

## 3. 架构

**现状**：无循环导入（agent→semantic 单向），但**全局单例隐式耦合**和**三处配置真源**是核心病灶。

| 等级 | 问题 | 位置 | 建议 |
|------|------|------|------|
| 🔴 High | 共享 `dws_adapter.dry_run` 被 web 回调线程与 poller/摘要线程**并发读写无锁** | `dws_adapter.py:183-187`, `main.py:254` | `dry_run` 改为构造参数；reload 时整体替换实例 |
| 🔴 High | 配置三处真源（磁盘 yaml / main 内存副本 / api 每请求重 load） | `config.py` / `main.py:251` / `api.py:102,2072` | 进程内 Config 单例（shared_state），yaml 仅持久层 |
| 🔴 High | web 层用**独立** DwsAdapter / Store 单例（与 bot 不一致） | `api.py:398` `get_dws()`, `:392` `get_store()` | web 与 bot 注入同一单例 |
| 🟠 Med | `main.py` 约 35 处手工 `tool_router.register(...)` | `main.py:306-425` | 工具自动发现（目录扫描/entry_points） |
| 🟠 Med | `agent.py` 编排 + RAG 意图分类业务逻辑混杂 | `agent.py:32-54` | 抽 `RAGIntentClassifier` |
| 🟡 Low | 模块级全局可变状态（embedding client、app_instance、callback） | `semantic.py:37`, `shared_state.py` | 收敛为显式注入 |

---

## 4. 安全性 🚨（最高风险维度）

**现状**：**当前部署若暴露在公网 = 高危**。已实机验证以下全部成立。

| 等级 | 问题 | 位置 | 建议 |
|------|------|------|------|
| 🔴 Critical | **认证默认关闭** `auth_enabled=False` | `src/config.py:423` | 默认 `True` |
| 🔴 Critical | **监听全网卡** `host="0.0.0.0"` | `web/api.py:3898` | 默认 127.0.0.1 或前置反代 |
| 🔴 Critical | 登录**非恒定时间**比对 + **无限流**（可爆破） | `api.py:122` | `hmac.compare_digest` + IP 失败计数限流 |
| 🔴 High | **SSRF 绕过**：import-url 仅校验一次 DNS，重定向到 169.254.169.254 可绕过；Playwright 分支 `page.goto` 完全无校验 | `api.py:1620`(跟随重定向), `:1658` | 每次重定向重校验 IP / `allow_redirects=False`；Playwright 走同一校验 |
| 🔴 High | **`/api/image/` 免认证** → OCR 截图（含人脸/消息）任意读 | `api.py:90` 白名单 | 移出白名单，复用 Basic Auth |
| 🟠 Med | 明文密钥存 `config.yaml`（api_key / hf_token / auth_password，默认 admin/admin） | `config.yaml:20,35,39,504` | 环境变量/密钥管理器注入；落盘不回写明文 |
| 🟠 Med | `shell=True` + `curl ... | bash` 装 skillhub（无签名校验） | `api.py:3526-3532` | 下载后校验哈希/pin 安装器；避免 shell=True |
| 🟠 Med | ReDoS：`/api/keywords/test-match` 用内置 `re`（无超时） | `api.py:1077` | 与生产一致走 `regex` + timeout |
| 🟠 Med | 日志含 PII/敏感命令，经 `/api/logs` 直出未脱敏 | `logger.py:114`, `api.py:443`, `dws_adapter.py:195` | 日志脱敏 + 角色可见性 |
| 🟡 Low | 原始 `dict` 请求体（非 pydantic） | `api.py:1393/1503/1561` | 统一 pydantic 模型 |

**好消息**：未发现 `eval/exec/pickle/yaml.load(不安全)`；`config.py:452` 用 `yaml.safe_load`；subprocess 多为参数列表；无通配 CORS。`config.yaml` 已被 gitignore，泄密风险已缓解。

---

## 5. 可维护性

**现状**：测试覆盖度优秀（测试/源码 1.13×，无回归文化），但文档与注释分布不均。

| 等级 | 评估项 | 结论 |
|------|--------|------|
| 🟢 良好 | 测试覆盖率 | 79 测试文件，路由/嵌入/决策/语义均有专项单测；CI 有 `pytest-timeout` 防卡死 |
| 🟢 良好 | 无 TODO/FIXME 垃圾、无可变默认参数 |
| 🟠 待改进 | 注释完整性 | 神模块（api/poller/store）内部缺段落级说明；多处 `except` 吞错无日志 |
| 🟠 待改进 | 模块级文档 | 整体架构见 `docs/architecture.md` 与 `docs/design.md`；仍缺部署手册 / 贡献指南（部署见 `docs/deployment.md`） |
| 🟠 待改进 | 前端可维护性 | `web/static/js/app.js` ~4800 行单文件，建议按页面拆 module |
| 🟡 小问题 | 重复脚本副本、`print` 调试残留 | 见代码质量 Low 项 |

**建议**：补 `ARCHITECTURE.md`（模块依赖图 + 数据流）；前端按 SPA 路由拆 `app.js`；为关键安全函数补 docstring。

---

## 6. 依赖管理

**现状**：核心库版本偏新、无明显过时漏洞，但**完全未锁版本**是供应链风险。

| 等级 | 问题 | 位置 | 建议 |
|------|------|------|------|
| 🟠 Med | 全 `>=` 无精确版本/哈希锁 | `requirements.txt` | 生成 `requirements.lock` + `pip-audit` 定期扫 |
| 🟡 Low | `requests` / `httpx` 并存（可统一 httpx） | `requirements.txt` | 评估收敛 |
| 🟢 良好 | fastapi 0.139 / pydantic 2.13 / sentence-transformers 5.6 / numpy 2.5 均为近版 |
| 🟢 良好 | `regex` 已用（ReDoS 防护）、`pytest-timeout` 防呆 |

**建议**：`pip freeze > requirements.lock`；CI 加 `pip-audit` 步骤；`huggingface_hub` 已 1.23（此前踩过 1.21 进度 API 差异，建议锁该版本避免回归）。

---

## 🎯 总体优先级路线图

### P0 — 立即做（安全/稳定性，半天~1天）
1. `auth_enabled` 默认 `True` + 恒定时间比对 + 登录限流
2. web 绑定 `127.0.0.1`（或文档明确前置反代才能 0.0.0.0）
3. `/api/image/` 移出免认证白名单
4. import-url SSRF 重定向重校验 + Playwright 分支校验
5. `dry_run` 改构造参数，消除跨线程并发写（当前真发风险）

### P1 — 本周（性能 + 架构解耦，2~3天）
6. EmbeddingClient / SQLiteStore / faiss 改为应用级单例（消除每请求重建）
7. 同步 SQLite 调用包 `run_in_threadpool`（解除事件循环阻塞）
8. 配置收归单例（消除三处真源 + web/bot 单例不一致）
9. `/api/stats/messages` 词频 TTL 缓存 + 时间窗预聚合
10. `/api/conversations` LEFT JOIN 去 N+1

### P2 — 迭代（质量/可维护，持续）
11. 拆分 4 大神模块（api/poller/store/agent）
12. 工具自动发现注册
13. 前端 app.js 按页拆分；补 ARCHITECTURE.md
14. `requirements.lock` + `pip-audit` CI
15. 清理重复/备份脚本、`print`→logging、异常精细化

---

## 一句话总结
**安全是当务之急**（默认无认证+全网卡+免认证图片接口），其次是**每请求重建重量对象导致的性能悬崖**，架构上**配置多真源与跨线程共享状态**是两颗定时炸弹。代码与测试基础其实不错，整改重点是"加固边界 + 消除重复造轮子"，而非推倒重来。
