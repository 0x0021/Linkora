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

## 严格问答模式（智能问答）

**定义**：一个全局可开关的问答模式。开启后，系统**所有问答请求**一律先检索知识库，
回答**只能**来自检索到的知识库内容——不调用通用大模型知识，不做外部推理；
知识库无相关内容时**直接返回固定「未收录」文案**（这一条是硬保证：此时根本不调用大模型，
从源头上杜绝编造）。关闭时完整恢复原有问答逻辑（RAG 自动注入 + 意图门控 + 三级递进兜底 +
工具/技能路由），行为与开启前完全一致。

### 启用与关闭

| 控制方式 | 说明 |
|---|---|
| 配置文件 | `llm.advanced.rag_strict_mode: true`（默认 `false` = 关闭） |
| Web 控制台 | 「设置 → 模型与参数 → RAG 严格问答模式」开关，保存后**热重载立即生效，无需重启** |

关闭只需把开关置回 `false` 并保存：严格模式的所有分支（强制检索、严格提示块、
工具收敛、未命中短路）都会整体跳过，回到原有链路。

### 开启状态下的检索与生成流程

```
消息进入
  ↓
① 强制检索：对任意消息做向量检索（跳过「意图门控」与「短消息过滤」，
   用 rag_strict_min_similarity / rag_strict_max_results 作为召回门槛）
  ↓
② 命中判断
  ├─ 未命中 → 直接返回 rag_strict_no_hit_reply，不调用 LLM、不追问、不兜底
  └─ 命中   → 把「知识库检索结果 + 仅依据资料作答」的硬约束块
              作为独立 system 消息放在用户消息之前（近因位）
  ↓
③ 生成：LLM 只做「基于资料的组织与复述」
   - 禁用技能激活与除 kb_search 外的所有工具（联网搜索/发消息/审批等一律剔除）
   - 不注入公共记忆（它不是知识库文档，会引入非 KB 事实来源）
   - 跳过三级递进兜底（其引导追问话术与兜底文案不属于知识库内容）
  ↓
④ 输出：回复内容只能由资料支撑；资料未覆盖的部分需明确说明「知识库中未收录」
```

### 相关配置

```yaml
llm:
  advanced:
    rag_strict_mode: false            # 总开关（默认关）
    rag_strict_min_similarity: 0.5    # 严格模式召回相似度门槛（覆盖 rag_min_similarity）
    rag_strict_max_results: 3         # 严格模式最多注入几条知识片段
    rag_strict_no_hit_reply: "知识库中暂未收录相关内容，我无法凭已有信息作答。"  # 未命中时的固定回复
```

**使用提示**：严格模式适合「制度问答 / 产品资料问答」这类要求答案 100% 可溯源的会话。
开启后闲聊、天气、问候等知识库未覆盖的话题也会得到「未收录」回复——这是预期行为，
如需兼顾日常对话，请保持关闭。开启前建议先把对应资料导入知识库并抽查召回效果，
再调 `rag_strict_min_similarity`（过高易漏召回、过低易引入弱相关片段）。Web 顶栏常驻模式徽章、设置页状态条与模拟面板的「对比两种模式」都能直观看到当前模式与两种模式的回答差异。

## 向量化配置

- 模型：`BAAI/bge-small-zh-v1.5`（中文场景，默认；provider 取值 `local` / `api`）
- 模式：**强制离线**（`HF_HUB_OFFLINE=1` + `local_files_only=True`）
- 加速：Apple Silicon 自动启用 MPS
- 存储：本地 `models/` 目录或 `./data/models/` 目录缓存
- 本地 embedding 服务：默认 `http://127.0.0.1:8910/v1`（provider=`api` 时走此地址）；**该服务不在时 RAG 自动降级为不检索**，不影响对话主流程
- 如需更强模型可切换为 `./data/models/bge-m3`（当前 `data/models/` 下实际提供的模型）

## 相关配置

```yaml
embedding:
  enabled: true
  model: BAAI/bge-small-zh-v1.5
  provider: local                 # local / api
  base_url: http://127.0.0.1:8910/v1   # 本地 embedding 服务；不可达则 RAG 降级
  top_k: 5

rag:
  chunk_size: 500
  chunk_overlap: 50
```

RAG 检索默认：`rag_min_similarity=0.30`、`rag_max_results=4`、`rag_max_content_chars=1200`（低于相似度阈值不强行作答，转草稿 / 转人工）。
