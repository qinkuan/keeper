"""
PyInstaller 冻结入口（**不要**直接冻结 keeper/main.py）。

为什么需要这个薄壳
------------------
仓库根目录本身就是 ``keeper`` 包（含 __init__.py），所以 ``import keeper.xxx``
要的是**它的父目录**在 sys.path 上。main.py 靠运行时改 sys.path 解决；
PyInstaller 的 Analysis 只认 ``pathex``，改不了运行时行为——直接冻结
main.py 会在打包期就报「找不到 keeper.config」。

于是这里给一个只有一行的入口：真正的包路径由 spec 里的
``pathex=[<仓库的父目录>]`` 提供。

另外 ``multiprocessing.freeze_support()`` 必须在入口最前面：冻结后的
multiprocessing 子进程会以 spawn 方式重新执行入口脚本，没有这一句，
子进程会把整个内核再跑一遍（起两个内核抢同一个数据库）。

两种运行姿态
------------
1. **内核**（默认）：``keeper-kernel --config <cfg> <port>``
2. **Python 解释器**（``--py``）：``keeper-kernel --py script.py [args...]``

为什么需要第 2 种：打包给「电脑上没装 Python」的普通用户时，PyInstaller 的
产物里**没有**独立的 python3 可执行文件（只有内核自己），于是
- 插件 bin 的 ``#!/usr/bin/env python3`` 找不到解释器（exit 127）；
- 模型写的 ``python3 x.py`` 同理；
- 代码沙箱也没法建 venv。

让**同一个**冻结产物兼当解释器，就不用再塞一份 CPython 进安装包：解释器本体
和 numpy 之类的依赖都已经在 bundle 里了。桌面壳会为它生成一个叫 ``python3``
的 shim 放进 PATH（见 desktop/sidecar.js）。
"""
import multiprocessing
import os
import sys


def _run_as_python(argv: list) -> int:
    """以「python 解释器」姿态运行后续参数。

    支持的用法（对齐 python 的常见调用）::

        keeper-kernel --py script.py [args...]
        keeper-kernel --py -c "print(1)"
        keeper-kernel --py -m module [args...]
        keeper-kernel --py --version

    冻结产物**不认 PYTHONPATH**（sys.path 由 PyInstaller 定死），所以脚本所在
    目录要手动加进 sys.path——沙箱生成的 main.py 就是靠这个 import 到同目录的
    keeper_tools.py。
    """
    import runpy

    args = list(argv)
    if not args:
        sys.stderr.write("usage: keeper-kernel --py <script.py|-c CODE|-m MODULE>\n")
        return 2

    # python 自身的这几个开关要在切 sys.argv 之前吃掉
    if args[0] in ("-V", "--version"):
        sys.stdout.write("Python %s\n" % sys.version.split()[0])
        return 0
    if args[0] in ("-h", "--help"):
        sys.stdout.write(
            "usage: keeper-kernel --py <script.py|-c CODE|-m MODULE> [args...]\n"
        )
        return 0

    if args[0] == "-c":
        code = args[1] if len(args) > 1 else ""
        sys.argv = ["-c"] + args[2:]
        exec(compile(code, "<string>", "exec"), {"__name__": "__main__", "__file__": "<string>"})
        return 0

    if args[0] == "-m":
        if len(args) < 2:
            sys.stderr.write("No module specified for -m\n")
            return 2
        mod = args[1]
        sys.argv = [mod] + args[2:]
        # run_module 会把 sys.path[0] 换成 cwd（跟 python -m 一致）
        runpy.run_module(mod, run_name="__main__", alter_sys=True)
        return 0

    script = args[0]
    if not os.path.isfile(script):
        sys.stderr.write("can't open file '%s': [Errno 2] No such file or directory\n" % script)
        return 2
    # 冻结产物忽略 PYTHONPATH：脚本同目录的模块要能 import
    script_dir = os.path.dirname(os.path.abspath(script))
    if script_dir and script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    sys.argv = [script] + args[1:]
    runpy.run_path(script, run_name="__main__")
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--py":
        raise SystemExit(_run_as_python(sys.argv[2:]))

    from keeper.main import main as keeper_main

    keeper_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
