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

from keeper.tools.guard import check_call  # noqa: E402

WS = Path("/Users/admin/.keeper/workspace/session/s1")

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
]


def _check(cmd: str, read_only: bool):
    return check_call(
        tool_name="bash__run",
        args={"command": cmd},
        dangerous=True,
        read_only=read_only,
        workspace=WS,
    )


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

    print()
    print(f"失败 {bad} 项" if bad else "全部通过")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()