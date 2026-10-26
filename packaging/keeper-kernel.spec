# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 规格：把 keeper 内核冻成**自包含**可执行文件（目标机不需要装 Python）。

为什么冻内核，而不是把 .venv 整个拷进 app
------------------------------------------
.venv 里记着基座解释器的**绝对路径**（这台机器是 /opt/anaconda3/bin/python3.13），
拷到别的 Mac 上 `bin/python` 直接失效；而「自带解释器」的发行版（python-build-standalone）
要从 GitHub 下载，本机网络到不了 GitHub。PyInstaller 把解释器本身与所有依赖
（langchain / fastapi / greenlet / pypdfium2 …）一起打进产物里，正是
「把 python 环境一起打包」要的效果。

产物形态：one-dir（不是 one-file）
----------------------------------
one-file 每次启动都要把几百 MB 解到临时目录，启动慢、还被 macOS 沙箱盯上；
one-dir 是一次性铺开，启动快、路径稳定（sidecar 还要按固定路径找前端产物）。

hiddenimports 为什么是手写的
---------------------------
keeper 的 provider 集成（openai / anthropic / ollama）是**按需 import** 的：
llm/config.py 的 PROVIDER_MAP 在运行时才决定 import 哪个包，PyInstaller 的
静态分析看不见 → 不点名就会在运行时炸 "No module named langchain_openai"。
"""
import sys
from pathlib import Path

# SPECPATH = 本 spec 所在目录（packaging/）
# 本 spec 所在目录（packaging/）。spec 是被 exec 的，没有 __file__，
# 用 PyInstaller 注入的 SPECPATH（= spec 所在目录）。
SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent                              # 仓库根 == keeper 包目录
PROJECT_ROOT = ROOT.parent                          # import 根：包目录的**父目录**
WEB_DIST = ROOT / "web" / "dist"

if not (ROOT / "main.py").is_file():
    raise SystemExit(f"spec 路径不对：{ROOT} 下没有 main.py")

# 前端产物一起冻进去：内核自己把它挂成站点根（main.py 的 web/dist）。
# 落点是 <MEIPASS>/keeper/web/dist —— 正好是 main.py 里
# `Path(__file__).parent / "web" / "dist"` 在冻结形态下算出来的位置。
datas = []
if WEB_DIST.is_dir():
    datas.append((str(WEB_DIST), "keeper/web/dist"))
else:
    print("[spec] 警告：没有 web/dist，冻出来的内核只提供 API（页面会 404）")
# 框架配置也放进包里：内核里不带参数的 load_config()（核心 agent 装配等）会
# 找 <包目录>/config.yaml。桌面壳会用 KEEPER_CONFIG 指到用户那份（~/.keeper/），
# 这份只是「单跑内核」时的兜底。
if (ROOT / "config.yaml").is_file():
    datas.append((str(ROOT / "config.yaml"), "keeper"))

# 原生库（.dylib / .dll / .so / .pyd）与数据文件分开收：原生库要走 binaries，
# PyInstaller 才会对 mac 上的 .dylib 做 ad-hoc 签名（.app 整体签名要求
# 嵌套的 dylib 也是签好的，否则 codesign 直接拒绝）。
binaries = []

hiddenimports = [
    # provider 集成：运行时按 PROVIDER_MAP 动态 import，静态分析看不见
    "langchain_openai",
    "langchain_anthropic",
    "langchain_ollama",
    "openai",
    # uvicorn 的协议实现是运行时按扩展名载入的
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 数据库驱动
    "aiosqlite",
    "sqlite3",
    # jieba 的词典是数据文件（collect_all 一并带上）
    "jieba",
    "jieba._compat",
]

# 明确排掉的：这些包体积大且本项目用不到（带上会让产物白胖几百 MB）
excludes = [
    "tkinter",
    "matplotlib",
    "numpy.f2py",
    "pytest",
    "_pytest",
    "IPython",
    "notebook",
    "jupyter",
    "torch",
    "tensorflow",
    "PIL.ImageQt",
    "setuptools",
    "pip",
]

# ---- 可选：litert-lm（本地向量检索的 embedding 运行时）----
# litert-lm 的 wheel 是**平台相关**的（mac arm64 / win x64 有，Intel Mac 没有），
# 所以它不在 requirements.txt 里，而是由 packaging/install_optional_deps.py
# 在打包时「尽力安装」——Intel Mac 上装不上就跳过，只少了语义检索，
# 关键词检索（SQLite FTS5 + BM25 + jieba）照常可用。
#
# 装上了就**必须**把它的原生库一起收进产物：litert 用 ctypes 按名字加载
#   mac: liblitert-lm.dylib      Windows: litert-lm.dll
# 路径是 importlib.resources.files("litert_lm") / <库名>，也就是原生库必须
# 落在 <产物>/litert_lm/ 目录下。少了它，向量检索会直接报
# "Could not find liblitert-lm.dylib"（关键词检索不受影响）。
try:
    from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

    hiddenimports += collect_submodules("litert_lm")
    binaries += collect_dynamic_libs("litert_lm")
    print("[spec] 已收集 litert-lm（向量检索运行时）")
except Exception as e:  # noqa: BLE001 没装 litert-lm 不该让构建失败
    print(f"[spec] 没有 litert-lm，跳过向量检索运行时：{e}")

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="keeper-kernel",   # 可执行文件名（放在 COLLECT 出来的 kernel/ 目录里）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 内核的 stdout/stderr 会被 sidecar 收走做日志面板
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
    name="kernel",
)
