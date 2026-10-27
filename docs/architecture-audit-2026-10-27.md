# Linkora 架构与代码系统性梳理 · 改进方案（待确认）

> 审计范围：`src/`(216 模块 / 63,783 行) + `web/` + `tests/`(298 文件) + Docker/CI/前端构建。
> 方法：4 个只读探查子代理分维度取证（架构 / RAG与核心逻辑 / 代码质量与安全 / 前端与部署）+ 交叉核验历史缺陷审计（F1–F13 / D1–D10 / R1–R6）+ 对 3 条 High 结论手工复核源码。
> 状态：**已于 2026-10-27 实施第一批 P0 + 第二批 P1-1/P1-2/P1-3/P1-4 + 部分 P2**（10 个 commit 推 main，CI 四 job + CodeQL 全绿）。下方部分结论已被后续修复证伪，**实施以文末「§实施结果」为准**。

---

## 一、事实校准（先纠正两个认知偏差）

1. **本地 Embedding 服务 `127.0.0.1:8910` / `Qwen3-Embedding-0.6B` / key `byoxoo21` 不在源码中。**
   全仓 grep `byoxoo21` / `8910` 命中 0 次（仅 `docs/rag.md` 的注释默认值与 `llm/client.py:425` 注释）。`config_models.py:740` 默认是 `provider=local, model=BAAI/bge-small-zh-v1.5`（384 维）。该服务若存在，应只在 live `config.yaml` / 运行环境侧配置，源码不构成其凭证。**建议**：核实线上 `config.yaml` 的 embedding 段，确认维度与向量索引维度校验（已有 F19 守卫）是否对齐，避免 1024/384 维度错配重演 D9。
2. **"BM25 混合重排序" 口径不实**：`reranker.py:15` 权重 0.6/0.4 属实，但 keyword 端是**自研简化 2-gram 打分**，非真正 BM25（无文档频率/长度归一）。属命名/文档与实现偏差，不影响功能。

---

## 二、已确认健康、不建议改动（避免误伤）

这些是被反复验证的资产，列出来是防止后续重构顺手破坏：
- 会话分库**写入闸门**（`_write_gate`/`write_with_retry`，仅 4 个分库 repo 收口，已成熟）
- `src/utils/net.py` 的 SSRF guard：默认拒绝 + IP 钉死消除 DNS 重绑定 + fake-IP 段处理（行业级）
- 全仓**无** `shell=True`/`eval`/`exec`/`pickle`，subprocess 均列表调用 + `shlex.quote`
- 日志注入防线 `NoLogInjectionFilter` + `_redact_sensitive`；密钥全 gitignore；pydantic 校验 + 未知 key 告警
- 语义软分块（`split_text` max_len 软上限 + `_split_recursive`）、FAISS 维度硬校验 + 原子落盘
- `runtime_*` 的 mixin 拆分（F1 重构）、ContextVar 多平台隔离（零侵入 100+ 调用点）
- XSS 核心 `escapeHtml` + `sanitizeUrl` + `<clickable>` 白名单；JWT 自动续期；四源依赖一致性校验；前端零漂移门禁
- 说话人署名 bug（`prompt_builder.py:301` 同发言人合并守卫）**已于当前版本修复**，建议加回归测试固化

---

## 三、优先级方案

### 🔴 P0 — 结构性 / 可靠性 / 供应链（建议优先确认）

#### P0-1 · 主库写路径系统性绕过写入闸门（同源 Oct-5 事故）
- **问题**：`_write_gate`/`write_with_retry`/`store._lock` 只施加到 4 个会话分库 repo；主库全部写 repo（`kb`/`decisions`/`draft`/`docs`/`routing_quality`/`keyword_rule`/`gate_rule`/`baseline`/`few_shot`/`memory`/`feedback`/`tool_execution`）均为 `conn.cursor()+conn.commit()` 直写。`test_write_gate_coverage.py` 的 `REPO_FILES` 对主库 repo **零断言**。
- **影响**：Oct-5 "database is locked" 事故的**同类缺陷在主库复现**；高并发下主库写可能互相穿插，且闸门的超时兜底/自愈对主库无效。影响数据完整性与一致性。
- **改进方案**：将主库高频写 repo 收口到统一 `write_with_retry`/`_write` 封装；把主库 repo 纳入 `test_write_gate_coverage` 的 AST 断言；先覆盖 `kb`/`memory`/`tool_execution` 三个最高频。
- **理由**：Oct-5 血的教训已证明分库闸门的价值，主库是同一模型下的盲点。
- **预期收益**：消除主库写锁竞态盲区；闸门覆盖率可量化（CI 红线）。
- **潜在风险**：改动面大（12 个 repo），需压测验证；低峰期灰度 + 回归测试先行。

