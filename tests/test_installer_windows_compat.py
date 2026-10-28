#!/usr/bin/env python3
"""plugin-installer 的 Windows 兼容层自测。

测的是**装进插件库的第三方包**在 Windows 上能不能跑起来：Windows 上没有
shebang，内核靠**文件后缀**判断「这是脚本、要用解释器启动」
（tools/subprocess_tool.py 的 build_argv 只认 .py/.pyw），所以无后缀的
Python 脚本必须被补上 .py 后缀，清单里的 command 与包内文档里的路径引用
也要跟着改。

不联网：直接测新加的那层兼容逻辑（_scan_script_commands /
_compat_windows_scripts）。

用法：
    .venv/bin/python tests/test_installer_windows_compat.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "plugins" / "plugin-installer" / "bin" / "installer.py"

FAILED = 0


def check(cond, msg):
    global FAILED
    if cond:
        print("  ✓ %s" % msg)
    else:
        FAILED += 1
        print("  ✗ %s" % msg)


def load_installer():
    spec = importlib.util.spec_from_file_location("installer_mod", str(INSTALLER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_pkg(root: Path, *, node: bool = False) -> Path:
    """造一个「Claude 生态风格」的包：bin 脚本无后缀、清单用 keeper 格式。"""
    pkg = root / ("node-plugin" if node else "cool-plugin")
    (pkg / "bin").mkdir(parents=True)
    (pkg / "skills").mkdir(parents=True)

    # 无后缀的 python 脚本（第三方包的常见写法）
    (pkg / "bin" / "runner").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "req = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'ok': True, 'result': 'hi ' + str(req.get('tool'))}))\n",
        encoding="utf-8",
    )
    # 本来就带 .py 的（不该被动）
    (pkg / "bin" / "helper.py").write_text(
        "#!/usr/bin/env python3\nprint('x')\n", encoding="utf-8"
    )
    # 真二进制（不该被动）
    (pkg / "bin" / "native").write_bytes(b"\x7fELF\x02\x01\x01\x00binary")
    # node 脚本（node=True 时加）：改名也救不了，只能警告
    if node:
        (pkg / "bin" / "install.mjs").write_text(
            "#!/usr/bin/env node\nconsole.log('x')\n", encoding="utf-8"
        )

    cmd = "${PLUGIN_ROOT}/bin/runner"
    if node:
        cmd = "${PLUGIN_ROOT}/bin/install.mjs"
    (pkg / "keeper-plugin.json").write_text(
        json.dumps(
            {
                "name": pkg.name,
                "version": "1.2.3",
                "description": "第三方包",
                "skill": "skills/SKILL.md",
                "bin": [
                    {
                        "name": "runner",
                        "summary": "跑一下",
                        "command": cmd,
                        "tools": [{"name": "run", "parameters": {"type": "object"}}],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (pkg / "skills" / "SKILL.md").write_text(
        "# 用法\n\n执行 bin/runner 即可，参见 bin/runner.py 与其它路径 bin/runnerx。\n",
        encoding="utf-8",
    )
    return pkg


def main() -> int:
    mod = load_installer()
    tmp = Path(tempfile.mkdtemp(prefix="keeper-installer-compat-"))
    try:
        print("1) 探测：无后缀 python 脚本被认出来")
        pkg = make_pkg(tmp)
        found = mod._scan_script_commands(pkg)
        check(
            found == [("bin/runner", "python")],
            "只认出 bin/runner 是 python 脚本：%s" % (found,),
        )

        print("2) 兼容处理：改名 + 改清单 + 改文档")
        renamed, node_scripts = mod._compat_windows_scripts(pkg)
        check(
            renamed == [("bin/runner", "bin/runner.py")],
            "改名结果正确：%s" % (renamed,),
        )
        check(node_scripts == [], "没有 node 脚本")
        check((pkg / "bin" / "runner.py").is_file(), "bin/runner.py 出来了")
        check(not (pkg / "bin" / "runner").exists(), "旧的无后缀文件没了")
        check((pkg / "bin" / "helper.py").is_file(), "本来就带 .py 的没被动")
        check((pkg / "bin" / "native").is_file(), "二进制没被动")

        manifest = json.loads((pkg / "keeper-plugin.json").read_text(encoding="utf-8"))
        cmd = manifest["bin"][0]["command"]
        check(cmd == "${PLUGIN_ROOT}/bin/runner.py", "清单 command 改对了：%s" % cmd)

        skill = (pkg / "skills" / "SKILL.md").read_text(encoding="utf-8")
        check("bin/runner.py" in skill, "文档里的路径引用改了")
        check("bin/runner.py.py" not in skill, "没有二次替换出来的 .py.py")
        check("bin/runnerx" in skill, "相似路径没被误伤")

        print("3) 幂等：再跑一次没有副作用")
        renamed2, _ = mod._compat_windows_scripts(pkg)
        check(renamed2 == [], "第二次没有再改名")
        cmd2 = json.loads((pkg / "keeper-plugin.json").read_text(encoding="utf-8"))["bin"][0][
            "command"
        ]
        check(cmd2 == "${PLUGIN_ROOT}/bin/runner.py", "清单没被改坏：%s" % cmd2)

        print("4) node 脚本：只警告，不改名")
        npkg = make_pkg(tmp, node=True)
        renamed3, node3 = mod._compat_windows_scripts(npkg)
        check(renamed3 == [], "node 脚本没被改名")
        check(node3 == ["bin/install.mjs"], "node 脚本被列进警告：%s" % (node3,))
        check((npkg / "bin" / "install.mjs").is_file(), "node 脚本原样保留")

        print("5) 清单里带参数的 command 也不该被改坏")
        p2 = tmp / "with-args"
        (p2 / "bin").mkdir(parents=True)
        (p2 / "bin" / "t").write_text("#!/usr/bin/env python3\nprint('x')\n", encoding="utf-8")
        (p2 / "keeper-plugin.json").write_text(
            json.dumps(
                {
                    "name": "with-args",
                    "bin": [
                        {
                            "name": "t",
                            "command": "${PLUGIN_ROOT}/bin/t --flag",
                            "tools": [{"name": "x"}],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        mod._compat_windows_scripts(p2)
        cmd3 = json.loads((p2 / "keeper-plugin.json").read_text(encoding="utf-8"))["bin"][0][
            "command"
        ]
        check(cmd3 == "${PLUGIN_ROOT}/bin/t.py --flag", "参数被保留：%s" % cmd3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print("结果：%d 项失败" % FAILED)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
