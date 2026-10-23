"""守卫逻辑的回归测试：白名单该放行的放行，绕过手法该拦的拦住。

（放在 ``keeper/tests/``，与 ``test_plugin_lib.py``、``test_planner_parse.py`` 同处）
写成独立脚本而不是 pytest，是为了让「跑一遍」不需要额外依赖——这个模块
是安全边界，值得有一份能随手执行、结果可读的验证。

运行：`python keeper/tests/test_guard.py`
"""
from __future__ import annotations

import sys
from pathlib import Path

# parents: [0]=tests/  [1]=keeper/  [2]=项目根（keeper 包的父目录）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.plugin import plugins_root  # noqa: E402
from keeper.tools.guard import check_call  # noqa: E402

WS = Path("/Users/admin/.keeper/workspace/session/s1")
AGENT_SPACE = Path("/Users/admin/.keeper/workspace/session/s1/.agent")
# 插件库根（默认 ~/.keeper/plugins）：agent 装卸插件、读技能随包附件都要读写它。
LIB = Path(plugins_root())
# 配置里的 settings.yaml：明确**不**随插件库一起放开。
SETTINGS = Path.home() / ".keeper" / "settings.yaml"

# (命令, 只读区是否应放行, 说明)
READONLY_CASES = [
    ("ls -la", True, "列目录"),
    ("grep -rn foo .", True, "只读 grep"),
    ("git status", True, "git 只读子命令"),
    ("pip list", True, "pip 只读子命令"),
    ("cat a.txt | head -20", True, "管道读"),
    ("find . -name *.py", True, "find 不带 -exec"),
    ("echo hi", True, "echo"),
    ("wc -l notes.md", True, "统计行数"),
    ("cp data.csv copy.csv", False, "cp 是写命令"),
    ("rm -rf /tmp/x", False, "rm 是写命令"),
    ("git push origin main", False, "git 写子命令"),
    ("pip install requests", False, "pip install 是写"),
    ("python -c print(1)", False, "解释器"),
    ("node -e 1", False, "解释器"),
    ("find . -name *.py -exec rm F", False, "find -exec"),
    ("ls | xargs rm", False, "xargs"),
    ("sudo rm x", False, "sudo 包装"),
    ("grep foo a.txt > out.txt", False, "重定向写文件"),
    ("grep foo /etc/hosts", False, "绝对路径越界"),
    ("cat ../../etc/passwd", False, ".. 越界"),
    ("cat ~/.ssh/id_rsa", False, "~ 越界"),
    ("cd /tmp && rm -rf x", False, "复合命令"),
    # 用 heredoc 写整个 HTML 文件：正文里的 </body> 曾经被误判成越界路径 /body。
    # 现在守卫会剥掉正文、放行落到工作空间内的绝对路径重定向。
    (
        "cat > /Users/admin/.keeper/workspace/session/s1/snake.html <<'EOF'\n"
        "<html><body><h1>hi</h1></body></html>\nEOF",
        True,
        "heredoc 写 HTML 到会话区（根内绝对路径）",
    ),
    # `...tail` 是 shell 字符串截取的省略写法。作为 `ls` 的参数它是普通文件名，
    # 合法；作为命令它不在白名单里，该拦。这里两组一起覆盖，确保它**不会**
    # 因为 `..` 前缀被误判成路径穿越（那才是真正的误杀）。
    ("ls -la ...tail", True, "...tail 作为文件名参数"),
    ("...tail -c 200 f", False, "...tail 作为命令（白名单拦）"),
]

# (命令, 可写区是否应拦截, 说明)
WRITABLE_CASES = [
    ("ls -la", False, "放行 ls"),
    ("git commit -m x", False, "放行写命令（正常开发流程）"),
    ("npm install", False, "放行包管理"),
    ("grep foo /etc/hosts", True, "仍拦越界读系统文件"),
    ("pip install requests", False, "包管理器豁免路径检查"),
    # heredoc 写文件：正文里的 </html> 不是路径，可写区不应拦
    (
        "cat > /Users/admin/.keeper/workspace/session/s1/snake.html <<'EOF'\n"
        "<html><body><h1>hi</h1></body></html>\nEOF",
        False,
        "heredoc 写 HTML（可写区不拦）",
    ),
    # 引号里的 HTML 片段也不是路径
    ('echo "<html></html>"', False, "引号内 HTML 片段不是路径"),
    # node -e 内联脚本里的正则斜杠（/const CELL = (\d+)/）不是越界路径
    (
        'node -e "const c=+/const CELL = (\\d+)/.exec(s)[1]; const open=(s.match(/<script>/g)||[]).length"',
        False,
        "node 内联脚本的正则斜杠不是路径",
    ),
]