#### P0-2 · Docker 安装 `requirements.txt` 而非 `requirements.lock`
- **问题**：`Dockerfile:44-45` 与 `Dockerfile.build:17-18` 均 `pip install -r requirements.txt`；但 `lock_deps.sh`/`check_deps.py` 注释声称"CI 与 Docker 均安装 requirements.lock"。实际发布镜像按直接依赖**浮动解析**传递闭包。
- **影响**：破坏"测试环境==发布环境"承诺；`check_deps.py --strict` 门禁只在 CI 跑、不作用于真实镜像；一旦某传递依赖发破坏性补丁，发布镜像即漂移/脆化（供应链风险）。
- **改进方案**：`Dockerfile`/`Dockerfile.build` 改为 `COPY requirements.lock .` + `pip install -r requirements.lock`；修正 `lock_deps.sh`/`check_deps.py` 注释。锁文件由 CI 生成，需在构建前确保 lock 可用（或加一步 `uv pip compile`）。
- **理由**：否则精心维护的四源锁体系对"真实发布物"完全失效。
- **预期收益**：发布可复现、供应链可审计；与 CI 门禁形成闭环。
- **潜在风险**：lock 由流水线生成（本地与 CI uv 版本不一致会漂移 revision）——需明确构建期如何取得一致 lock；首次切换可能暴露 lock 与 txt 的细微差异需先对齐。

#### P0-3 · 默认双进程启动触发已知跨进程 SQLite 写锁
- **问题**：`run_linkora.py:229` 单进程需显式 `--single-process`；默认走 `else`(237 行) 双进程（web + worker 两独立进程）。README 明确"推荐单进程"却非默认。
- **影响**：正是 Oct-5 生产事故的触发形态（web/worker 跨进程争用 SQLite 写锁，进程内自愈无效）。默认配置与"无锁"目标相悖，新部署者易踩坑。
- **改进方案**：把 `--single-process` 设为**默认**，`--multi-process` 作为显式逃生口；或在启动横幅/日志显式警告双进程锁风险。
- **理由**：单进程是根治跨进程锁的方案，且 `lifecycle.py:115` 的 `both` 模式已完备支持，零业务改动。
- **预期收益**：消除默认部署的锁争用脚枪；代价仅是改 Web 代码会连带重启 worker（已有 `--dev` 热重载承担）。
- **潜在风险**：单进程下若 worker 长阻塞会拖慢 Web 响应——对本地/单机部署可接受；大规模部署仍建议 `--multi-process` + 单平台库隔离。

---

### 🟠 P1 — 高杠杆（安全 / 一致性，建议本轮批次处理）

#### P1-1 · SSRF 防护能力强但采用率≈0
- **问题**：`net.py` 的 `safe_http_request`/`ssrf_safe_get` 是行业级实现，但全仓仅 2 个文件调用（且含 net.py 自身）。`web-composite-search/scripts/search.py:579/583`（裸 `requests.get(..., allow_redirects=True)` 无 guard）、`contract-review/scripts/*`（仅校验 base_url 不钉 IP，存在 DNS 重绑定窗口）。**无 lint 规则强制出站走 guard**。
- **影响**：防护形同可选，新代码持续用裸 `requests`；若脚本引擎配置被影响，可经重定向打内网/云元数据。
- **改进方案**：加 lint 门禁（正则扫描 `requests.get/httpx.Client` 必须包裹 `safe_http_request` 或带 `# ssrf-ok` 豁免注释）；修复 search 脚本用 `ssrf_safe_get` 并 `follow_redirects=False`；contract 脚本补 IP 钉死。
- **理由**：能力已具备，缺的是强制推行，用 lint 收敛成本最低。
- **预期收益**：出站请求统一受 SSRF 约束；新代码不再绕过。
- **潜在风险**：对"可信配置端点"（LLM base_url、embedding）需合理豁免白名单，避免误伤。

#### P1-2 · 宽异常被当拐杖（计费静默吞噬 + SILENT 未纳入基线）
- **问题**：`usage_ledger.py:143` `except Exception: store = None`（带 `noqa: BLE001`）——LLM 用量记账在 store 注册失败时**静默丢弃**，无日志，计费/配额失真。全仓 AST 基线 638 处宽异常中 **23 处真 SILENT、459 处 LOGGED、156 处 RERAISE**；`check_no_bare_except.py` 只管"数量不增"，不管"SILENT 类别"。
- **影响**：涉及计费/持久化的失败被静默降级，掩盖真实缺陷。
- **改进方案**：`usage_ledger` 补 `logger.error` 并 fail-closed 告警；扩展 `check_no_bare_except.py` 区分 SILENT/LOGGED 类别，对 SILENT（尤其计费/密钥/写盘/事务类）设专门基线并逐步清零。
- **理由**：宽异常应保留为有意 fail-soft 探针，但计费/安全边界必须暴露。
- **预期收益**：计费可观测；CI 从"数量不增"升级为"类别收敛"。
- **潜在风险**：逐个收敛 SILENT 需逐处判断是否有意降级，工作量大但低风险（先做 usage_ledger 等高危点）。

