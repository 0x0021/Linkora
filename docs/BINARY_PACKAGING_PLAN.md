# Linkora 纯二进制跨平台发布方案（可行性研究 + 实施计划）

> 目标：把 Linkora 打成 **macOS / Windows / Linux** 三个平台的纯可执行文件（单文件 `.exe` / ELF / Mach-O），用户下载后无需安装 Python、无需 `pip install`、无需 Node，开箱即用。
> 状态：可行性 **成立**，但有两个硬性成本驱动项（torch 体积 / Playwright 浏览器）和一处必做代码改造（路径可重定位）。本文给出具体方案与分阶段实施路线。
> 关联：A1 进程分离（`--mode {both,web,worker}`）已落地，单个二进制即可覆盖三种运行模式，打包只需产出一个二进制。

---

## 0. 可行性结论（先看这个）

| 维度 | 结论 | 说明 |
|---|---|---|
| 能否打纯二进制 | ✅ 可行 | 用 PyInstaller `--onefile` 即可，前端是纯静态、无 Node 运行时依赖。 |
| 体积 | ⚠️ 偏大 | 默认配置走**本地 embedding**（`bge-large-zh-v1.5` + `offline: true`），运行时必须带 `torch`/`transformers`/`sentence-transformers`/`faiss`。仅 Python 依赖盘占 **~1.4 GB**（torch 504 MB）。产物压缩后约 **1.2–1.6 GB**。 |
| 能否变小 | ✅ 可选 | 若部署侧用 **API embedding**（`provider: api`），可剔除 torch 全家桶，二进制降到 **~80–150 MB**（见「Profile B」）。 |
| 跨平台编译 | ⚠️ 不能交叉编译 | PyInstaller/Nuitka 必须在**目标 OS** 上构建。需 CI 矩阵（GitHub Actions 的 ubuntu / windows / macos runner），无法在一台机器上打出三平台。 |
| 必做改造 | 🔴 路径可重定位 | 当前 `config.yaml`、`./data/*`、PID 文件全部相对 cwd。装到 `/opt` 或 `C:\Program Files` 会因只读/路径错乱而崩。必须先改（见 §4）。 |
| 外部运行时依赖 | ⚠️ Playwright 浏览器 + Tesseract OCR | `web/routers/kb.py` 懒加载 `playwright`（需 `playwright install chromium`，~150 MB，不进二进制）；`pytesseract` 需目标机装 **系统级 Tesseract OCR 二进制**（非 pip 可装，Win/Mac/Linux 各自独立安装）。 |
| 模型权重 | ⚠️ 单独分发 | `./data/models/bge-large-zh-v1.5`（~1.3 GB）在 site-packages 之外，是用户数据资产，不进二进制，需单独放置或随包附带。 |

**一句话**：能打、能跨三平台，但默认配置下是个「大二进制」（torch 撑起来的）；想小就得切 API embedding 或接受模型外挂。路径改造是绕不过去的前置工作。

---

## 1. 技术选型

### 1.1 打包工具对比

| 工具 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **PyInstaller `--onefile`** | 单文件产出、数据文件 `--add-data` 简单、numpy/torch 有现成 hook、文档多 | 启动慢（解压到临时目录 ~1–3s）、体积大 | **主选（推荐起步）** |
| **Nuitka** | 编译成 C、启动快、体积更小、抗逆向 | 构建极慢、torch/transformers 兼容性坑多、排错难 | 备选（等 PyInstaller 跑通后再评估收益） |
| cx_Freeze | 多文件、跨平台 | 单文件需自解压脚本，体验差 | 不推荐 |
| shiv / pex / zipapp | 打包但**仍需目标机有 Python** | 不满足「纯二进制」 | ❌ 排除 |
| Briefcase (BeeWare) | 面向 GUI 应用打包成原生安装包 | Linkora 是后端服务，不匹配 | ❌ 排除 |

**结论**：以 **PyInstaller `--onefile`** 为主方案；若后续对体积/启动敏感且 torch 仍要带，再评估 Nuitka。

### 1.2 为什么前端不是问题
`web/static/` 是**纯静态资源**（css/js/vendor/fontawesome），由 FastAPI `StaticFiles` 直接挂载（`web/api.py:144`）。`node_modules` 只装了 `@fortawesome/fontawesome-free`，字体已拷贝进 `web/static/fontawesome`，**运行时不需要 Node**。PyInstaller 把 `web/static` 和 `web/templates` 当数据文件打进二进制即可。

---

## 2. 体积剖析（决定走哪条 Profile）

