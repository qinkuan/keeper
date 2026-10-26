#!/usr/bin/env python3
"""按平台安装**可选**的运行时依赖（当前只有 litert-lm）。

为什么单独一个脚本
------------------
litert-lm 提供本地向量检索用的 embedding 引擎（``.litertlm`` 模型文件）。
它的预编译 wheel 是**平台相关**的：

    macOS arm64 ✓   macOS x86_64 ✗（Intel Mac 根本没有这个 wheel）
    Windows x64 ✓   Linux x86_64 / aarch64 ✓

所以它**不能**进 requirements.txt 的硬依赖 —— 否则在 Intel Mac 上
``pip install -r requirements.txt`` 会直接失败，把整个安装流程堵死。

这里的做法是：**打包时按平台尽力装，装不上就跳过**。跳过只少了语义检索，
关键词检索（SQLite FTS5 + BM25 + jieba）完全不受影响。

打包产物里能不能用上向量检索，还取决于 PyInstaller 有没有把 litert 的原生库
（mac 的 ``liblitert-lm.dylib`` / Windows 的 ``litert-lm.dll``）收进去 ——
那部分在 packaging/keeper-kernel.spec 里做。

用法::

    python3 packaging/install_optional_deps.py

退出码恒为 0：它只是「尽力而为」，不该把构建搞挂。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

#: (发行名, 版本约束, import 名)
OPTIONAL_DEPS = [
    ("litert-lm", "0.18.0", "litert_lm"),
]


def already_installed(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def install(dist: str, version: str) -> bool:
    """装一个可选依赖；成功返回 True。"""
    cmd = [sys.executable, "-m", "pip", "install", "-q", f"{dist}=={version}"]
    print(f"    安装 {dist}=={version} ...", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        print(f"    跳过 {dist}：{e}")
        return False
    if proc.returncode != 0:
        # pip 的报错里最有用的是最后几行（"No matching distribution found"）
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        print(f"    跳过 {dist}（这个平台没有对应的 wheel）")
        for line in tail:
            print(f"      {line}")
        return False
    print(f"    {dist} 就绪")
    return True


def main() -> int:
    print("==> 可选运行时依赖（litert-lm：本地向量检索的 embedding 引擎）")
    for dist, version, import_name in OPTIONAL_DEPS:
        if already_installed(import_name):
            print(f"    {dist} 已安装，跳过")
            continue
        install(dist, version)
    print("    （没装上也不影响构建：关键词检索照常可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