#### P1-3 · 多平台 `config.poller` 别名到 dingtalk
- **问题**：`config.py:_sync_root_poller`(262-280) 把根级 `config.poller` 别名到 dingtalk 平台块的 poller。多平台部署时，无论当前平台是飞书/企微，`config.poller` 始终反映 dingtalk 配置。
- **影响**：非 dingtalk 平台可能读到错误的轮询设置（间隔/并发/历史窗口），属有意兼容但存在误配风险。
- **改进方案**：废弃根级 `poller` 兼容段，所有读取按"当前活跃平台"取 `platforms[].poller`；保留 example 注释说明 legacy 段仅单平台兼容。
- **理由**：多平台已是默认形态，legacy 别名是单平台时代的残留。
- **预期收益**：消除跨平台配置串扰。
- **潜在风险**：需全仓搜 `config.poller` 调用点改为平台感知；单平台用户行为不变（低风险）。

#### P1-4 · RBAC 敏感 GET 白名单手工维护 + viewer 名实不符
- **问题**：`api.py:419-420` `_SENSITIVE_GET_PATHS` 手写白名单（仅 `/api/config/export`、`/api/logs`）；新增敏感只读端点必须手动加，易遗漏。`ROLE_VIEWER` 概念存在但**任何登录者都是 admin 或 operator**（无真正只读角色）。登出黑名单/登录限流 `_revoked_token_hashes` 为**进程内集合**，多 worker 仅本 worker 生效。
- **影响**：新增敏感只读接口若漏加白名单 → 非 admin 可读；多副本部署下登出/限流失效。
- **改进方案**：敏感 GET 改从路由元数据/装饰器自动归集（如 `@sensitive_get`），或显式注册；补一个只读 viewer 角色（如对外分享场景）；多 worker 场景用共享存储（Redis）或文档注明限制。
- **理由**：手工白名单必然随接口增长而遗漏。
- **预期收益**：敏感只读端点自动受控；角色模型名副其实。
- **潜在风险**：自动归集需约定注解方式，改造需遍历现有只读端点核对。

#### P1-5 · 平台适配器归一化/错误分类逐平台重写
- **问题**：`feishu.py`(872)/`wecom.py`(938) 各自重写了 `_normalize_chat`/`_normalize_message`/`_classify_error`/`_infer_single_chat`/`_is_benign_error` 及大量同名 send/list/contact 方法。`dws_adapter` 已拆成 11 个垂直 mixin 并复用 `im_adapter.markdown_fix`/`message_format`，方向相反（复用而非复制）。
- **影响**：三平台 bug 修复需在三处同步；同一边界逻辑三份实现易漂移。
- **改进方案**：把稳定的归一化/错误分类下沉到 `im_adapter` 共享基类（已有 `base_adapter.py` 439 行做载体），平台子类仅保留 CLI 子命令差异；删除 feishu/wecom 的重复实现。
- **理由**：DWS 的 mixin 拆分已是成功先例，可借鉴。
- **预期收益**：修复一次、三平台受益；减少回归面。
- **潜在风险**：重构涉及消息/错误解析核心路径，需逐方法加等价性测试（先 feishu/wecom 各抽 2-3 个最高频方法试点）。

---

### 🟡 P2 — 工程纪律 / 技术债（中等，可排期）

#### P2-1 · `base.py` 星号导入枢纽 + 导入副作用 + 幽灵重导出
- **问题**：`src/platform/base.py` 被 13 个模块 `from .base import *`；`base.py:22` `load_dotenv()` 导入即产生全局副作用；`:31-33` 注释自承 `DwsAdapter` 是"为星号转出而保留、删除即 NameError"的幽灵导入。
- **影响**：导入图脆弱——改 base.py 可能级联 13 处故障；`load_dotenv` 副作用使 base 不可安全单独导入/测试。
- **改进方案**：用显式命名再导出替代星号枢纽；把 `load_dotenv()` 上移到真正入口（main/run_linkora）；移除幽灵重导出。
- **理由**：脆弱导入网是大型 Python 项目的经典债，迟早反噬。
- **预期收益**：导入图可推理、可单测；消除级联风险。
- **潜在风险**：13 处消费模块需逐个改 import，工作量大；需配合循环导入的后置 import 一并清理（见 P2-1b）。

#### P2-2 · gitleaks 未接 CI + 无镜像扫描
- **问题**：`.github/workflows/` 搜 gitleaks **无命中**（仅 CodeQL）；无任何 `docker build` + Trivy/Grype 环节。
- **影响**：密钥自动化扫描缺位（CI 仅依赖 `.gitignore`/`.dockerignore`）；镜像供应链（基础镜像 CVE、依赖 CVE）无把关，与 P0-2 叠加放大。
- **改进方案**：加 `gitleaks/gitleaks-action` 到 CI（与 README 承诺对齐）；加 `docker build` + `trivy image` 扫描步骤。
- **理由**：生产级项目应有 secret + 镜像两层扫描。
- **预期收益**：secret 与镜像漏洞自动拦截。
- **潜在风险**：gitleaks 可能误报既有测试夹具假值（已有 `.gitleaksignore` 处置经验）；Trivy 初跑可能报基础镜像 CVE 需 triage。

