# 第三方开源组件声明

灵桥 Linkora 本体以 **GNU GPL-3.0-or-later** 发布（见 [LICENSE](LICENSE)）。
本文件列出 Linkora 在运行、构建、测试过程中**引用或分发**的全部第三方开源组件及其许可证，
既是合规义务的履行（署名 / 保留声明），也方便使用者做二次分发的许可证审计。

> **本文件是第三方组件清单的唯一真源。** README 与文档站仅提供摘要与跳转，不重复维护明细。
> 增删依赖时，请同步更新本文件（新增组件必须补上「许可证」与「上游地址」两列）。

## 目录

1. [Python 运行时直接依赖](#1-python-运行时直接依赖)
2. [前端第三方资源（随仓库分发）](#2-前端第三方资源随仓库分发)
3. [AI 模型与权重（运行时下载，不随仓库分发）](#3-ai-模型与权重运行时下载不随仓库分发)
4. [外部工具与系统依赖（需自行安装，不随仓库分发）](#4-外部工具与系统依赖需自行安装不随仓库分发)
5. [开发与 CI 工具链](#5-开发与-ci-工具链)
6. [Python 传递依赖（锁文件完整闭包）](#6-python-传递依赖锁文件完整闭包)
7. [许可证合规要点](#7-许可证合规要点)
8. [许可证全文索引](#8-许可证全文索引)

---

## 1. Python 运行时直接依赖

版本号与 `pyproject.toml` / `requirements.txt` 完全一致（两者由 `scripts/check_deps.py` 在校验中强制同步）。

| 组件 | 版本 | 许可证 | 在 Linkora 中的用途 | 上游 |
| --- | --- | --- | --- | --- |
| [PyYAML](https://pypi.org/project/PyYAML/) | 6.0.3 | MIT | 读取 / 写回 `config.yaml` | https://github.com/yaml/pyyaml |
| [pydantic](https://pypi.org/project/pydantic/) | 2.13.5 | MIT | 配置模型校验（`src/config_models.py`） | https://github.com/pydantic/pydantic |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | 1.2.3 | BSD-3-Clause | 加载 `.env` 环境变量 | https://github.com/theskumar/python-dotenv |
| [platformdirs](https://pypi.org/project/platformdirs/) | 4.11.8 | MIT | 跨平台用户数据 / 缓存目录 | https://github.com/platformdirs/platformdirs |
| [openai](https://pypi.org/project/openai/) | 2.54.0 | Apache-2.0 | LLM 客户端（含 OpenAI 兼容接口） | https://github.com/openai/openai-python |
| [numpy](https://pypi.org/project/numpy/) | 2.5.3 | BSD-3-Clause | 向量运算 / 索引 | https://github.com/numpy/numpy |
| [sentence-transformers](https://pypi.org/project/sentence-transformers/) | 5.7.0 | Apache-2.0 | 本地嵌入与 CrossEncoder 重排 | https://github.com/UKPLab/sentence-transformers |
| [huggingface_hub](https://pypi.org/project/huggingface_hub/) | 1.31.0 | Apache-2.0 | 模型 / 权重下载与缓存 | https://github.com/huggingface/huggingface_hub |
| [tokenizers](https://pypi.org/project/tokenizers/) | 0.23.2 | Apache-2.0 | HF 分词器运行时 | https://github.com/huggingface/tokenizers |
| [fastapi](https://pypi.org/project/fastapi/) | 0.141.1 | MIT | Web 管理台 HTTP 框架 | https://github.com/fastapi/fastapi |
| [uvicorn](https://pypi.org/project/uvicorn/) | 0.52.4 | BSD-3-Clause | ASGI 服务器 | https://github.com/encode/uvicorn |
| [jinja2](https://pypi.org/project/jinja2/) | 3.1.6 | BSD-3-Clause | 页面模板渲染 | https://github.com/pallets/jinja |
| [python-multipart](https://pypi.org/project/python-multipart/) | 0.0.32 | Apache-2.0 | 表单 / 文件上传解析 | https://github.com/Kludex/python-multipart |
| [PyJWT](https://pypi.org/project/PyJWT/) | 2.13.0 | MIT | 管理台登录令牌签发与校验 | https://github.com/jpadilla/pyjwt |
| [faiss-cpu](https://pypi.org/project/faiss-cpu/) | 1.15.0 | MIT | 向量索引与近邻检索 | https://github.com/facebookresearch/faiss |
| [requests](https://pypi.org/project/requests/) | 2.34.2 | Apache-2.0 | HTTP 抓取（URL 入库等） | https://github.com/psf/requests |
| [jieba](https://pypi.org/project/jieba/) | 0.42.1 | MIT | 中文分词（关键词 / 检索） | https://github.com/fxsjy/jieba |
| [regex](https://pypi.org/project/regex/) | 2026.9.10 | Apache-2.0 | 增强正则（规则引擎） | https://github.com/mrabarnett/mrab-regex |
| [rapidocr-onnxruntime](https://pypi.org/project/rapidocr-onnxruntime/) | 1.2.3 | Apache-2.0 | 本地 OCR（图片 / 扫描件） | https://github.com/RapidAI/RapidOCR |
| [beautifulsoup4](https://pypi.org/project/beautifulsoup4/) | 4.15.0 | MIT | HTML 正文抽取 | https://www.crummy.com/software/BeautifulSoup/ |
| [pdfplumber](https://pypi.org/project/pdfplumber/) | 0.11.10 | MIT | PDF 文本与表格抽取 | https://github.com/jsvine/pdfplumber |
| [pypdfium2](https://pypi.org/project/pypdfium2/) | 5.13.0 | Apache-2.0 OR BSD-3-Clause（捆绑的 PDFium 及各静态库另有许可，随包分发） | PDF 页面渲染（扫描版 PDF → 图片 → OCR） | https://github.com/pypdfium2-team/pypdfium2 |
| [python-pptx](https://pypi.org/project/python-pptx/) | 1.0.2 | MIT | PPTX 解析 | https://github.com/scanny/python-pptx |
| [python-docx](https://pypi.org/project/python-docx/) | 1.2.0 | MIT | DOCX 解析 | https://github.com/python-openxml/python-docx |
| [pytesseract](https://pypi.org/project/pytesseract/) | 0.3.13 | Apache-2.0 | Tesseract OCR 的 Python 封装 | https://github.com/madmaze/pytesseract |
| [Pillow](https://pypi.org/project/pillow/) | 12.3.0 | MIT-CMU (HPND) | 图像读写与预处理 | https://github.com/python-pillow/Pillow |
| [openpyxl](https://pypi.org/project/openpyxl/) | 3.1.5 | MIT | XLSX 解析 | https://foss.heptapod.net/openpyxl/openpyxl |
| [rich](https://pypi.org/project/rich/) | 15.0.0 | MIT | 终端富文本输出 | https://github.com/Textualize/rich |
| [psutil](https://pypi.org/project/psutil/) | 7.2.2 | BSD-3-Clause | 进程 / 资源监控 | https://github.com/giampaolo/psutil |

> ℹ️ **本项目当前不含 AGPL 组件。** 早期版本曾使用 `PyMuPDF`（AGPL-3.0 / 商业双许可），
> 已替换为 `pypdfium2`（Apache-2.0 / BSD-3-Clause）以消除 AGPL 第 13 条的网络分发义务，
> 详见 [7.2 节](#72-已移除的-agpl-组件pymupdf--pypdfium2-替换记录)。

## 2. 前端第三方资源（随仓库分发）

以下文件以**二进制 / 压缩源码形式直接存放于仓库**（`web/static/vendor/`、`web/static/fontawesome/`），
因此属于「再分发」，需要保留版权与许可证声明。

| 组件 | 版本 | 许可证 | 位置与用途 | 上游 |
| --- | --- | --- | --- | --- |
| [Bootstrap](https://getbootstrap.com/) | 5.3.3 | MIT | `web/static/vendor/bootstrap.min.css` —— 基础样式与栅格 | https://github.com/twbs/bootstrap |
| [Bootstrap](https://getbootstrap.com/) | 5.3.3 | MIT | `web/static/vendor/bootstrap.bundle.min.js` —— **已 vendored 但当前无引用**（含 Popper.js，同为 MIT） | https://github.com/twbs/bootstrap |
| [Chart.js](https://www.chartjs.org/) | 4.4.1 | MIT | `web/static/vendor/chart.umd.min.js` —— 仪表盘图表（按需懒加载） | https://github.com/chartjs/Chart.js |
| [Font Awesome Free](https://fontawesome.com/) | 7.3.1 | 图标 **CC BY 4.0**；字体 **SIL OFL 1.1**；代码 **MIT** | `web/static/fontawesome/`（子集化 CSS + webfonts）、`web/static/webfonts/` —— 界面图标 | https://github.com/FortAwesome/Font-Awesome |

Font Awesome Free 的图标部分采用 **CC BY 4.0**，该协议**要求署名**。本项目已在 README、
本文档与文档站中给出署名，请勿移除：

> 图标由 [Font Awesome Free 7.3.1](https://fontawesome.com/) 提供，© Fonticons, Inc.，
> 依 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 授权使用。

> 说明：`bootstrap.bundle.min.js` 目前未被任何页面或脚本引用（Bootstrap JS 组件未启用），
> 保留在此以说明其许可归属；如确认不再使用，可安全删除该文件并从本表移除。

## 3. AI 模型与权重（运行时下载，不随仓库分发）

模型权重由 `huggingface_hub` 在首次使用时下载到本地 HF 缓存（`~/.cache/huggingface`），
**不随本仓库分发**。使用与再分发模型权重时，须遵守各模型自身的许可证。

| 模型 | 许可证 | 用途 | 上游 |
| --- | --- | --- | --- |
| [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | MIT | Embedding（默认，中文） | https://huggingface.co/BAAI |
| [BAAI/bge-base-zh-v1.5](https://huggingface.co/BAAI/bge-base-zh-v1.5) | MIT | Embedding（`config.yaml.example` 默认值） | https://huggingface.co/BAAI |
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | MIT | Embedding（可选，1024 维，多语） | https://huggingface.co/BAAI |
| [BAAI/bge-reranker-base](https://huggingface.co/BAAI/bge-reranker-base) | MIT | CrossEncoder 重排 | https://huggingface.co/BAAI |
| [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Apache-2.0 | Embedding（可选，经 OpenAI 兼容服务调用） | https://huggingface.co/Qwen |

此外，Linkora 通过 **OpenAI 兼容 HTTP 接口**调用用户自行配置的 LLM / Embedding 服务
（云端 API 或本地 Ollama、LM Studio、vLLM 等）。这些服务及其模型**由用户自行部署或订阅**，
不属本项目的分发范围，其条款以用户与服务提供方的约定为准。

## 4. 外部工具与系统依赖（需自行安装，不随仓库分发）

Linkora 通过命令行调用下列外部程序。它们**不随仓库分发**，需使用者自行安装并遵守各自许可。

| 工具 | 许可证 | 用途 | 说明 |
| --- | --- | --- | --- |
| `dws`（DingTalk Workspace CLI） | 见钉钉官方发布说明 | 钉钉消息 / 文档 / 审批等读写 | 由用户或镜像自行安装；Linkora 仅以子进程调用 |
| `lark-cli` | 见飞书官方发布说明 | 飞书消息读写 | 同上 |
| `wecom-cli` | 见企业微信官方发布说明 | 企业微信消息读写 | 同上 |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | Apache-2.0 | `pytesseract` 的底层 OCR 引擎 | 需系统安装（`brew install tesseract` / `apt install tesseract-ocr`） |
| [Ollama](https://github.com/ollama/ollama)（可选） | MIT | 本地 LLM 推理服务 | 可选替代方案 |
| [Node.js](https://nodejs.org/)（仅前端构建） | MIT | 运行 esbuild / vitest | 开发与构建期依赖 |
| [Python](https://www.python.org/) | PSF-2.0 | 运行时 | 本项目要求 Python 3.14 |

## 5. 开发与 CI 工具链

这些工具**不进入发布产物**（Docker 镜像 / PyInstaller 二进制），仅用于开发与流水线。

| 组件 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| [ruff](https://github.com/astral-sh/ruff) | 0.16.7 | MIT | Python lint |
| [pyright](https://github.com/microsoft/pyright) | 1.1.414 | MIT | 静态类型检查（只减不增门禁） |
| [pytest](https://github.com/pytest-dev/pytest) | 9.1.1 | MIT | 测试框架 |
| [pytest-cov](https://github.com/pytest-cov/pytest-cov) | 7.1.0 | MIT | 覆盖率统计 |
| [pytest-timeout](https://github.com/pytest-dev/pytest-timeout) | 2.4.0 | MIT | 用例超时保护 |
| [pip-audit](https://github.com/pypa/pip-audit) | 2.10.1 | Apache-2.0 | 依赖漏洞扫描（含 CycloneDX SBOM 依赖链） |
| [uv](https://github.com/astral-sh/uv) | — | MIT / Apache-2.0 | 依赖解析与锁文件生成 |
| [gitleaks](https://github.com/gitleaks/gitleaks) | — | MIT | pre-commit 密钥扫描 |
| [pre-commit](https://github.com/pre-commit/pre-commit) | — | MIT | 本地提交门禁框架 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | — | GPL-2.0-or-later + 例外条款 | 构建 Linux 单文件二进制（`linkora.spec`） |
| [esbuild](https://github.com/evanw/esbuild) | ^0.28.1 | MIT | 前端资源打包（`scripts/build_frontend.mjs`） |
| [jsdom](https://github.com/jsdom/jsdom) | ^25.0.1 | MIT | 前端单测 DOM 环境 |
| [vitest](https://github.com/vitest-dev/vitest) | ^4.1.11 | MIT | 前端单测框架 |

**GitHub Actions（CI 使用的第三方 Action）**

| Action | 许可证 | 用途 |
| --- | --- | --- |
| `actions/checkout`、`setup-python`、`upload-artifact`、`configure-pages`、`deploy-pages`、`jekyll-build-pages`、`upload-pages-artifact`、`dependency-review-action`、`stale` | MIT | 官方通用 Action |
| `astral-sh/setup-uv` | MIT | 安装 uv |
| `peaceiris/actions-gh-pages` | MIT | 文档站发布 |
| `dependabot/fetch-metadata` | MIT | 依赖升级 PR 元数据 |
| `github/codeql-action` | MIT | CodeQL 代码扫描 |

## 6. Python 传递依赖（锁文件完整闭包）

以下为 `requirements.lock` 解析出的**全部传递依赖**（直接依赖已在第 1 节列出）。
CI 与 Docker 均安装该锁文件，因此它们同样会被分发进镜像与二进制。

**宽松许可：MIT / BSD / ISC / Apache-2.0 / MPL-2.0 / PSF / Unlicense 家族**

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cloudpickle | 3.1.2 | BSD-3-Clause |
| colorama | 0.4.6 | BSD-3-Clause |
| coverage | 7.16.1 | Apache-2.0 |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| cuda-pathfinder | 1.8.1 | Apache-2.0 |
| distro | 1.9.0 | Apache-2.0 |
| et-xmlfile | 2.0.0 | MIT |
| filelock | 3.32.0 | MIT |
| flatbuffers | 25.12.19 | Apache-2.0 |
| fsspec | 2026.6.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.6.0 | Apache-2.0 |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| jiter | 0.17.0 | MIT |
| joblib | 1.6.0 | BSD-3-Clause |
| lxml | 6.1.3 | BSD-3-Clause |
| markdown-it-py | 4.2.0 | MIT |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| mdurl | 0.1.2 | MIT |
| mpmath | 1.3.0 | BSD-3-Clause |
| narwhals | 2.26.0 | MIT |
| networkx | 3.6.1 | BSD-3-Clause |
| onnxruntime | 1.27.0 | MIT |
| opencv-python | 5.0.0.93 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pdfminer.six | 20260107 | MIT |
| pluggy | 1.6.0 | MIT |
| protobuf | 7.36.2 | BSD-3-Clause |
| pyclipper | 1.4.0 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic-core | 2.46.5 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| pypdfium2 | 5.13.0 | Apache-2.0 OR BSD-3-Clause（已提升为直接依赖，见第 1 节） |
| safetensors | 0.8.0 | Apache-2.0 |
| scikit-learn | 1.9.1 | BSD-3-Clause |
| scipy | 1.18.1 | BSD-3-Clause |
| setuptools | 84.0.0 | MIT |
| shapely | 2.1.2 | BSD-3-Clause |
| shellingham | 1.5.4 | ISC |
| six | 1.17.0 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| soupsieve | 2.9.2 | MIT |
| starlette | 1.6.0 | BSD-3-Clause |
| sympy | 1.14.0 | BSD-3-Clause |
| threadpoolctl | 3.7.0 | BSD-3-Clause |
| torch | 2.14.0 | BSD-3-Clause 为主（另含 Apache-2.0 / MIT / BSL-1.0 等子组件） |
| tqdm | 4.69.0 | MPL-2.0 AND MIT |
| transformers | 5.17.0 | Apache-2.0 |
| triton | 3.8.0 | MIT（仅 Linux） |
| typer | 0.27.2 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| xlsxwriter | 3.2.9 | BSD-2-Clause |

**需单独注意的许可**

| 组件 | 版本 | 许可证 | 说明 |
| --- | --- | --- | --- |
| pypdfium2（含捆绑的 PDFium 二进制与各静态库） | 5.13.0 | Apache-2.0 OR BSD-3-Clause；捆绑静态库（freetype / libpng / libjpeg-turbo / libopenjpeg / libtiff / lcms / ICU / simdutf 等）为各自主许可证 | 再分发时须随附 wheel 内 `licenses/` 目录的全部许可文本。该目录由上游随包分发（`*.dist-info/licenses/`），无需人工维护；**PDFium 在 macOS/Linux 会随包携带约 3–4 MB 原生库** |
| certifi | 2026.7.22 | MPL-2.0 | 文件级 copyleft：修改其文件须公开该文件源码；仅使用无需开源本项目 |
| tqdm | 4.69.0 | MPL-2.0 AND MIT | 同上，文件级 copyleft |
| cuda-bindings / cuda-toolkit / nvidia-*（cublas、cudnn、nccl、cusolver 等，共 20 个包） | 见锁文件 | NVIDIA 官方发行包：Apache-2.0 或 NVIDIA 专有 EULA（视具体包） | **仅 Linux 平台安装**（torch 的 CUDA 运行时）；macOS 上不会拉取 |

## 7. 许可证合规要点

### 7.1 主许可证与第三方许可证的兼容性

Linkora 以 **GPL-3.0-or-later** 发布，与下表所有第三方许可证均**兼容**：

| 第三方许可证 | 是否与 GPL-3.0 兼容 | 分发时的义务 |
| --- | --- | --- |
| MIT / BSD-2 / BSD-3 / ISC / MIT-0 | ✅ 兼容 | 保留版权声明与许可证全文 |
| Apache-2.0 | ✅ 兼容 | 保留 NOTICE 与许可证；若修改其文件需标注 |
| MPL-2.0（certifi、tqdm） | ✅ 兼容（文件级 copyleft） | 修改这些组件自身文件时须公开该文件源码 |
| PSF-2.0 / Unlicense | ✅ 兼容 | 保留版权声明 |

### 7.2 已移除的 AGPL 组件：PyMuPDF → pypdfium2 替换记录

**结论：本项目当前不含任何 AGPL / 强网络 copyleft 组件，无需履行 AGPL 第 13 条的源码提供义务。**

早期版本（≤ 1.28.2 依赖期）的 PDF 页面渲染使用 `PyMuPDF`，其为 **AGPL-3.0 或 Artifex 商业许可**
双许可模式。对本项目的影响曾是：

1. **分发二进制 / 镜像时**：AGPL 要求向接收者提供完整对应源码。Linkora 本身即以 GPL-3.0 开源，
   此项可自然满足。
2. **作为网络服务提供时（SaaS / 内部部署 ≠ 分发）**：AGPL §13 的关键条款是
   **「通过网络交互使用的用户，有权获得源码」**。GPL 不因「仅提供服务、不分发」而触发，**AGPL 会**。
   即：以 Linkora 对外提供服务的部署方必须向使用者提供完整源码。
3. 若无法接受上述条款：① 向 [Artifex](https://artifex.com/licensing/) 购买商业许可；② 替换实现。

**本项目选择了方案 ②，已完成替换：**

| 项目 | 替换前 | 替换后 |
| --- | --- | --- |
| 组件 | PyMuPDF 1.28.2 | pypdfium2 5.13.0 |
| 许可证 | AGPL-3.0 / 商业双许可 ⚠️ | Apache-2.0 OR BSD-3-Clause ✅ |
| 用途 | PDF 页面渲染为位图（扫描件 OCR） | 同左 |
| 渲染等价性 | `get_pixmap(dpi=300)` | `page.render(scale=300/72)` —— 同为 72 DPI 基准，输出尺寸一致 |
| 新增依赖 | — | **无**（pypdfium2 本就是 `pdfplumber` 的传递依赖，本次仅提升为显式直接依赖） |
| 代码位置 | `src/tools/parse_document.py::_parse_pdf_ocr` | 同左（`_render_pdf_page_png`） |

> 替换后**净减少一个直接依赖**（29 → 28 个直接依赖，新增的 pypdfium2 原本已作为传递依赖存在），
> 且不再有任何 AGPL 传染风险。若后续需要在「文本型 PDF」上扩展能力，优先使用已有的 `pdfplumber`（MIT）。

### 7.3 CC BY 4.0 署名（Font Awesome）

Font Awesome Free 的**图标**采用 CC BY 4.0，署名是**硬性要求**（非可选项）。
本项目已在 [README](README.md)、本文档与[文档站](docs/third-party.md)三处给出署名。
任何衍生作品（含二次分发的 UI）**必须保留该署名**，或改为使用商业授权的 Font Awesome Pro。

### 7.4 商标与平台条款

- 「钉钉」「飞书」「企业微信」为对应公司的商标，本项目仅描述兼容性，**不主张任何关联或背书**。
- 使用各平台开放能力时，请遵守其开发者协议与权限规范（详见 [README](README.md) 与
  [docs/security.md](docs/security.md)）。

## 8. 许可证全文索引

分发本软件时，请随附下列许可证全文：

| 许可证 | 全文地址 |
| --- | --- |
| GPL-3.0 | https://www.gnu.org/licenses/gpl-3.0.txt （亦见本仓库 [LICENSE](LICENSE)） |
| Apache-2.0 | https://www.apache.org/licenses/LICENSE-2.0 |
| MIT | https://opensource.org/license/mit |
| BSD-2-Clause | https://opensource.org/license/bsd-2-clause |
| BSD-3-Clause | https://opensource.org/license/bsd-3-clause |
| ISC | https://opensource.org/license/isc-license-txt |
| MPL-2.0 | https://www.mozilla.org/en-US/MPL/2.0/ |
| PSF-2.0 | https://opensource.org/license/psf-2-0 |
| Unlicense | https://unlicense.org/ |
| CC BY 4.0 | https://creativecommons.org/licenses/by/4.0/ |
| SIL OFL 1.1 | https://openfontlicense.org/ |

---

## 维护说明

- 依赖增删后，请更新第 1 节（直接依赖）与第 6 节（锁文件闭包）。
- 重新生成锁文件：`bash scripts/lock_deps.sh`（改 `requirements.txt` 后必做）。
- 依赖一致性校验：`.venv/bin/python scripts/check_deps.py`（CI 强制）。
- 漏洞扫描：`.venv/bin/python -m pip_audit -r requirements.lock`。
- 本文件的许可证信息取自各发行包的 `METADATA`（PEP 639 `License-Expression` / Classifier）
  与随包 `LICENSE` 文件，个别条目以项目主页声明为准。

_最后更新：2026-09-18_