def _check(cmd: str, read_only: bool, extra_roots=None):
    return check_call(
        tool_name="bash__run",
        args={"command": cmd},
        dangerous=True,
        read_only=read_only,
        workspace=WS,
        extra_roots=extra_roots,
    )


# 插件库放开后的边界。（命令, 是否应放行, 说明）—— 只读区视角
PLUGIN_READONLY_CASES = [
    (f"ls {LIB}/dashi-ppt-skill/skill/scripts/", True, "读插件库目录"),
    (f"cat {LIB}/dashi-ppt-skill/skill/scripts/a.js", True, "读插件随包脚本"),
    (f"grep -rn foo {LIB}/dashi-ppt-skill/", True, "在插件库内 grep"),
    (f"ls {LIB}", True, "读插件库根本身"),
    ("node -v", True, "解释器版本探测（纯读）"),
    ("node --version", True, "解释器版本探测（长参）"),
    ("python3 -V", True, "解释器版本探测（python 用 -V）"),
    ("python3 --version", True, "解释器版本探测（python 长参）"),
    # 版本探测之外，解释器照旧是洞：能执行任意代码
    ("node -e 'console.log(1)'", False, "解释器执行代码仍拦"),
    ("node script.js", False, "解释器跑脚本仍拦"),
    ("python3 -v", False, "python 的 -v 是 verbose（会执行脚本），仍拦"),
    ("sh -v", False, "shell 的 -v 不是版本号，仍拦"),
    (f"rm -rf {LIB}/x", False, "只读区不删插件"),
    (f"cat {LIB}/../settings.yaml", False, "从插件库上跳读 settings.yaml"),
    (f"cat {SETTINGS}", False, "settings.yaml 明确不放开"),
    ("grep foo /etc/hosts", False, "系统文件仍拦"),
    ("cat ~/.ssh/id_rsa", False, "ssh 私钥仍拦"),
]

# （命令, 是否应拦截, 说明）—— 可写区视角（只查路径越界）
PLUGIN_WRITABLE_CASES = [
    (f"echo x > {LIB}/_probe.txt", False, "插件库可写（装插件要写它）"),
    (f"ls {LIB}/pkg/skill/assets/skill/theme.png", False, "插件库可读"),
    (f"cat {SETTINGS}", True, "settings.yaml 仍拦"),
    (f"cat {LIB}/../settings.yaml", True, "上跳读 settings.yaml 仍拦"),
    ("grep foo /etc/hosts", True, "系统文件仍拦"),
]


def _main_plugin_cases() -> int:
    bad = 0
    roots = [AGENT_SPACE, LIB]
    print("=== 插件库放开（只读区）===")
    for cmd, want_ok, why in PLUGIN_READONLY_CASES:
        r = _check(cmd, read_only=True, extra_roots=roots)
        ok = (r is None) == want_ok
        if not ok:
            bad += 1
        print(
            f"{'PASS' if ok else 'FAIL'}  "
            f"{'放行' if r is None else '拦截'}  {why:<28s} {cmd[:40]}"
        )
        if not ok:
            print("        理由:", (r or "")[:100])

    print("\n=== 插件库放开（可写区）===")
    for cmd, want_block, why in PLUGIN_WRITABLE_CASES:
        r = _check(cmd, read_only=False, extra_roots=roots)
        blocked = r is not None
        if blocked != want_block:
            bad += 1
        print(
            f"{'PASS' if blocked == want_block else 'FAIL'}  "
            f"{'拦截' if blocked else '放行'}  {why}"
        )
        if blocked != want_block:
            print("        理由:", (r or "")[:100])
    return bad


def main() -> None:
    bad = 0
    print("=== 只读工作区 ===")
    for cmd, want_ok, why in READONLY_CASES:
        r = _check(cmd, read_only=True)
        ok = r is None
        if ok != want_ok:
            bad += 1
        print(
            f"{'PASS' if ok == want_ok else 'FAIL'}  "
            f"{'放行' if ok else '拦截'}  {why:<18s} {cmd[:42]}"
        )
        if not ok and ok != want_ok:
            print("        理由:", r[:100])

    print("\n=== 可写工作区（只查路径越界）===")
    for cmd, want_block, why in WRITABLE_CASES:
        r = _check(cmd, read_only=False)
        blocked = r is not None
        if blocked != want_block:
            bad += 1
        print(
            f"{'PASS' if blocked == want_block else 'FAIL'}  "
            f"{'拦截' if blocked else '放行'}  {why}"
        )
        if blocked and blocked != want_block:
            print("        理由:", r[:100])

    bad += _main_plugin_cases()

    print()
    print(f"失败 {bad} 项" if bad else "全部通过")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()