#### P2-3 · esbuild 版本浮动 + ESLint 声明无配置
- **问题**：`package.json` `esbuild: "^0.28.2"` caret 浮动——minify 重排行为与此强相关，正是 data-args 故障根因；`lint` 脚本声明了 eslint 但**仓库无 eslint 配置**且未进 CI 门禁。
- **影响**：本地与 CI 若 lock 未同步可能产生不同 minify 结果，绕过 `check_data_args_concat` 逻辑；JS 质量除自定义检查外无通用 lint。
- **改进方案**：esbuild 精确钉版（`"esbuild": "0.28.2"`）；要么补 `.eslintrc` 并加入 CI，要么移除无效的 `lint` 脚本。
- **理由**：构建可复现性要求工具版本固定；无效脚本会误导。
- **预期收益**：构建结果确定；JS 有通用质量门禁。
- **潜在风险**：钉版后若 esbuild 发兼容修复需手动升；ESLint 初配可能有大量存量告警需分批消化。

#### P2-4 · Python 版本未钉 3.14.6
- **问题**：`Dockerfile:1` `FROM python:3.14-slim`、`ci.yml` `python-version: '3.14'` 均浮动；`requires-python=">=3.14"`；`requirements.lock` 按 `--python-version 3.14` 解析，与运行时小版本可能产生细微差异。
- **影响**：CI/Docker/lock 三处 Python 版本可能不一致，复现性受损。
- **改进方案**：钉 `python:3.14.6-slim` + `python-version: '3.14.6'`。
- **理由**：消除补丁级漂移。
- **预期收益**：三处 Python 版本一致，构建可复现。
- **潜在风险**：需确认 3.14.6 镜像在 Docker Hub 可用。

#### P2-5 · `rag.js:769` 未转义拼接（XSS 防御缺口）
- **问题**：`web/static/js/pages/rag.js:769` `previewEl.innerHTML = '...' + (data?.message || '同步失败') + '...'`——`data.message` 未过 `escapeHtml`，而同文件相邻行均用了。
- **影响**：对自身 XSS 规则明确违反；利用需上游返回攻击者可控的 HTML 错误信息（前提苛刻），但属 defense-in-depth 缺口，修复成本极低。
- **改进方案**：包一层 `escapeHtml(data?.message)`。
- **理由**：与项目既有 XSS 纪律一致，零副作用。
- **预期收益**：消除一个明确违规点。
- **潜在风险**：无。

#### P2-6 · `print()` 代替 logging（68 处）绕过脱敏滤器
- **问题**：`src/` 中 68 处 `print()`（集中在 skills 搜索脚本），绕过 `NoLogInjectionFilter`（CWE-117）与日志级别；部分被脚本误判为 SILENT。
- **影响**：敏感内容可能直接打到 stdout，且不受日志脱敏/级别管控。
- **改进方案**：统一改为 `logging.getLogger(__name__)`，恢复脱敏与级别控制；脚本改用 logger 而非 print/traceback.print_exc。
- **理由**：CWE-117 防护需要全链路走 logger。
- **预期收益**：敏感日志受控；输出可分级。
- **潜在风险**：需确认脚本运行环境能看到 logger 输出（通常无碍）。

#### P2-7 · 上帝模块 `config_models.py`(1127 行) 优先拆分
- **问题**：`config_models.py` 是单一超大 pydantic 聚合（40 个配置类）；`poller_strategy.py`(1069)/`schema.py`(973) 也超千行，但 poller 系列耦合低内聚高（拆分价值有限），schema 已集中且健康（不必动）。
- **影响**：`config_models` 单文件难导航、回归面大。
- **改进方案**：按领域（platform/llm/tools/memory/web/rag）拆成 `config_models/` 子包，保留 `src/config_models.py` 作为再导出兼容层（已有 F401 豁免先例）。
- **理由**：配置类是高频改动面，拆分收益最高；poller/schema 不必动。
- **预期收益**：配置可维护性与可读性提升。
- **潜在风险**：大量 `from src.config_models import X` 需保留兼容层；需全量门禁回归。

#### P2-8 · 重排器命名"BM25"实为简化 2-gram 打分
- **问题**：`reranker.py:15` 权重大实为自研 2-gram 打分，无 BM25 的 IDF/长度归一；文档与代码口径不一致。
- **影响**：误导调参；非功能缺陷。
- **改进方案**：注释/文档对齐命名（"keyword_score"）；可选引入 `rank_bm25` 做真 BM25 提升长尾召回（需评估依赖与成本）。
- **理由**：诚实优先；真 BM25 是可选增强。
- **预期收益**：口径一致；可选提升检索质量。
- **潜在风险**：引入新依赖需锁版与测试；真 BM25 对中文需 jieba 分词配合。

---

### 🟢 P3 — 长尾 / 可延后

