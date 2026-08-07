# 灵桥 (Linkora) — 开发指南

## 环境要求

| 组件 | 最低版本 |
|------|---------|
| Python | 3.14+（仅 3.14 系列） |
| pip | 23.0+ |
| Git | 2.30+ |
| 操作系统 | macOS 13+ / Linux (Ubuntu 20.04+ / Debian 11+) / Windows |
| 磁盘空间 | 至少 5 GB（含模型下载与数据库） |
| 内存 | 至少 4 GB（BGE 模型加载约需 2 GB） |

### 平台 CLI 工具

根据目标 IM 平台安装对应的命令行工具：

| 平台 | CLI 工具 | 安装方式 |
|------|---------|---------|
| 钉钉 | `dws` | `curl -fsSL https://dtalkapp.sjtu.edu.cn:443/dwscript/install.sh \| bash` |
| 飞书 | `lark-cli` | 联系飞书管理员获取 |
| 企业微信 | `wecom-cli` | 联系企业微信管理员获取 |

---

## 安装步骤

```bash
# 1. 克隆仓库
git clone <repo-url> linkora
cd linkora

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量（可选，LLM 密钥敏感信息推荐用 .env）
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY 等密钥

# 5. 初始化配置文件
cp config.yaml.example config.yaml
# 编辑 config.yaml，至少配置 platforms[0].adapter.cli_path 和 llm.* 段
```

### 依赖说明

依赖全部**精确钉版**（`==`），以保证 CI、Docker 与 PyInstaller 打包三处装到同一套版本。
四处声明各司其职，**必须保持一致**：

| 文件 | 角色 | 谁消费 |
|------|------|--------|
| `requirements.txt` | **直接依赖唯一真源**（31 条，全 `==`） | `Dockerfile`、`Dockerfile.build` |
| `pyproject.toml` `[project].dependencies` | 逐条镜像 `requirements.txt` | 打包元数据、`uv` |
| `requirements.lock` | 完整传递闭包（113 条，跨平台标记） | CI 安装与 `pip-audit` 扫描 |
| `uv.lock` | `uv` 工作流锁文件 | `uv sync` / `uv run` |

核心依赖分组见 `requirements.txt` 内注释（配置 / LLM / Web / 向量检索与 OCR / 文档解析 / 系统工具）。

> Python 下限为 **3.12**，由 `numpy==2.5.1` 决定（它没有 cp39–cp311 的 wheel）。
> 放宽 `requires-python` 前请先确认所有钉版依赖都提供对应 wheel。

#### 改依赖的正确姿势

```bash
# 1. 改 requirements.txt（新增/升级，必须写 == 精确版本）
# 2. 同步 pyproject.toml 的 [project].dependencies（逐条镜像）
# 3. 重新生成两个锁文件（需要 uv >= 0.11）
bash scripts/lock_deps.sh

# 4. 校验四处声明一致（CI lint 阶段会跑同一个脚本，不一致直接红）
python scripts/check_deps.py
```

`scripts/check_deps.py` 是纯标准库、不联网的离线门禁，会断言：
直接依赖全部钉版、`pyproject.toml` 与 `requirements.txt` 逐条一致、
两个锁文件都覆盖全部直接依赖且版本相同、`requires-python` 与 CI 矩阵 / Docker
基础镜像的 Python 版本相容（加 `--strict` 可把版本相容告警升级为失败）。

首次运行时，`sentence-transformers` 会自动从 HuggingFace 下载 BGE 模型（约 1.3 GB），请确保网络通畅。

---

## config.yaml 配置说明

`config.yaml` 是灵桥的**唯一配置入口**。从 `config.yaml.example` 复制后，按需修改以下关键段落：

### platforms（多平台隔离）

```yaml
platforms:
  - id: dingtalk                  # 平台唯一标识
    display_name: 钉钉            # 管理台展示名称
    enabled: true                 # 是否启用
    adapter_type: dingtalk        # 适配器类型：dingtalk / feishu / wecom
    storage:
      type: sqlite
      path: ./data/linkora.db     # 数据库文件路径
      backup_enabled: true        # 是否启用自动备份
      backup_dir: ./data/backups
      backup_interval_hours: 24
      backup_max_count: 7
      backup_on_start: true
      decisions_retention_days: 14
      messages_retention_days: 90
```