实测当前 `.venv`：

```
.venv/lib/python3.14/site-packages        1.4 GB
└─ torch/                                 504 MB   ← 头号大户
   transformers / sentence-transformers   数百 MB  ← 本地 embedding 必需
   faiss-cpu / onnxruntime / rapidocr     ~百 MB
   pymupdf / pdfplumber / scipy / numpy / pillow / openpyxl / playwright
```

`embedding.provider` 默认 = **local**（`config.yaml:17-23`，`offline: true`，模型 `./data/models/bge-large-zh-v1.5`）。因此默认分发**必须带 torch 全家桶**。

### Profile A —— 全量（本地 embedding，离线自托管）
- 包含：torch + transformers + sentence-transformers + faiss + 全部依赖。
- 二进制体积：压缩后 **~1.2–1.6 GB**。
- 适用：当前默认配置、内网/离线、不想依赖外部 embedding API 的用户。
- 模型权重 `bge-large-zh-v1.5` 单独分发（放 `LINKORA_HOME/data/models/`）。

### Profile B —— 精简（API embedding）
- 条件：部署侧 `embedding.provider: api`（用远程 embedding 服务）。
- 剔除：`torch`、`transformers`、`sentence-transformers`、`faiss-cpu`（向量索引若仍走 faiss 需保留 faiss，可换纯 numpy 实现或保留 faiss-cpu ~30MB）。
- 二进制体积：**~80–150 MB**。
- 实施：独立的 `requirements-slim.txt` + PyInstaller `excludes=[torch, transformers, sentence_transformers]`。

> 建议：**先交付 Profile A**（与现有默认配置零改动即可用），把 Profile B 作为后续优化项。两条 Profile 共享同一套 §4 路径改造与 §5 打包骨架。

---

## 3. 跨平台构建策略（不能交叉编译）

必须在目标 OS **原生环境**构建（容器也只能提供对应 OS 的环境，无法跨 OS 编译出真二进制）。两种落地方式等价：**本地容器**（你开容器，我在里面编译）或 **GitHub Actions 矩阵**（CI 自动）。先用 §3.1 说清容器能/不能做什么，再给矩阵方案。

| 目标 | Runner | 架构 | 产物 |
|---|---|---|---|
| Linux x86_64 | `ubuntu-22.04` | x64 | `linkora-linux-x64` (ELF) |
| macOS arm64 (Apple Silicon) | `macos-14` | arm64 | `linkora-macos-arm64` (Mach-O) |
| macOS x86_64 (Intel) | `macos-13` | x64 | `linkora-macos-x64` |
| Windows x86_64 | `windows-2022` | x64 | `linkora-win-x64.exe` |
| (可选) Windows arm64 | `windows-11-arm` | arm64 | `linkora-win-arm64.exe` |

### 3.1 容器辅助构建：能做什么、不能做什么（关键）

你本机是 macOS，Docker Desktop 能跑 **Linux 容器**。结论：

| 平台 | 能否用「你本机开的 Linux 容器」编译出真二进制？ | 正确做法 |
|---|---|---|
| **Linux** | ✅ 能，且最干净 | 在 `python:3.13-slim` 容器内 `pip install` + `pyinstaller`，挂载卷取回 `dist/linkora`。可复现、依赖钉版、不污染本机。 |
| **macOS** | ❌ 不能 | macOS 二进制需 macOS 本体 + Apple 工具链（clang/codesign），Linux 容器跑不出 Mach-O，且 Apple 许可禁止在非 Apple 硬件跑 macOS。直接在你 Mac 上原生构建（或 mac CI runner）。容器对此无帮助。 |
| **Windows** | ❌ 不能 | Windows `.exe` 需 Windows 环境。Linux 容器无法产出真 exe；Wine+PyInstaller 对 torch 这种大 native 依赖极不稳定，不推荐。需 Windows VM（Parallels/UTM/云）或 GH Actions `windows-2022` runner。 |

> 所以「容器编译」这个思路 **对 Linux 完全成立、且是最优解**；但它不能把 mac/win 也顺手解决。三平台真实组合：
> - **Linux**：你开容器（或 CI）→ 一条命令出二进制。 ✅
> - **macOS**：你 Mac 原生构建（A1 的 `--mode` 已就绪）。 ✅
> - **Windows**：另起 Windows 环境（VM / CI）。 ⚠️

### 3.2 容器构建交付物（Linux）

已在仓库内置可复用的容器构建配置，直接解决 Linux 二进制：

