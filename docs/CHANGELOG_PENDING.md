# Linkora 待发布变更（自动生成，请勿手工编辑）

> 生成时间：2026-09-11　|　起始 tag：v0.5.0（自动取仓库最新 tag）　|　生成方式：`scripts/gen_docs.py --changelog`


## 2026-09-11 (未发布)


> 自 v0.5.0 以来的变更


### 缺陷修复

- fix(llm): extra kwargs 显式声明 dict[str, Any]，修复 pyright 类型报错
- fix(llm): 重排模型跟随 embedding.device，避免自动选到 MPS 吃掉 1GB 显存
- fix(memory): model_kwargs 改用 dict[str, Any]，修复 pyright 类型报错
- fix(web): 严格问答模式简化为单一开关

### 性能优化

- perf(memory): 常驻内存优化，向量模型推理设备可配 + SQLite 页缓存瘦身

### 文档

- docs(changelog): 未发布段补充常驻内存优化说明