### poller（轮询器）

```yaml
    poller:
      interval_seconds: 10        # 轮询间隔
      unread_conversation_count: 50
      messages_per_conversation: 16
      merge_window_seconds: 5     # 同会话消息合并窗口
      history_window: 6           # LLM 上下文历史条数
      reply_cooldown_seconds: 10  # 回复冷却
      max_concurrent_replies: 4   # 并发回复上限
```

### llm（大语言模型）

```yaml
llm:
  api_key: ""                     # 主服务商 API Key（推荐通过 .env/LLM_API_KEY 设置）
  base_url: "https://kenari.id/v1"
  model: "your-model-name"
  fallback_api_key: ""            # 备用服务商（触发 429/超时后自动切换）
  fallback_base_url: "https://integrate.api.nvidia.com/v1"
  fallback_model: "deepseek-ai/deepseek-v4-flash"
```

### embedding

```yaml
embedding:
  enabled: true                         # 默认 false；开启后 RAG 检索生效
  model: "BAAI/bge-small-zh-v1.5"      # BGE 中文向量模型（local 模式自动下载缓存）
  provider: local
  offline: true                         # 强制离线，禁止联网下载
```

### rules（规则引擎）

```yaml
rules:
  enabled: true
  blacklist:                            # 黑名单中的用户/群组消息不处理
    users: []
    groups: []
  keywords:                             # 关键词规则（DB 管理，热更新）
    - match: "在吗"
      reply: "在的，请问有什么事？"
  intent_filter:                       # 意图过滤：跳过无业务价值的社交消息
    enabled: true
```

### logging

```yaml
logging:
  level: DEBUG
  file: ./logs/linkora.log
  max_size_mb: 50
  max_backups: 5
```

### web

```yaml
web:
  port: 8080
  host: "127.0.0.1"               # 安全默认仅本机回环；公网暴露须经反代+认证
  auth_enabled: true              # 默认开启；auth_password 为空且开启会启动报错（fail-closed）
  auth_username: admin
  auth_password: ""               # 通过 .env/WEB_AUTH_PASSWORD 或 config.yaml 显式设置
```

完整配置项说明参见 `config.yaml.example`（约 470 行，含详细注释）。

---

## 启动命令

```bash
# 基础启动（仅消息处理，不启动 Web 管理台）
python main.py

# 启动 Web 管理台（默认端口从 config.yaml web.port 读取）
python main.py --web

# 指定 Web 端口
python main.py --web 8080

# 指定配置文件路径
python main.py custom_config.yaml --web 8080

# 开发模式（开启调试日志等）
python main.py --web 8080 --dev

# 规则测试模式（不启动服务，仅测试规则命中）
python main.py --test-rule "今天天气怎么样"
```

### 命令行参数一览

| 参数 | 说明 | 示例 |
|------|------|------|
| `--web [PORT]` | 启动 Web 管理台，可选指定端口 | `--web 8080` |
| `--test-rule TEXT` | 规则测试模式 | `--test-rule "你好"` |
| `--dev` | 开发模式 | `--dev` |
| `[config_path]` | 配置文件路径（位置参数） | `custom_config.yaml` |

---

## 测试运行

### 运行全部测试

```bash
# 基础运行
pytest

# 带覆盖率报告
pytest --cov=src --cov-report=term-missing

# 并行运行（需安装 pytest-xdist）
pytest -n auto
```

### 运行特定测试

```bash
# 按文件名
pytest tests/test_rule_engine.py

# 按关键词
pytest -k "blacklist"

# 按标记
pytest -m "integration"
```

### 测试结构说明

