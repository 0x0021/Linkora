# RAG 知识库

## 支持格式

| 格式 | 解析方式 | 备注 |
|---|---|---|
| PDF（文本） | `pdfplumber` + `pymupdf` | 优先前者，失败回退 |
| PDF（扫描版） | `pymupdf` 渲染 + `pytesseract` OCR | 需要安装 tesseract |
| Word | `python-docx` | |
| PPT | `python-pptx` | |
| 图片 | `pytesseract` OCR | jpg/png 等 |
| Markdown / 文本 | 直接读取 | |
| URL 网页 | `requests` + BeautifulSoup | 自动清洗 HTML |
| 钉钉文档 | `dws doc export` + 上述解析 | 支持自动定时同步 |

## 处理流程

```
原始文件 → 解析为纯文本
        → 自动清洗（去 HTML/Markdown/控制字符）
        → 标题粘连预处理（见下）+ 按 chunk_size（默认 500）切块，overlap 50
        → BGE 向量化（本地，offline）
        → 写入 FAISS 索引 + SQLite 元数据
```

### 分块预处理（标题粘连，Feature C）

`split_text()` 在切块前先做预处理：将标题行（`# 标题` / `第X章` / `1. xxx` / `一、xxx`）与紧随其后的正文粘连为一个段落，避免标题独占一块、正文被切到下一块导致语义割裂。随后按 `chunk_size`（默认 500，可配）切块，块间重叠 `chunk_overlap`（默认 50）。

## 检索策略

`kb_search` 工具调用时：

1. 用 BGE 把 query 编码为向量
2. FAISS Top-K 召回
3. 混合重排序（`SimpleReranker`，向量相似度权重 0.6 + BM25 关键词权重 0.4）：对召回结果按综合得分重排，把真正相关的结果顶上来
4. 截断 Top-K 结果送回 LLM

## 向量化配置

- 模型：`BAAI/bge-small-zh-v1.5`（中文场景，默认；部署可切换为 `./data/models/bge-large-zh-v1.5` 获得更高质量）
- 模式：**强制离线**（`HF_HUB_OFFLINE=1` + `local_files_only=True`）
- 加速：Apple Silicon 自动启用 MPS
- 存储：本地 `models/` 目录或 `./data/models/` 目录缓存

## 相关配置

```yaml
embedding:
  enabled: true
  model: BAAI/bge-small-zh-v1.5
  top_k: 5

rag:
  chunk_size: 500
  chunk_overlap: 50
```
