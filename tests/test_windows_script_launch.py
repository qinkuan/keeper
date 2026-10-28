#!/usr/bin/env python3
"""Windows 上「脚本怎么启动」的自测。

Windows 没有 shebang：CreateProcess 只认真可执行文件，文本脚本（.py / .js /
无后缀）一律起不来，报「不是有效的 Win32 应用程序」。mac/Linux 有 shebang
兜底，所以这类问题在开发态看不出来——这些断言就是替 Windows 跑的。

用法：
    .venv/bin/python tests/test_windows_script_launch.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# 仓库本身是 keeper 包（keeper/__init__.py 存在），所以要把**上一级**目录
# 加进 sys.path，跟其它测试（test_guard.py 等）保持一致。
sys.path.insert(0, str(ROOT.parent))

FAILED = 0


def check(cond, msg):
    global FAILED
    if cond:
        print("  ✓ %s" % msg)
    else:
        FAILED += 1
        print("  ✗ %s" % msg)


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def main() -> int:
    from keeper.framework.runtime import script_kind, windows_script_interpreter
    from keeper.tools.subprocess_tool import build_argv
    from keeper.mcp.launcher import connection_config

    tmp = Path(tempfile.mkdtemp(prefix="keeper-win-script-"))
    try:
        print("1) script_kind：后缀优先，其次读 shebang")
        py = write(tmp / "a.py", "#!/usr/bin/env python3\nprint('x')\n")
        pyw = write(tmp / "a.pyw", "print('x')\n")
        mjs = write(tmp / "a.mjs", "#!/usr/bin/env node\n")
        cjs = write(tmp / "a.cjs", "console.log(1)\n")
        sh_py = write(tmp / "noext", "#!/usr/bin/env python3\n")
        sh_py3 = write(tmp / "noext3", "#!/usr/bin/python3.13\n")
        sh_node = write(tmp / "noext_node", "#!/usr/bin/env node\n")
        sh_sh = write(tmp / "noext_sh", "#!/bin/sh\n")
        binary = tmp / "native"
        binary.write_bytes(b"\x7fELF\x02\x01\x01\x00binary")
        empty = write(tmp / "empty", "")

        check(script_kind(str(py)) == "python", ".py → python")
        check(script_kind(str(pyw)) == "python", ".pyw → python")
        check(script_kind(str(mjs)) == "node", ".mjs → node")
        check(script_kind(str(cjs)) == "node", ".cjs → node")
        check(script_kind(str(sh_py)) == "python", "无后缀 + python shebang → python")
        check(script_kind(str(sh_py3)) == "python", "无后缀 + python3.13 shebang → python")
        check(script_kind(str(sh_node)) == "node", "无后缀 + node shebang → node")
        check(script_kind(str(sh_sh)) == "", "无后缀 + sh shebang → 不认")
        check(script_kind(str(binary)) == "", "二进制 → 不认")
        check(script_kind(str(empty)) == "", "空文件 → 不认")
        check(
            script_kind(str(tmp / "nope.py")) == "python",
            "不存在的 .py 仍按后缀判为 python（存在性由调用方管）",
        )

        print("2) windows_script_interpreter：选内置运行时；.cmd 用 cmd /c 包一层")
        fake_python = write(tmp / "runtime" / "python3.exe", "")
        fake_node = write(tmp / "runtime" / "node.exe", "")
        env = {"KEEPER_PYTHON": str(fake_python), "KEEPER_NODE": str(fake_node)}
        with mock.patch.dict(os.environ, env, clear=False):
            r = windows_script_interpreter(str(py), windows=True)
            check(r == (str(fake_python), []), "内置 python 被选中：%s" % (r,))
            r = windows_script_interpreter(str(mjs), windows=True)
            check(r == (str(fake_node), []), "内置 node 被选中：%s" % (r,))
            r = windows_script_interpreter(str(sh_py), windows=True)
            check(r == (str(fake_python), []), "无后缀 python 脚本走内置 python")
            r = windows_script_interpreter(str(sh_node), windows=True)
            check(r == (str(fake_node), []), "无后缀 node 脚本走内置 node")

            shim = write(tmp / "runtime" / "python3.cmd", "@echo off\r\n")
            with mock.patch.dict(os.environ, {"KEEPER_PYTHON": str(shim)}, clear=False):
                r = windows_script_interpreter(str(py), windows=True)
                ok = r is not None and r[0].lower().endswith(("cmd.exe", "comspec"))
                check(ok, "解释器是 .cmd 时借 cmd.exe：%s" % (r,))
                check(
                    r is not None and len(r[1]) == 2 and r[1][0] == "/c",
                    "前置参数是 /c + 脚本路径：%s" % (r,),
                )

        print("3) 非 Windows / 非脚本 / 找不到解释器 → 不插手")
        check(windows_script_interpreter(str(py), windows=False) is None, "非 Windows → None")
        check(
            windows_script_interpreter(str(tmp / "nope.py"), windows=True) is None,
            "文件不存在 → None",
        )
        check(windows_script_interpreter(str(binary), windows=True) is None, "二进制 → None")
        with mock.patch("keeper.framework.runtime._system_interpreter", lambda kind: ""):
            with mock.patch.dict(os.environ, {"KEEPER_PYTHON": "", "KEEPER_NODE": ""}):
                r = windows_script_interpreter(str(py), windows=True)
                check(r is None, "找不到任何解释器 → None（让调用方照旧直接执行）")

        print("3b) .cmd / .bat 借 cmd.exe /c")
        bat = write(tmp / "run.cmd", "@echo off\r\necho hi\r\n")
        r = windows_script_interpreter(str(bat), windows=True)
        check(
            r is not None and r[0].lower().endswith(("cmd.exe", "comspec")) and r[1] == ["/c"],
            ".cmd → cmd.exe /c：%s" % (r,),
        )

        print("4) build_argv：Windows 上脚本走解释器")
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("keeper.framework.runtime.is_windows", return_value=True):
                argv = build_argv(str(py), ["--flag"])
                check(
                    argv == [str(fake_python), str(py), "--flag"],
                    ".py → [解释器, 脚本, 参数]：%s" % (argv,),
                )
                argv = build_argv(str(sh_py), [])
                check(
                    argv == [str(fake_python), str(sh_py)],
                    "无后缀 python 脚本同样起得来：%s" % (argv,),
                )
                argv = build_argv(str(mjs), [])
                check(argv == [str(fake_node), str(mjs)], ".mjs 走内置 node：%s" % (argv,))
                argv = build_argv(str(binary), [])
                check(argv == [str(binary)], "二进制照旧直接执行：%s" % (argv,))
                argv = build_argv("node", ["server.js"])
                check(argv == ["node", "server.js"], "PATH 里的命令不受影响：%s" % (argv,))

        print("5) MCP 连接配置：stdio 型脚本同样要选解释器")
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("keeper.framework.runtime.is_windows", return_value=True):
                cfg = connection_config(
                    "x", {"command": str(sh_py), "args": ["--port", "1"], "transport": "stdio"}
                )
                check(
                    cfg["command"] == str(fake_python)
                    and cfg["args"] == [str(sh_py), "--port", "1"],
                    "MCP stdio 脚本 → 解释器 + 脚本 + 原参数：%s" % (cfg,),
                )
            with mock.patch("keeper.framework.runtime.is_windows", return_value=True):
                cfg = connection_config("x", {"url": "http://h/mcp"})
                check(cfg.get("url") == "http://h/mcp", "http 型不受影响：%s" % (cfg,))
            with mock.patch("keeper.framework.runtime.is_windows", return_value=True):
                cfg = connection_config("x", {"command": "npx", "args": ["-y", "srv"]})
                check(
                    cfg["command"] == "npx" and cfg["args"] == ["-y", "srv"],
                    "PATH 里的 MCP 命令不受影响：%s" % (cfg,),
                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print("结果：%d 项失败" % FAILED)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    import shutil

    sys.exit(main())
