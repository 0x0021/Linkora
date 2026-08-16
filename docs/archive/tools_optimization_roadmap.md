# Linkora 工具（src/tools/）优化路线图

> 滚动维护：第一批已落地（2026-08-02），第二批已落地（同日），后续按优先级持续推进。

## 框架现状（基线）
- 37 个工具（第二批去重 1 个→实为删 2 留 1），基类 `BaseTool` 抽象方法 `execute(args)`；错误协议「返回 `{error:...}` 而非抛异常」。
- 确认门控：`require_confirm` + `build_confirmation_preview` + 本次新增的 `needs_confirm(args)`（按参数条件确认）。
- `ToolRouter` 统一限流 / 审计 / 确认令牌。
- 安全合规：出站 HTTP 全过 `ssrf_safe_get`；无裸 `json.loads`（解析 LLM 输出）；无 `TODO/FIXME`；无裸 `except:`。

## 第一批已落地（6 commits，gitleaks 通过，全量回归 3240 通过）
| 提交 | 改动 |
|------|------|
| `6030b3b` | weather `_geocode`：失败结果不再被 `lru_cache` 永久缓存（修复城市永走 wttr.in 兜底） |
| `f1886cc` | doc/contact/calendar/conversation/memory 外部 dws 调用加 `try/except` + `isinstance` 守卫；memory 加整体保护、防 KeyError |
| `eed25d2` | minutes 转写原文超长截断（`MAX_TRANSCRIPT_CHARS=6000`），防冲爆 LLM 上下文 |
| `29fa31b` | config_manage 写盘加二次确认（仅 `update` 需确认）+ 修复 LLM 传 JSON bool/int 时类型强转崩溃；`base.py` 新增 `needs_confirm` 钩子 |
| `aa87505` | web_search 返回 payload 剥离内部字段（`_source/_from_query/total_raw/total_dedup` 改 logger）+ 结果够数即早退，降延迟/配额 |
| `7df6788` | `utils.py` 新增 `arg_str` / `list_result` 辅助，统一参数解析三件套 |

新增/更新测试：test_weather_geocode_cache、test_minutes_truncate、test_config_manage_guard、test_tools_utils_helpers，以及 web_search 两个测试适配新契约。

## 第二批已落地（4 commits，gitleaks 通过，全量回归 3210 通过）
| 提交 | 改动 |
|------|------|
| `34aed76` | send_ding 强提醒确认门控：`needs_confirm(args)` 仅 `sms`/`call` 需二次确认，`app` 直接放行；预览只读预检 |
| `6369011` | web_search `_clean_and_rank` 死代码统一：支持多 query（取任一 query 最高分），execute 复用单一实现，删除 30 余行内联重复；测试兼容字符串/列表 |
| `2cfaffc` | oa_approval/wiki 共享 `_coerce_limit`/`list_result`：消除 4 处重复定义与 9 处返回模板，51 项定向测试通过 |
| `e1e138a` | 收敛双套审批工具：删除 business 的 get_my_approvals/get_approval_detail，保留 oa_approval 的 approval_list_pending/approval_get_detail 为唯一真源；`approval_list_pending` 的 start/end 改可选（默认近 30 天）无缝承接原职责；同步清理 registry/config.py 默认值+rate_limit/磁盘 config.yaml/intent.py TOOL_ACTION_MAP/三份文档，白名单漂移防护测试自动校验三方一致 |

新增/更新测试：test_send_ding_confirm、test_web_search_utils（多 query 锁定）、test_tools_oa_approval（默认窗口/部分窗口）。

## 第三批已落地（4 commits，gitleaks 通过，全量回归待跑）
| 提交 | 改动 |
|------|------|
| `1881728` | kb_search 重构：`_search_by_embedding`/`_search_by_embedding_vector` 合并为 `_search_kb_embedding`，embed 逻辑上提到 `search()` 入口；三处格式化统一 `_format_hit`；execute 恢复基类契约（原关键字调用收敛为公开 `search()`，style.py RAG 注入改用它） |
| `c514347` | `BaseTool.safe_execute` 模板方法：异常兜底下沉到基类（未捕获异常→`{error}`+保留 traceback），`ToolRouter._run_tool` 经 safe_execute 执行；新工具实现 execute 即自动获异常保护 |
| `e34f79b` | P3：chat.py 空 if/elif pass 块删除；`llm_json.extract_last_json` 新增（取最后一个 JSON，与 extract_json 语义互补），`im_adapter._extract_json` 委托之，消除裸 raw_decode 副本 |
| `b46c925` | P3：冗余日志+pass 清理（utils/web_search 双条同文案→单条语义化），f-string 日志统一 `%s` 惰性求值 |

新增/更新测试：test_kb_search（_search_kb_embedding 迁移+复用向量不重复 embed）、test_tools_base（safe_execute 3 项）、test_llm_json_extract（extract_last_json 6 项）。

## 待做（按优先级）
- **P1** ~~business.py 与 oa_approval.py 双套审批工具去重~~ ✅ 已落地（e1e138a）
- **P1** ~~web_search `_clean_and_rank` 死代码统一~~ ✅ 已落地（6369011）
- **P1** ~~send_ding 的 `sms`/`call` 确认门控~~ ✅ 已落地（34aed76）
- **P2** ~~kb_search 两个向量检索方法合并 + `_format_hit` 抽取~~ ✅ 已落地（1881728）
- **P2** ~~`BaseTool` 加 `safe_execute` 模板方法~~ ✅ 已落地（c514347）
- **P3** ~~chat.py 空 `if/elif pass` 块~~ ✅ 已落地（e34f79b）；~~冗余日志+`pass`~~ ✅ 已落地（b46c925）；~~`im_adapter/base.py` 私有 `_extract_json` 改用 `llm_json`~~ ✅ 已落地（e34f79b）
- **P3** ~~test_embedding.py macOS segfault + 断言过期~~ ✅ 已落地（4f38127，33→55 项覆盖，全量回归不再 --ignore）

## 暂缓（结构性整理，收益/风险比差，待用户明确要求再做）
- **P2** `parse_document.py`（934 行非工具，被 poller_core_ocr 引用）移出 tools/：纯目录整理，需改 2 处生产 import + 6 处测试 import，不修任何真实问题。
- **P2** `utils.py` 的 `split_text`/`cross_process_lock` 拆到 `text/chunking`、`utils/locking`：被 4 个生产模块引用（含 db_backup / doc_sync_scheduler 两条关键链路），纯结构改动风险不小。
- **P3** tools/ 目录内 `_coerce_limit` 的可见性：约定类提示，仅需注意勿再扩散手写 `int()` 强转。

## 结论
tools/ 优化高价值项已全部完成；继续投入边际价值递减。下一步建议：**重启 bot 验证本次改动真实运行效果**（冒烟测试 send_ding 确认门控 / kb_search / config_manage），而非继续结构整理。

## 约定
- 改 `src/` 后需**重启 bot** 生效（非配置热重载）。
- 新增工具：在 `registry.py` 的 `BUILTIN_TOOL_MANIFEST` 追加一行类名即可，依赖由 `build_tool` 自动注入。
- 新增工具实现 `execute(args)` 后**无需手写 try/except**——`BaseTool.safe_execute` 已兜底；错误协议统一「返回 `{error}` 不抛异常」。