| 项 | 问题 | 改进方案 | 风险 |
|---|---|---|---|
| **P3-1** 意图分类纯关键词 | `intent/registry.py` 分类为 jieba/关键词驱动，长尾表达易误判/重叠 | 可选加 LLM 兜底分类（高置信关键词优先，模糊时 LLM） | 引入延迟/成本 |
| **P3-2** 提示注入面 | 系统字段已清洗，但对话历史/用户输入正文未内容级净化，仅靠 `final_guard` 近因约束 | 对历史/输入做指令式语句检测或隔离标记 | 误伤正常内容 |
| **P3-3** 测试过度 mock | 376 处 mock，部分可能验证 mock 而非行为 | 抽样复核核心管线测试的行为覆盖率 | 低 |
| **P3-4** 死配置 | `AlertConfig.webhook_url` 声明未被任何出站逻辑消费 | 实现 webhook 通知或删除字段 | 低 |
| **P3-5** 文档漂移 | `docs/configuration.md` 仍写 `bge-base-zh-v1.5`；`package.json` 缺 `version`；`TEST_COVERAGE.md` 过时 | 同步文档与代码默认值 | 低 |
| **P3-6** 生产公开 sourcemap | `bundle.*.js.map` 随 `/static` 匿名可访问 | 生产构建关闭 sourcemap 下发 | 排障不便 |

---

## 四、建议实施节奏（待你确认）

- **第一批（P0，发布前）**：P0-1 主库写闸门收口 → P0-2 Docker 改 lock → P0-3 单进程默认。均带回归测试 + 门禁。
- **第二批（P1，安全/一致性）**：P1-1 SSRF lint 强制 → P1-2 宽异常 SILENT 收敛 → P1-3 poller 别名废弃 → P1-4 RBAC 自动归集 → P1-5 适配器归一化（先试点）。
- **第三批（P2，工程纪律）**：P2-2 gitleaks+镜像扫描 → P2-3 esbuild 钉版/ESLint → P2-4 Python 钉版 → P2-5/6 小修 → P2-1 导入图清理（可独立成专项）→ P2-7 config_models 拆分。
- **第四批（P3）**：随版本迭代顺手处理。

> 下一步：你确认优先级与范围后，我按批次实施（每批 `fix → 跑门禁 [ruff/pyright/check_deps/check_no_bare_except/gitleaks/tests] → 验证 → 汇报`）。P0-1/P0-3 涉及运行期行为，建议先在测试环境验证再上生产。

---

## §实施结果（2026-10-27）

**已落地（10 个 commit，main 推送，CI `test`/`frontend`/`密钥扫描`/`CI 汇总` + CodeQL 全绿）**

| 原编号 | 落地情况 | commit |
|---|---|---|
| P0-1 | ✅ 主库剩余裸写收口到 `write_main_with_retry`（含 2 处 DDL）+ 新增 `tests/test_main_db_write_gate.py` | `249705f` |
| P0-2 | ✅ `Dockerfile` / `Dockerfile.build` 改装 `requirements.lock` | `9d1a54c` |
| P0-3 | ✅ `run_linkora.py` 默认单进程 both，`--multi-process` 显式逃生口，README 同步 | `069b2d9` |
| P1-1 | ✅ 新增 `scripts/check_ssrf_guard.py`（AST 判定 + CI 强制）+ 12 处裸出站显式豁免 + web-composite-search 引擎 host 白名单 | `0acfcac` |
| P1-2 | ✅ `check_no_bare_except.py` 增设 `SILENT_BASELINE=22` 闸门 + `usage_ledger` 两处静默降级补 warning | `1862796` |
| P1-3 | ✅ 新增 `src/config.py::resolve_poller_config`，runtime 消息链路 17 处读取点平台感知 + 热重载串扰修复 | `c87297e` |
| P1-4 | ✅ **修真实凭据泄漏**（`GET /api/config` 明文回传 `jwt_secret`）+ 敏感只读端点判定改「精确 + `/` 前缀」并补齐 16 个高危端点 + 测试对齐 | `177e397` / `4eadb52` |
| P1-4 续 | ✅ 敏感只读端点升级为**「敏感 + 公开」全覆盖**（106 个 GET 端点全部表态：敏感 63 / 公开 43），新增端点漏表态 → CI 红 | `7737937` |
| P1-5 | ✅ **试点**：`_normalize_chat`（唯一真重复）下沉到基类模板方法 + 差分等价性测试；其余方法经 AST 差分证实**语义真实分叉**，不合并 | `9600ea3` |
| P2-1 | ✅ **步骤 1**：`src/platform` 12 处 `from .base import *` → 显式导入清单（+105/-12 行），F403/F405 豁免移除 | `b2e7999` |
| P2-2 | ✅ gitleaks 接入 CI（独立 job「密钥扫描」，`fetch-depth: 0`） | `08173d9` / `ded1369` |
| P2-3 | ✅ esbuild 精确钉版 `0.28.2` + `npm run lint` 入 CI | `08173d9` |
| P2-4 | ✅ Python 运行时钉 `3.14.6`（5 处声明统一；`lock_deps.sh` 的 `PY_FLOOR` 仍为 3.14 下限） | `08173d9` |
| P2-5 | ✅ `rag.js:769` 未转义拼接 → `escapeHtml`（前端产物已重建入仓） | `08173d9` |
| P2-6 | ⚠️ **原判断证伪后按真问题收窄**：15 处技能脚本堆栈改投 stderr（不再把内部路径/堆栈喂进 LLM 上下文） | `7737937` |
| P2-8 | ✅ 关键词重排口径统一：「BM25」→「自研 2-gram 打分（非 BM25）」，差异写进 docstring | `158d9db` |
| P2-1 步骤 2 | ✅ `base.py` 瘦身为只定义自有符号（移除 43 个纯重导出）+ 16 条不变量测试 | `5f86338` |
| P2-7 | ✅ `config_models.py`（1127 行）→ **包** `src/config_models/`（`_const` / `platform` / `llm` / `rules_tools` / `memory_skills` / `system` / `app`），`__init__.py` 降到 244 行只做 re-export + 69 条不变量测试 | `c4181ca` / `01e2835` / `e78c2ad` |
| P3-4 | ✅ **告警系统接线 + webhook 渠道消费**：修「`record_error()` 零调用方」的死代码（`/api/alerts/stats` 永远返回空） | `c2e5e67` |
| P3-5 | ✅ 嵌入模型口径同步（`bge-base-zh` → `bge-small-zh`）+ `package.json` 补 `name`/`version` | `6357118` |
| P3-2 | ✅ **提示注入面缓解**：RAG 片段 / 公共记忆 / 历史 user 消息加「不可信数据」边界标记（正文逐字保留、零误伤、不改消息结构） | `d66bb60` |
| P3-3 | ✅ 补齐**双重回复防护**与**死信队列分支**的生产代码覆盖（7 条测试，**变异测试证实**：改坏生产判断 → 旧测试全漏检 / 新测试全捕获） | `938e8fc` |
| dotenv 专项 | ✅ `.env` 加载入口**显式化**：`main()` 首行 + `base.py` 幂等兜底（3 处模块级 env 读取仍有效）；Web 进程行为刻意不变；**显式指定 .env 路径**规避 python-dotenv 帧遍历断言 | `8bbffe0` / `c810ae5` |

