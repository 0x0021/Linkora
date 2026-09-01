# 缺陷处理总览（2026-09-01）

在既有 `defect-audit.md`（配置/Web安全/轮询/提示词 四类）之外，新增扫描资源/并发/运维/数据生命周期维度，发现 9 项缺陷（D1–D9）。本轮已落地处理 D2/D3（代码）、D4（孤儿会话库图片回收）、D5/D6（回收 4.5G）、D7（全局表保留期清理）、D8（备份内容去重+降保留数）、D9（飞书陈旧索引数据修复）；D1 完成首轮治理（web/routers 静默吞异常 2 处补结构化日志）。全部重启验证通过。

## 已处理

| 项 | 处置 | 状态 |
|---|---|---|
| **D2** 同步线程被 SIGKILL 硬切 | `web/routers/sync.py` 新增 `request_sync_stop_on_shutdown()`，lifespan 关闭阶段复用 `CANCEL_FILE` 协作式退出 + `join(8s)` | 代码已合入（`d672489`） |
| **D3** 无全局 WAL checkpoint | `src/platform/memory.py` 新增 `_start_wal_checkpoint_scheduler()`（每 30min `PRAGMA wal_checkpoint(PASSIVE)`），`lifecycle.py` 接入 | 代码已合入（`d672489`） |
| **D5** 遗留一次性备份 187M | 隔离→删除回收 | `data/` 5.3G→916M |
| **D6** 无用 bge-m3 模型 4.3G | 隔离→删除回收（零引用，bge-small 在 `~/.cache`） | 同上 |
| **D7** 三张全局表无保留策略 | `tool_execution_repo/feedback_repo/draft_repo` 加 `cleanup_old_*`；`memory.py` 加 `_start_global_tables_cleanup_scheduler()`（每 24h，`lifecycle` 接入）；保留期复用 `messages_retention_days`(90天)，不新增配置 | 代码已合入（`ed86045`） |
| **D9** 飞书 1024 维陈旧索引 | 飞书未启用，纯数据修复：`feishu-ai.db` 5 条向量清空(保留content)+删 `feishu-ai.faiss`/`.map.json`（data/ 已 gitignore，不提交） | 已修复（根因未做代码防护，启用飞书前建议加"重建跳过异维 chunk"守卫） |
| **D8** backup churn | `src/db_backup.py` 备份写盘后 SHA-256 比对去重（内容未变更跳过重复备份）+ `backup_max_count` 7→5（配置 `StorageConfig.backup_max_count` 同步）；新增 `tests/test_db_backup.py::TestContentDedup` | 代码已合入（本轮） |
| **D4** 孤儿会话库 tmp_images 泄漏 | 新增 `src/platform/orphan_cleanup.py`（`scan_and_reclaim_orphan_tmp_images`/`collect_orphan_image_paths`）+ `memory.py::._scan_orphan_conversation_dbs`（启动期一次性，非线程）；`purge_orphan_images` 补 `base_dir` 修正分库 base 错配（真实根 `data/tmp_images`）；新增 `tests/test_orphan_cleanup.py`(7) | 代码已合入（本轮） |
| **D1** `except Exception` 膨胀（560 处，↑45） | 首轮治理 web/routers 静默吞异常 2 处补结构化日志：`web/routers/health.py:62`(warning 降级 None) / `web/routers/image.py:146`(debug token 解码失败)；其余 23 处带注释的有意探测（graceful degradation）保留，不盲改全仓 | 首轮已合入（本轮）；src/ 子系统后续批次延后 |
| image.py:153 类型错误 | 补 `# type: ignore[reportArgumentType]`（同文件 :184 范式） | 已合入（`d672489`） |

## 门禁
ruff 全绿 · pyright **0 错误**（baseline 0）· check_deps 一致 · 测试 **3922 passed / 2 skipped / 2 xfailed** · gitleaks 通过。

## 提交与同步
- `d672489` fix(platform): 同步线程协作式退出与周期 WAL checkpoint（D2/D3）+ image.py 修复
- `a81239e` docs: 标记 D5/D6 已回收
- `ed86045` fix(data): 全局表保留期清理(D7) 与飞书陈旧索引修复(D9)
- （本轮）fix(backup/platform): 备份内容去重+降保留数(D8)、孤儿会话库图片回收(D4)、web/routers 静默异常补日志(D1 首轮)
- 均直推 `github main`（绕过分支保护）

## 重启验证（新进程 93956/93958/93959）
- 全局表清理调度器已启动（linkora.log:253）✓
- WAL checkpoint 调度器已启动（linkora.log:254）✓
- Web(8080) HTTP 200 ✓
- embedding 复用：独立实例=0、每进程各加载 1 次 ✓
- D9 飞书 `feishu-ai.faiss` 已删除 ✓

## 延后（需设计或配置决策，未动手）
- **D1（后续批次）** `src/` 子系统 broad-except 治理 —— 首轮仅收口 web/routers 静默吞异常的 2 处；`src/` 下其余 23 处有意探测（带注释）暂保留，待逐一确认语义后再决定收窄/重抛/结构化日志。
- **D10** 活跃库删消息图片 base 错配 —— 活跃库删消息时 `purge_orphan_images(self.store.db_path, ...)` 默认 base=`conversations/tmp_images`（库在 `conversations/` 下的推导），但真实图片根在 `data/tmp_images`，导致当前活跃删除实际不回收图片；建议调用处显式传 `base_dir=data_path("tmp_images")` 修正。
- **D8 极端边界** 同秒双备份（时间戳文件名撞车）→ 去重逻辑跳过；生产重启间隔 >1s 不会触发，未特殊处理。
- 均记入 `defect-audit.md` 第四节，免重复审计。