- `Dockerfile.build` —— 基于 `python:3.13-slim`，装 `libgomp1`（torch OpenMP）+ `tesseract-ocr`（pytesseract），钉版装依赖后跑 `pyinstaller linkora.spec`。
- `scripts/docker-build-linux.sh` —— 一条命令：`docker build` → 起一次性容器 → `docker cp` 取回 `dist/linkora`。
- `.dockerignore` —— 排除 `.venv`/`node_modules`/`data`/`dist` 等，避免把本地污染带进镜像。

```bash
# 在你 Mac 上（Docker Desktop 运行中）：
bash scripts/docker-build-linux.sh
# 产物：./dist/linkora  （可在任意 glibc x64 Linux 运行）
```

> 前提：`linkora.spec` 在 P1 阶段生成；`requirements.txt` 需先钉版（见 §11）。容器只解决 Linux，mac/win 仍按 §3.1 走原生/CI。

- 无论本地容器还是 CI runner，都用 **Python 3.13** 干净环境建 venv，`pip install -r requirements.txt`（Profile A）或 `requirements-slim.txt`（Profile B）。
- 产物用 `actions/upload-artifact` + 一个 release 工作流（`softprops/action-gh-release`）按 tag 发布。
- **签名/公证**：
  - **macOS**：必须 `codesign --force --deep --sign -`（ad-hoc）至少让 Gatekeeper 不硬拦；有 Developer ID 时做真签名 + `notarytool` 公证避免「无法验证开发者」。
  - **Windows**：无强制签名，但无签名会被 SmartScreen 报「未知发布者」，建议有 EV 代码签名证书时加 `signtool`。
  - **Linux**：无需签名；建议 `upx` 进一步压缩（CPU 架构要对，arm64 需用对应 upx）。

---

## 4. 必做代码改造：路径可重定位（🔴 前置，不改造二进制必崩）

当前所有路径基于 cwd（`src/config.py:18-21`、`src/audit.py:28`、`config.yaml` 加载自 cwd、`web/api.py:115 _BASE_DIR`）。装到只读/非 cwd 位置会失败。

### 4.1 新增 `src/platform/paths.py`（用户数据目录解析）

```python
import os
import sys
from pathlib import Path

try:
    from platformdirs import PlatformDirs
except ImportError:  # 兜底（platformdirs 仅 5KB 纯 Python，应加入依赖）
    PlatformDirs = None

def app_data_dir() -> Path:
    """确定可写的用户数据根目录，优先级：
    LINKORA_HOME 环境变量 > --data-dir 参数 > 安装态默认 > 开发态(cwd)。
    """
    env = os.environ.get("LINKORA_HOME")
    if env:
        return Path(env).expanduser().resolve()
    # 安装态（PyInstaller 冻结）：二进制旁 data/ 或系统标准目录
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if PlatformDirs is not None:
            return Path(PlatformDirs("Linkora", "LinkoraTeam").user_data_dir)
        return exe_dir / "data"
    # 开发态：保持现状（仓库根 ./data），向后兼容
    return Path(os.getcwd()) / "data"

def resource_dir() -> Path:
    """冻结态下静态/templates 在 _MEIPASS；否则用源码位置。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(Path(sys.executable).parent)))
    return Path(__file__).resolve().parent.parent.parent  # 仓库根
```

要点：
- `pip install platformdirs`（纯 Python，5KB，不增加体积负担）。
- `main.py` 解析新增 `--data-dir` 与 `--config <path>`；并把 `LINKORA_HOME`/data-dir 注入 `app_data_dir()`。
- `src/config.py` 的 `DEFAULT_DATA_DIR` / `DEFAULT_STORAGE_PATH` 等改为**基于 `app_data_dir()`** 解析，仅当用户未显式配置时回退。
- PID 文件（`data/linkora*.pid`）改为写入 `app_data_dir()`（A1 已按模式分文件，只需把锚点从 cwd 根换到 data 根）。
- `config.yaml` 默认搜索顺序：`--config` > `LINKORA_HOME/config.yaml` > `<data-dir>/config.yaml` > cwd（开发兼容）。首次运行若无 config，从内置模板 `config.example.yaml` 复制一份到 data 目录。

### 4.2 `web/api.py` 冻结态路径 shim

```python
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE_DIR = Path(getattr(sys, "_MEIPASS", str(Path(sys.executable).parent)))
else:
    _BASE_DIR = Path(__file__).resolve().parent
```

`StaticFiles(directory=_BASE_DIR/"static")` 与 `templates` 目录随之可定位（PyInstaller 把它们 `--add-data` 到 `_MEIPASS`）。

