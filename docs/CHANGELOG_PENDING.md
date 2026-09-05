# Linkora 待发布变更（自动生成，请勿手工编辑）

> 生成时间：2026-09-05　|　起始 tag：v0.4.8（自动取仓库最新 tag）　|　生成方式：`scripts/gen_docs.py --changelog`


## 2026-09-05 (未发布)


> 自 v0.4.8 以来的变更


### 缺陷修复

- fix(summary): 修复摘要取值范围错误导致混合不相关内容
- fix(embedding): 放宽持久化状态的过期判定，避免 worker 无查询时 web 误判
- fix(web): 模型状态页正确展示 worker 维护的 embedding 状态
- fix(llm): 修复续写补全把模型编造内容拼进对外回复
- fix(deps): 对齐声明依赖至锁文件已验证版本，消除 uv run 升级污染 venv 根因
- fix(dws): 瞬时连接断开误判不可重试 + 清理 3 处 pyright 类型错误
- fix(tools): 副作用工具加幂等护栏，修复 at-least-once 重放双发 [P3]
- fix(web): 修复 image.py 类型注解令 pyright CI 通过；RAG 概览卡片横向铺满
- fix(memory): D10 修复活跃消息删除图片回收路径错配，避免 data/tmp_images 磁盘泄漏
- fix(platform): D4 修正活跃会话库路径匹配，避免误删活跃账号图片
- fix(data): 全局表保留期清理(D7) 与飞书陈旧索引修复(D9)
- fix(platform): 同步线程协作式退出与周期 WAL checkpoint（D2/D3）
- fix(embedding): KBSearchTool 复用 runtime_setup 共享 EmbeddingClient，消除每进程重复加载模型
- fix(launcher): 跨进程日志去重修正——首条缓冲等伙伴、短时重复折叠，杜绝双份
- fix(proactive): 主动触达摘要加跨进程文件锁防止 web+worker 双发重复

### 重构

- refactor(llm): 移除固定1小时周期摘要，改为信号驱动动态摘要

### 性能优化

- perf(platform): web 模式不再预加载嵌入模型，由 worker 常驻负责

### 文档

- docs(changelog): 同步摘要取材范围修复（未发布段）
- docs: overview 补充 D4 修复运行时验证与 14:43 误删事件说明
- docs: 标记 D5/D6 已隔离删除回收 4.5G（bge-m3 + 遗留备份）

### 杂项

- chore(deps): bump actions/setup-python from 5 to 7 (#24)
- chore(deps): bump astral-sh/setup-uv from 5 to 7 (#23)
- chore(deps): bump actions/upload-artifact from 4 to 7 (#22)
- chore(deps): bump actions/checkout from 4 to 7 (#21)
- chore(deps): bump the python-deps group with 5 updates (#25)

