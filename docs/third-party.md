# 第三方组件声明

灵桥 Linkora 本体以 **GNU GPL-3.0-or-later** 发布（[LICENSE](https://github.com/0x0021/Linkora/blob/main/LICENSE)）。
本页列出 Linkora 在运行、构建、测试过程中引用或分发的第三方开源组件及其许可证。

> 本页是仓库根目录
> [THIRD_PARTY_NOTICES.md](https://github.com/0x0021/Linkora/blob/main/THIRD_PARTY_NOTICES.md)
> 的文档站镜像。**以该文件为准**，两者不一致时以仓库文件为唯一真源。

## 一览

| 领域 | 组件 | 许可证 |
| --- | --- | --- |
| Web 服务 | FastAPI、Uvicorn、Starlette、Jinja2、PyJWT、python-multipart | MIT / BSD-3-Clause / Apache-2.0 |
| 配置与校验 | pydantic、PyYAML、python-dotenv、platformdirs | MIT / BSD-3-Clause |
| RAG 与检索 | sentence-transformers、faiss-cpu、numpy、jieba、transformers、tokenizers、huggingface_hub | Apache-2.0 / MIT / BSD-3-Clause |
| LLM 接入 | openai（OpenAI 兼容协议客户端） | Apache-2.0 |
| 文档解析 | pdfplumber、pdfminer.six、python-docx、python-pptx、openpyxl、beautifulsoup4、lxml | MIT / BSD-3-Clause |
| OCR | rapidocr-onnxruntime、onnxruntime、opencv-python、pytesseract、Pillow、pyclipper、shapely | Apache-2.0 / MIT / MIT-CMU / BSD-3-Clause |
| PDF 渲染 | PyMuPDF | ⚠️ **AGPL-3.0 或 商业许可**（双许可） |
| 前端 | Bootstrap 5.3.3、Chart.js 4.4.1、Font Awesome Free 7.3.1 | MIT / CC BY 4.0 + SIL OFL 1.1 |
| 开发与 CI | ruff、pyright、pytest、pip-audit、uv、gitleaks、esbuild、vitest、jsdom | MIT / Apache-2.0 |
| AI 模型权重 | BAAI bge 系列（MIT）、Qwen3-Embedding（Apache-2.0） | 运行时下载，不随仓库分发 |

## 前端第三方资源（随仓库分发）

以下资源以二进制 / 压缩源码形式直接存放在仓库中，属于「再分发」，须保留版权与许可证声明。

| 组件 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| [Bootstrap](https://getbootstrap.com/) | 5.3.3 | MIT | 基础样式与栅格（CSS 在用；JS bundle 已 vendored 但当前无引用） |
| [Chart.js](https://www.chartjs.org/) | 4.4.1 | MIT | 仪表盘图表（按需懒加载） |
| [Font Awesome Free](https://fontawesome.com/) | 7.3.1 | 图标 CC BY 4.0；字体 SIL OFL 1.1；代码 MIT | 界面图标（子集化） |

**必需署名**

> 图标由 [Font Awesome Free 7.3.1](https://fontawesome.com/) 提供，© Fonticons, Inc.，
> 依 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 授权使用。
> 字体依 [SIL OFL 1.1](https://openfontlicense.org/) 授权，代码依 MIT 授权。

## AI 模型与权重

模型权重由 `huggingface_hub` 在首次使用时下载到本地缓存，**不随本仓库分发**；
使用与再分发权重须遵守各模型自身许可证。

| 模型 | 许可证 | 用途 |
| --- | --- | --- |
| [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | MIT | Embedding（默认） |
| [BAAI/bge-base-zh-v1.5](https://huggingface.co/BAAI/bge-base-zh-v1.5) | MIT | Embedding（可选） |
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | MIT | Embedding（可选，多语，1024 维） |
| [BAAI/bge-reranker-base](https://huggingface.co/BAAI/bge-reranker-base) | MIT | CrossEncoder 重排 |
| [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Apache-2.0 | Embedding（可选，经 OpenAI 兼容服务调用） |

LLM / Embedding 服务（云端 API 或本地 Ollama、LM Studio、vLLM 等）由使用者自行配置与部署，
不属本项目分发范围。

## 外部工具与系统依赖

| 工具 | 许可证 | 用途 |
| --- | --- | --- |
| `dws`（钉钉 Workspace CLI） | 见钉钉官方发布说明 | 钉钉消息 / 文档 / 审批读写 |
| `lark-cli`（飞书） | 见飞书官方发布说明 | 飞书消息读写 |
| `wecom-cli`（企业微信） | 见企业微信官方发布说明 | 企业微信消息读写 |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) | Apache-2.0 | `pytesseract` 底层 OCR 引擎 |
| [Ollama](https://github.com/ollama/ollama)（可选） | MIT | 本地 LLM 推理服务 |
| [Node.js](https://nodejs.org/)（仅前端构建） | MIT | 运行 esbuild / vitest |
| [Python](https://www.python.org/) 3.14 | PSF-2.0 | 运行时 |

以上均**不随本仓库分发**，需使用者自行安装。

## Python 传递依赖

完整闭包见
[requirements.lock](https://github.com/0x0021/Linkora/blob/main/requirements.lock)，
逐包许可证明细见
[THIRD_PARTY_NOTICES.md 第 6 节](https://github.com/0x0021/Linkora/blob/main/THIRD_PARTY_NOTICES.md#6-python-传递依赖锁文件完整闭包)。
其中绝大多数为 MIT / BSD / Apache-2.0 / ISC 等宽松许可；需单独注意的条目：

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| pymupdf | AGPL-3.0 / 商业双许可 | 见下方合规要点 |
| certifi、tqdm | MPL-2.0 | 文件级 copyleft：修改其自身文件时须公开该文件源码 |
| cuda-bindings、cuda-toolkit、nvidia-*（20 个包） | NVIDIA 官方发行包（Apache-2.0 或 NVIDIA 专有 EULA） | 仅 Linux 安装（torch 的 CUDA 运行时） |

## 许可证合规要点

### 主许可证兼容性

Linkora 以 **GPL-3.0-or-later** 发布，与以下第三方许可证均兼容：
MIT、BSD-2-Clause、BSD-3-Clause、ISC、MIT-0、Apache-2.0、MPL-2.0（文件级 copyleft）、
PSF-2.0、Unlicense、CCO、以及 **AGPL-3.0**（AGPL 第 13 条允许与 GPLv3 组合作品）。

### ⚠️ PyMuPDF 的 AGPL 影响

`pymupdf` 采用 **AGPL-3.0 或 Artifex 商业许可**双许可。影响如下：

1. **分发二进制 / 镜像时**：AGPL 要求向接收者提供完整对应源码。Linkora 本身即以 GPL-3.0
   开源，此项自然满足——请确保分发时一并附上仓库源码与 LICENSE。
2. **作为网络服务提供时**：AGPL 第 13 条要求**通过网络交互使用的用户有权获得源码**。
   GPL 不因「只提供服务、不分发」而触发，**AGPL 会**。因此以 Linkora 对外提供服务的部署方，
   须向使用者提供（或明示获取方式）完整源码。
3. **若无法接受 AGPL**：可向 [Artifex](https://artifex.com/licensing/) 购买 PyMuPDF 商业许可，
   或替换 PDF 解析实现（`pypdfium2` / `pdfplumber`，均为宽松许可）后移除该依赖。

### CC BY 4.0 署名（Font Awesome）

Font Awesome Free 的**图标**采用 CC BY 4.0，署名是硬性要求。任何衍生作品（含二次分发的 UI）
**必须保留署名**，或改用商业授权的 Font Awesome Pro。

### 商标与平台条款

- 「钉钉」「飞书」「企业微信」为对应公司的商标，本项目仅描述兼容性，不主张任何关联或背书。
- 使用各平台开放能力时，请遵守其开发者协议与权限规范，详见 [安全说明](security.html)。

## 许可证全文索引

| 许可证 | 全文地址 |
| --- | --- |
| GPL-3.0 | https://www.gnu.org/licenses/gpl-3.0.txt |
| AGPL-3.0 | https://www.gnu.org/licenses/agpl-3.0.txt |
| Apache-2.0 | https://www.apache.org/licenses/LICENSE-2.0 |
| MIT | https://opensource.org/license/mit |
| BSD-2-Clause | https://opensource.org/license/bsd-2-clause |
| BSD-3-Clause | https://opensource.org/license/bsd-3-clause |
| ISC | https://opensource.org/license/isc-license-txt |
| MPL-2.0 | https://www.mozilla.org/en-US/MPL/2.0/ |
| PSF-2.0 | https://opensource.org/license/psf-2-0 |
| CC BY 4.0 | https://creativecommons.org/licenses/by/4.0/ |
| SIL OFL 1.1 | https://openfontlicense.org/ |

_最后更新：2026-09-18_