### 4.3 外部系统依赖（不进二进制，需目标机具备）

| 依赖 | 用途 | 目标机安装方式 |
|---|---|---|
| **Playwright Chromium** | `kb.py` 网页转 Markdown | `linkora --install-deps`（封装 `playwright install chromium`），~150 MB |
| **Tesseract OCR** | `pytesseract` 图片 OCR | macOS `brew install tesseract`；Ubuntu `apt install tesseract-ocr`；Windows 装 UB-Mannheim 安装包并加 PATH |
| **系统库（Linux）** | torch/opencv 等可能依赖 `libgomp`/`libgl` | Ubuntu `apt install libgomp1 libgl1` 等（CI 用 `ubuntu-22.04` 基础镜像一般已带） |

- Playwright 推荐**文档要求自装**，避免二进制再胀 150MB 且跨版本不兼容。
- Tesseract 无法 pip 安装，`--install-deps` 只能提示/引导，真正的二进制需目标系统包管理器；Windows 可随包附带 `tesseract` 并把路径写入 `TESSDATA_PREFIX`/`PATH`。
- 建议提供 `linkora --check-deps` 自检命令，启动前一次性报告 Playwright/Tesseract/模型权重是否就绪，降低「纯二进制」用户的踩坑率。

---

## 5. PyInstaller 打包骨架

### 5.1 `.spec` 要点（Profile A）

```python
# pyinstaller linkora.spec  （由 pyinstaller --onefile 生成后改）
a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("web/static", "web/static"),
        ("web/templates", "web/templates"),
        ("config.example.yaml", "."),
    ],
    hiddenimports=[
        "faiss", "sentence_transformers", "transformers", "torch",
        "onnxruntime", "rapidocr_onnxruntime", "pymupdf", "pdfplumber",
        "playwright", "playwright.sync_api", "numpy", "scipy",
        "openpyxl", "PIL",
    ],
    # 收集 torch/transformers 的子模块与数据文件，避免运行时 ImportError
    collect_submodules=["torch", "transformers", "sentence_transformers"],
    excludes=["tkinter", "unittest", "pydoc", "doctest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas,
         name="linkora",
         console=True,          # 服务端要控制台；Windows 可加 --hide-console
         upx=True,
         icon="docs/icon.ico")  # 可选
# onefile：不写 COLLECT，直接 EXE 包含全部
```

- `hiddenimports` 必须覆盖**懒加载**的依赖（faiss/playwright/sentence_transformers 都是函数内 `import`，PyInstaller 静态分析抓不到）。
- `collect_submodules=["torch","transformers"]` 防止 torch 的众多子包被裁掉导致运行时崩。
- Profile B 把 `excludes=[torch, transformers, sentence_transformers]` 并去掉对应 `collect_submodules`。

### 5.2 构建命令
```bash
# 干净 venv
python -m venv build-venv && build-venv/bin/pip install -r requirements.txt pyinstaller
build-venv/bin/pyinstaller linkora.spec --clean --noconfirm
# 产物：dist/linkora（mac/linux）或 dist/linkora.exe（win）
```

---

## 6. 启动器（替代 run_linkora.py）

A1 的 `scripts/run_linkora.py` 是 Python，纯二进制用户手里没有 Python。两种替代：

**方案 1（推荐，最省事）**：文档指导用户用两条命令 / 两个 systemd/launchd/Windows 服务分别拉起：
```bash
linkora --mode web --web 8080      # 服务 unit: linkora-web
linkora --mode worker              # 服务 unit: linkora-worker
```
（DEPLOY.md 已有进程分离章节，补一段「二进制模式」即可。）

**方案 2（体验更好）**：提供原生启动器脚本（不进二进制，随包分发）：
- macOS/Linux：`linkora-run.sh`（`nohup linkora --mode web ... &` + `linkora --mode worker &`，Ctrl+C 转发）。
- Windows：`linkora-run.ps1`（Start-Process 两个 `--mode`，监听退出转发）。
可选：把启动器逻辑编译进二进制（`linkora --daemon` 自己 fork 两子进程），但要处理好 PID/日志，工作量更大，建议先用方案 1。

---

## 7. CI/CD 流水线草图（GitHub Actions）

