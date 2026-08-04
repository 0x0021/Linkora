# -*- mode: python ; coding: utf-8 -*-
"""
Linkora 单目录（onedir）PyInstaller 构建配置 —— P0 路径可重定位 + P1 钉版依赖。

设计要点：
- 入口：main.py（其 ``if __name__ == "__main__"`` 调 ``main(PROJECT_ROOT)``）。
- 资源根：冻结态下 src/paths.py 用 ``sys._MEIPASS`` 作为应用根，故把
  web/static、web/templates、config.yaml 样例打进 datas，解包后落在 ``_MEIPASS`` 下。
- 数据目录：冻结态一律落到用户数据目录（platformdirs / LINKORA_HOME / ~/.linkora），
  与安装目录解耦（见 src/paths.py 的解析优先级），因此只读安装目录不会崩。
- 采用 one-folder（COLLECT）而非 one-file：1.5GB 体量下，one-file 每次启动要整体
  解包到临时目录，启动极慢；one-folder 直接运行 dist/linkora/linkora 即可。
  若要 one-file，把下方 COLLECT 折叠进 EXE（a.binaries 等）即可，但启动会明显变慢。

运行（开发机 / 容器内）:
    .venv/bin/pyinstaller linkora.spec
产物：dist/linkora/linkora（可执行）+ dist/linkora/_internal/*（依赖）
"""

import os

ROOT = os.path.abspath(SPEC_SOURCE if "SPEC_SOURCE" in globals() else ".")

# PyInstaller 在分析期会跟随 import 收集依赖，但以下均为「函数内懒加载」或内部有动态
# 子模块的库，必须显式声明 hiddenimports，否则运行期 ImportError：
#   - sentence_transformers / transformers / tokenizers / huggingface_hub（torch 生态，内部大量动态导入）
#   - faiss（实际由 faiss-cpu 提供，import 名为 faiss）
#   - playwright（浏览器自动化，懒加载）
#   - scipy / onnxruntime / rapidocr_onnxruntime（OCR 链路）
# 注意：sklearn / torchvision 在本项目 venv 中未安装，切勿加入，否则构建直接失败。
hidden_imports = [
    "torch",
    "sentence_transformers",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "faiss",
    "scipy",
    "onnxruntime",
    "rapidocr_onnxruntime",
    "playwright",
    "playwright.sync_api",
    "playwright.async_api",
    # Web / 文档解析链路（多为静态导入，列出仅为稳妥）
    "fastapi",
    "uvicorn",
    "jinja2",
    "pydantic",
    "fitz",            # PyMuPDF
    "pptx",            # python-pptx
    "docx",            # python-docx
    "openpyxl",
    "PIL",             # Pillow
    "jieba",
    "regex",
    "requests",
    "rich",
    "psutil",
    "platformdirs",
    "numpy",
]

a = Analysis(
    ["main.py"],
    pathex=[ROOT],
    binaries=[],
    # 只读资源：冻结态由 src/paths.get_app_root() 从 sys._MEIPASS 读取
    datas=[
        (os.path.join(ROOT, "web", "static"), "web/static"),
        (os.path.join(ROOT, "web", "templates"), "web/templates"),
        (os.path.join(ROOT, "config.yaml"), "."),  # 打包态首次运行拷贝到用户数据目录
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 测试与开发工具不进发布包，缩小体积
        "pytest",
        "_pytest",
        "pytest_cov",
        "pytest_timeout",
        "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="linkora",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # torch OpenMP 库经 UPX 压缩后易损坏，关闭
    console=True,  # Linkora 是 CLI 服务，保留终端输出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="linkora",
)