**本轮证伪/修正的原判断（勿再照抄原文）**

1. **「主库 12 个 repo 全直写 `conn.commit()`」不成立** —— 写闸门 `write_main_with_retry` 于 2026-10-05 已建立并收口绝大多数 repo；真实剩余仅 3 处业务写（`memory_repo` 的 `upsert_summary` / `cleanup_old_memories` / `_supersede_similar_memories`）+ 2 处 DDL，且这些点虽在 `with self.store._lock:` 内（`_GatedWriteLock.__enter__` 已叠加全局闸门），缺的是**遇锁自愈重试**。
2. **「SSRF guard 形同可选（仅 2 处调用）」不成立** —— `weather.py` / `web_search.py` 早已正确使用 `ssrf_safe_get`（7 处）；真实缺口仅 web-composite-search 两处 `requests.get`。
3. **「`check_no_bare_except.py` 只管数量不管 SILENT 类别」不成立** —— SILENT/LOGGED/RERAISE 分类与 `report` 子命令早已存在，缺的是**基线门禁**（CI 只跑 count）。
4. **「ESLint 声明了但仓库无配置」不成立** —— `.eslintrc.cjs` 早已存在且 eslint 0 error（429 warning）。
5. **`requirements.lock` / `uv.lock` 已入库**（git 跟踪），P0-2 无文件缺失风险。
6. **「`src/` 里 68 处 `print()` 绕过脱敏滤器，应统一改 logging」不成立** —— 其中 **69 处在 `src/skills/`**，而 `src/skills/tool_wrapper.py:167-170` 是把**整个 stdout 原样作为工具结果喂给 LLM**（不是机器解析的 JSON），改 logger 会直接破坏输出契约；`platform/lifecycle.py` 的 16 处是 CLI 错误提示/启动横幅（logger 配置尚未就绪），`utils/logger.py` 的 4 处是 **logger 自身的输出实现**，`config_models.py` 的 2 处只是错误消息文本里的 `print(...)` 字样（grep 误匹配）。真问题只有一个：**内部堆栈被 stdout 喂进模型上下文**（浪费 token + 扩大提示注入面）→ 已把 15 处 `traceback.print_exc()` 改投 stderr。
7. **「feishu/wecom 重写了 `_normalize_chat`/`_normalize_message`/`_classify_error`/`_infer_single_chat`/`_is_benign_error`，三平台 bug 修复需三处同步」不成立** —— AST 差分比对（去 docstring 后 `ast.unparse` 归一化 + difflib）显示两文件的 20 个同名方法**0 个字节相同**，最高相似度仅 `_normalize_chat` 的 0.867；`_is_benign_error` 在 wecom **根本不存在**（只有 feishu 与 base 各一份）；`_normalize_message` 的 sender 结构（feishu 嵌套 dict vs wecom 扁平字段）、content 路径、时间戳字段三处分叉；`_classify_error` 的错误包结构（feishu 嵌套 `error.code` vs wecom 扁平 `errcode`）与**分类顺序相反**（feishu retryable→permission→unsupported，wecom permission→retryable）；`run` 的重试策略相反（base 对基类错误**不重试**，wecom **会重试**）→ 合并会改变企微线上行为。