{% raw %}
```yaml
name: build-binaries
on:
  push:
    tags: ["v*"]
jobs:
  build:
    strategy:
      matrix:
        include:
          - os: ubuntu-22.04      arch: x64    name: linux-x64
          - os: macos-14          arch: arm64  name: macos-arm64
          - os: macos-13          arch: x64    name: macos-x64
          - os: windows-2022      arch: x64    name: win-x64
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: python -m venv v && v/bin/pip install -r requirements.txt pyinstaller
        shell: bash
      - run: v/bin/pyinstaller linkora.spec --clean --noconfirm
      - name: macOS codesign (ad-hoc)
        if: startsWith(matrix.os, 'macos')
        run: codesign --force --deep --sign - dist/linkora
      - uses: softprops/action-gh-release@v2
        with:
          files: dist/linkora*
```
{% endraw %}

---

## 8. 验证清单（产物质量门）

二进制产出后必须过：
1. `./linkora --version` / `--help` 正常（无 GUI，控制台）。
2. `./linkora --mode worker --test-rule "hello"` 启动即退、写 `linkora.worker.pid` 到 data 目录。
3. `./linkora --mode web --web 8080` 起服务，`curl localhost:8080/health` 返回 200。
4. 在**干净虚拟机**（无 Python/无 venv）跑上述 2/3，确认零依赖。
5. 验证数据写在 `LINKORA_HOME`（非二进制旁只读区）。
6. Playwright 路径：`linkora --install-deps` 或文档步骤后，kb 网页抓取可用。
7. 三平台各跑一遍（mac 用 arm64 机，win 用干净 Win10/11，linux 用 ubuntu 22.04）。
8. `linkora --check-deps` 在干净机上报 Playwright/Tesseract/模型权重缺失时给出明确指引（而非运行时才崩）。

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 杀软误报（PyInstaller 二进制常被标记） | 用户不敢运行 | 代码签名；提供哈希与发布说明；可选 Nuitka 降低误报率 |
| macOS Gatekeeper 拦截 | 双击打不开 | ad-hoc / Developer ID 签名 + 公证；文档给「右键打开」兜底 |
| 二进制过大（~1.5GB）下载慢 | 体验差 | Profile B（API embedding）瘦身；模型权重单独分发；CDN/分卷 |
| 冷启动慢（onefile 解压 1–3s） | 体感卡 | 换 `--onedir` 或多文件；或 Nuitka |
| torch 在二进制内 OpenMP/线程数异常 | 性能/崩溃 | 冻结后显式设 `OMP_NUM_THREADS`；测试期观测 CPU 占用 |
| 模型权重体积（~1.3GB）未随包 | 首跑需用户放模型 | 文档明确；或提供「首次运行自动下载」开关（需联网） |
| 跨大版本 Python 行为差异 | 诡异 bug | CI 固定 3.13，与本地一致 |

---

## 10. 分阶段实施路线（建议）

- **P0 — 路径可重定位（必做前置，约 1–1.5 天）**
  `src/platform/paths.py` + `app_data_dir()` 接入 config/PID/audit + `web/api.py` shim + `--data-dir`/`--config`/`LINKORA_HOME` + `config.example.yaml` 模板复制。跑通：装到 `/tmp/linkora-install/` 仍可正常读写。
- **P1 — PyInstaller 骨架 + Profile A 打 mac 本地包（约 1 天）**
  写 `linkora.spec`、`requirements.txt` 钉版、本地出 mac 二进制，过 §8 验证 1–4。
- **P2 — CI 矩阵三平台（约 1–1.5 天）**
  GitHub Actions 出 linux/win/mac 三产物 + release；mac 签名。
- **P3 — 体验收尾（约 1 天）**
  原生启动器脚本（§6）、Playwright 浏览器分发策略、DEPLOY.md 补「二进制部署」章节、Profile B 精简包（可选）。

**总计约 4–5 个工作日**可达「三平台纯二进制 + CI 自动发布」。P0 是硬前置，P1 即可在本地验证可行性，P2 实现跨平台，P3 打磨。

---

## 11. 下一步建议

1. 先确认 **目标用户是否接受 ~1.5GB 大二进制**（本地 embedding）还是更想要 **~100MB 的 API-embedding 版**（Profile B）。这决定 P1 直接打哪条。
2. 批准 **P0 路径改造**（不分发也值得做，能让任意安装位置稳定运行）。
3. 提供仓库写权限用于加 `platformdirs` 依赖、改 `requirements.txt` 钉版、加 `linkora.spec`。

> 备注：当前 `pyproject.toml` 的 `[project]` 未声明运行时依赖（仅有 `[project.optional-dependencies].dev`），运行时依赖实际来自仓库根的 `requirements.txt`。打包前必须把 `requirements.txt` 钉版（含精确版本）作为构建 venv 的唯一来源，避免 CI 拉到不兼容新版本。