```
tests/
├── conftest.py                    # 共享 fixtures（store、config mock 等）
├── test_rule_engine.py            # 规则引擎（黑名单/冷却/关键词/意图）
├── test_poller.py                 # 轮询器核心逻辑
├── test_sqlite_store.py           # 数据库 CRUD
├── test_skill_loader.py           # 技能加载
├── test_skill_router.py           # 技能路由
├── test_tool_wrapper.py           # SkillTool 自动包装
├── test_tool_routing.py           # 工具路由
├── test_tools_*.py                # 各内置工具专项测试
├── test_decision_tracker.py       # 决策追踪
├── test_metrics.py                # 可观测性指标
├── test_web_api_endpoints.py      # Web API 端点
├── test_integration_pipeline.py   # 集成测试（端到端管线）
├── test_im_adapter.py             # IM 适配器接口
├── test_feishu_adapter.py         # 飞书适配器
├── test_wecom_adapter.py          # 企微适配器
├── test_llm_*.py                  # LLM 客户端/限流/异常
├── test_rag_*.py                  # RAG 检索/注入/门控
├── test_embedding.py              # Embedding 客户端
├── test_vector_index.py           # FAISS 索引
└── ...（共约 190 个测试文件，tests/ 下）
```

### 测试超时

所有测试配置了 60 秒超时（`pyproject.toml` 中 `--timeout=60`），防止网络/IO 卡死拖垮流水线。超时方法为 `signal`（可中断 C 层阻塞调用）。

---

## 代码风格约定

### Type Hints

强制使用类型注解：

```python
from __future__ import annotations

def get_store(db_path: str | None = None) -> SQLiteStore:
    ...
```

- 所有公开方法**必须**包含参数类型与返回值类型
- 使用 `from __future__ import annotations` 启用延迟求值
- 复杂类型使用 `TYPE_CHECKING` 避免循环导入

### Docstring

```python
def init_schema(conn: sqlite3.Connection, db_path: str) -> None:
    """初始化/迁移数据库 schema。

    该函数幂等：所有 DDL 使用 IF NOT EXISTS 或前置列检查。
    """
```

- 公开函数/类**必须**有 docstring
- 描述"做什么"而非"怎么做"
- 关键约束（幂等性、副作用）显式说明

### Repository 模式

新增数据访问**必须**遵循 Repository 模式：

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore


class NewDomainRepo:
    """Repository for new_domain operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def do_something(self, ...) -> ...:
        cur = self.store.conn.cursor()
        cur.execute("SELECT ...")
        ...
```

- 构造参数仅接收 `SQLiteStore` 实例
- 通过 `self.store.conn` 获取线程级连接
- **严禁**将数据库连接逻辑写回 `SQLiteStore` 主类

### 会话数据隔离（conv_conn 铁律）

数据库按「主库 / 按平台会话库」双层隔离，**读写位置弄错会导致数据不可见或跨平台串台**（曾导致会话图片回填永不生效、测试出现全空/串台假阳性）：

- **主库** `self.store.conn`：平台无关元数据、知识库（kb_*）、路由质量（routing_quality）、成本统计等。
- **按平台会话库** `self.store.conv_conn(platform)`：消息（messages）、会话摘要（conversation_summaries）、去重（dedup_messages）、黑名单（blocked_conversations）等**按平台隔离**的会话数据。

铁律：

- 凡涉及 `messages` / `conversation_summaries` / `dedup_messages` / `blocked_conversations` 等会话数据，**必须**走 `store.conv_conn(platform)`（经对应 `*_repo` 的 `_cc()` / `_cc_for(platform)`），**严禁**误用主库 `store.conn`。
- Web 层与脚本需要平台上下文时，必须通过 `platform_scope()` 取当前平台再定位会话库；缺省平台按 `dingtalk` 回落。
- 测试种子/断言若写到主库而代码读会话库（或反之），会出现「用例全空 / 串台」的隔离假阳性——务必对齐读写库。

### 配置热重载

配置修改后无需重启，灵桥自动检测 `config.yaml` 的 mtime 变化并重新加载。开发时注意：
- 新增配置字段需在 `src/config.py` 的 Pydantic 模型中声明
- 默认值在模型 `Field(default=...)` 中定义
- 敏感字段（密钥）通过 `.env` 环境变量注入，不在 config.yaml 中明文存放

### 日志

```python
import logging
logger = logging.getLogger(__name__)

logger.info("message")
logger.debug("detail")
```

- 使用模块级 logger
- 敏感信息（API Key、Token）会被 logger 自动脱敏
- 生产环境日志级别 `INFO`，开发调试可临时改为 `DEBUG`