8. **「`AlertConfig.webhook_url` 是死配置」判断偏轻** —— 实测问题**大得多**：整个告警系统**没有任何调用方** —— `record_error()` 与 `src/llm/client.py` 的 `_rethrow_classified()` 均**零调用**，导致 `GET /api/alerts/stats` 永远返回空、阈值告警永不触发，而 `docs/alerts.md` 明确教用户「在 `src/llm/client.py` 中集成告警」并给出 `webhook_url` 配置示例（**配了不生效**），web 端还暴露了 `/api/alerts/config` 的 PUT 写入接口进一步印证「以为配了就生效」。→ 已接线（最终失败出口记一次，重试不放大计数）+ 实现 webhook 消费（走 SSRF guard）。
9. **「生产应关闭 sourcemap 下发」对本项目不成立** —— 实测 `bundle.*.js.map` 含完整前端源码（65 万字符），但**零绝对路径、零密钥字面量**，且前端源码本就在**公开仓库**（`github.com/0x0021/Linkora`）→ 关闭 sourcemap 不产生实质安全收益，反而丧失浏览器 DevTools 排障能力。**决定不改**。
10. **「`TEST_COVERAGE.md` 过时」判断过时** —— 该文件**根本不存在**，无需同步。
11. **「376 处 mock，部分可能验证 mock 而非行为」数字不准但结论成立** —— 实测 mock 调用点 **1134 处**、**25%** 的测试函数含 mock（376 这个数字偏低）。但**真问题确实存在**且经**变异测试**证实：`tests/test_double_reply.py` 的「防双重回复」用例**自己重写了一遍生产消费逻辑**（`if already_sent: pass / elif reply.text: _send_reply(...)`），把 `runtime_llm_reply.py:134` 的判断改成恒假它**依然全绿**；「LLMProcessingError + DLQ 开启」的死信分支从未被覆盖，删掉 `_enqueue_dead_letter` 调用不会被发现。三处变异对**全仓库 4515 个用例零报警**。→ 已新增 7 条测试直接驱动生产方法 `_process_llm_reply`，变异后旧测试 failed=0 / 新测试各 2 failed。
12. **`.env` 自动查找不可依赖** —— `python-dotenv` 的 `load_dotenv()` 不传路径时会调 `find_dotenv()`，后者靠**遍历调用栈帧**推断「用户代码所在目录」；调用链较深时一路退到栈底，触发 `assert frame.f_back is not None` → AssertionError。该脆弱点**本机测不出来、只有 CI 暴露**（本机 `.env` 存在且调用链不同）→ 改为**显式指定路径**（`src/paths.py::get_app_root()`），传了 `dotenv_path` 即不走帧遍历。同理，**测试不能依赖仓库里存在 `.env`**（在 `.gitignore` 里，CI 的 checkout 没有）→ 改用 `tmp_path` 造文件 + 打桩解析根。
13. **「提示注入仅靠 `final_guard` 近因约束」描述不准** —— `final_guard` 防的是**输出泄漏**（不展示系统指令/思考过程），**不防注入**，二者是不同问题；项目另有 `src/gate/reply_gate.py` 关键词规则，但 `scripts/seed_gate_rules.py` **无自动调用点**（默认部署不生效）、只查**当前**消息不查历史与 RAG。真正空档是**角色错配**：RAG 片段与公共记忆被提升为 `role:"system"` 且前导语强调「最高优先级/唯一事实来源」→ 文档里的指令会被当成系统指令执行。

**尚未实施**

- **（P1-5 试点已完成）** 唯一真重复的 `_normalize_chat` 已下沉到基类模板方法（3 个类属性注入）+ 差分等价性测试（旧实现原样复制为 legacy 基准逐条断言相等）。**其余方法不要合并**：`_normalize_message`（嵌套 vs 扁平 sender、content 路径、时间字段）、`_classify_error`（错误包结构/分类顺序/错误码集）、`run`（重试策略相反）语义真实分叉。后续低风险收益点：wecom 文件内 send/reply 的 payload 重复（`wecom.py:723-777` vs `:779-828`）+ `MAX_DOWNLOAD_SIZE` 常量重复（`base.py:301` vs `wecom.py:884`）。
- **（P1-4 续已完成）** 端点全集改用**公开 API** `app.openapi()` 取（含 router prefix 拼接），不依赖新版 FastAPI 的私有 `_IncludedRouter` 包装；敏感/公开两张清单外即「未表态」→ CI 红。
- **（P2-1 步骤 2 已完成）** `base.py` 已瘦身（移除 43 个纯重导出，只保留 6 个自有定义 + TYPE_CHECKING 类型块）。
  ⚠️ **`load_dotenv()` 搬迁仍是最高风险项，须另开专项**：实测 Web 进程**当前根本不加载 .env**
  （`import web.api` → 0 次调用），只在 `main.py` 加载会**改变 Web 侧行为**；且
  `src/memory/account_identity.py:38` 在**导入期**读 `DWS_BIN`，dotenv 时序变动会让该覆盖静默失效
  （钉钉账号隔离退化为 `dingtalk:unknown`）。
