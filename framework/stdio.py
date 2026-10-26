"""标准输出的编码兜底：Windows 上强制 UTF-8。

为什么需要
----------
Windows 的控制台/重定向默认编码**不是 UTF-8**：

- 控制台：cp437 / cp1252 / cp936（取决于系统语言）
- 重定向到文件或管道：locale 编码（同样不是 UTF-8）

于是任何一句 ``print("中文")`` 都会抛::

    UnicodeEncodeError: 'charmap' codec can't encode characters ...

这在 CI 上就是这么炸的：``packaging/ci/check_ps1_syntax.py`` 只是想打印一行
自检结果，就因为控制台是 cp1252 而整个步骤失败。

所以在**进程入口**处一次性把 stdout/stderr 切成 UTF-8：

- ``packaging/launcher.py``（冻结入口：内核与 python 解释器两种姿态）
- ``keeper/main.py``（内核启动）

``errors="replace"`` 保证遇到真的编不出来的字符（控制字符、孤立代理项）时
只是替换成 ``?``，而不是把整个进程带崩 —— 日志里少几个字符远好过进程死掉。
"""
from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """把 stdout / stderr 切成 UTF-8（能切就切，切不了就算了）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 流已经被关闭 / 不是 TextIOWrapper（比如被替换成了自定义对象）
            pass