- **（P2-6 已按真问题收窄）** 原「68 处 print 全改 logger」不成立（skills 的 stdout 是喂给 LLM 的输出契约，改 logger 会破坏契约）→ 只修「内部堆栈进 stdout」这一项：15 处 `traceback.print_exc()` 改投 stderr。
- **（P2-7 已完成）** `config_models.py` 已拆为包 `src/config_models/`（`_const` / `platform` / `llm` / `rules_tools` / `memory_skills` / `system` / `app`），导入面对消费者完全不变。
- **（P2-8 已完成）** 重排器命名口径已统一为「自研 2-gram 打分（非 BM25）」。
- **（P3-4 已完成）** 告警系统已接线 + webhook 渠道已消费（不再是死配置）。
- **（P3-5 已完成）** 嵌入模型口径已同步；`package.json` 已补 `name`/`version`。
- **（P3-6 判断证伪，不改）** sourcemap 无敏感信息且源码本就公开 → 关闭无实质收益，保留以便排障。
- **（P3-2 已完成）** 提示注入面已缓解：RAG 片段 / 公共记忆 / 历史 user 消息加「不可信数据」边界标记 + 来源标注（**正文逐字保留、零误伤**）。**当前用户消息有意不包裹** —— 它是用户此刻的真实诉求，包裹会降低模型对正常意图的敏感度。**不改 `role`** —— RAG/记忆的 system 角色承载「近因位权重提升」设计（2026-08-20 事故教训），降级回归风险大于收益；包裹发生在块内部，故 `system_content.endswith(_rag_block)` 的剥离逻辑不受影响。
- **（P3-3 已完成）** 双重回复防护与死信队列分支已补齐生产覆盖（变异测试验证有效）。
- **（P3-1 经量化评估后决定不做）** 意图分类 LLM 兜底。实测数据（20 条真实样本）：
  关键词分类器在业务消息 **8/8**、社交客套 **7/8**、极短业务词 **8/8** 全部正确；
  唯一误判是「麻烦了」被判 business。**但按 `confidence < 0.6` 触发 LLM 兜底会命中 12/20
  （60%）样本** —— 因为 `business` 的 confidence 只在命中 ≥5 个关键词时才是 1.0，
  单个业务词命中仅 0.5、纯业务名词仅 0.2。即「按置信度兜底」会让**绝大多数正常业务消息**
  都多走一次 LLM 调用（成本与延迟翻倍），而纠错收益仅 1/20。
  且该决策点的错误是**双向且不对称**的：社交消息误判为 business → 对每条「谢谢」都生成
  回复（刷屏 + 成本）；业务消息误判为 social → 该回的不回（业务事故）。两侧都不可接受。
  **结论：这是产品成本决策而非工程缺陷，不实施**；若日后确需，正确的触发条件应是
  「**仅** social 判定为 business 且内容为短客套」这种窄口径，而非通用置信度阈值。
- **（dotenv 专项已完成 + Web 侧边界已拍板）** Web 进程**不加载** `.env` —— **有意为之的安全默认**：`.env` 里通常是开发机的个人密钥，Web 面板是对外暴露的面，自动加载等于把本机密钥带进对外服务；需要 Web 侧密钥时用**环境变量**注入。该边界已升级为**显式契约**（`docs/configuration.md` 的「.env 加载契约」小节）+ **双层守护**（运行时计数桩 + 源码级断言：`web/` 下出现 `load_dotenv`/`dotenv_loader` 即 CI 红）。`.env` 加载入口已显式化：新增 `src/dotenv_loader.py::load_env()`（幂等封装），**真正入口**移到 `main()` 首行，`base.py` 保留一次幂等兜底 —— 全仓 AST 扫描确认有 **3 处模块级** env 读取（`account_identity.py:38` 的 `DWS_BIN`、`audit.py:33`、`web_search.py:27`），这些模块可能在 `main()` 之前被导入，缺了兜底 `DWS_BIN` 覆盖会静默失效（钉钉账号隔离退化为 `dingtalk:unknown`）。**Web 进程行为刻意不变**（仍不加载 `.env`）—— 改变它会改变 Web 侧行为，属独立决策。

---

## 📌 审计结论：全部条目已处置完毕

| 批次 | 条目 | 状态 |
|---|---|---|
| P0 | P0-1 / P0-2 / P0-3 | ✅ 全部完成 |
| P1 | P1-1 ~ P1-5 | ✅ 全部完成（P1-5 为试点，其余方法经 AST 差分证实语义真实分叉，**不可合并**） |
| P2 | P2-1 ~ P2-8 | ✅ 全部完成（P2-1 分两步；P2-6 原判断证伪后按真问题收窄；P2-7 完成拆分） |
| P3 | P3-1 ~ P3-6 | ✅ 全部处置（P3-1 经**量化评估后决定不做**；P3-6 判断证伪、**决定不改**；其余均已实施） |
| 专项 | `load_dotenv()` 搬迁 | ✅ 已完成（行为等价） |

**累计**：本轮（审计落地）共 **30 个 commit**（其中本批次 15 个）；过程中**证伪 12 处原判断**（详见上文逐条标注）—— 审计方案本身亦有 6 处判断被后续修复证伪，说明「先校准事实、再动手」这一步是必要的。

