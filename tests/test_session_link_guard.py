"""验证守卫侧：agent 经会话工作区里的 my-space 链接访问「我的空间」是否放行。

这是整个功能的**安全前提**——如果守卫因为 resolve 到工作区外而拦住，链接就是
个摆设；如果不该放的被放行了，那是漏洞。

跑法：python keeper/tests/test_session_link_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.tools.guard import check_call  # noqa: E402
from keeper.zonefilesystem.session_link import LINK_NAME, ensure_my_space_link  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}{('  → ' + extra) if extra else ''}")


class Tool:
    """最小工具桩：check_call 只读 dangerous / parameters。"""

    def __init__(self) -> None:
        self.dangerous = True
        self.parameters = {"command": {"type": "string"}}


def guard(cmd: str, ws: Path, extra: list, read_only: bool = False):
    return check_call(
        tool_name="bash__run",
        args={"command": cmd},
        dangerous=True,
        read_only=read_only,
        workspace=ws,
        extra_roots=extra,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 家目录 + settings：把我的空间配到 tmp/my-space
        home = tmp / "home"
        (home / ".keeper").mkdir(parents=True)
        os.environ["HOME"] = str(home)
        user_space = tmp / "my-space"
        (user_space / "docs").mkdir(parents=True)
        (user_space / "docs" / "a.md").write_text("x", encoding="utf-8")
        (home / ".keeper" / "settings.yaml").write_text(
            f"user_space:\n  path: {user_space}\n", encoding="utf-8"
        )

        session = tmp / "workspace" / "session" / "s1"
        ensure_my_space_link(session)
        link = session / LINK_NAME

        print("\n=== 守卫：经链接访问我的空间（extra_roots 含我的空间）===")
        # 这是 resolve_workspace 组出来的 roots：会话空间 + 我的空间
        ws = session
        extra = [user_space]

        for cmd, label in [
            (f"cat {link}/docs/a.md", "读链接里的文件（绝对路径）"),
            (f"ls {link}", "列链接目录"),
            (f"grep -r foo {link}/docs", "递归搜链接目录"),
        ]:
            r = guard(cmd, ws, extra)
            check(label, r is None, f"被拦：{r}")

        print("\n=== 守卫：经链接写到我的空间 ===")
        r = guard(f"echo hi > {link}/docs/new.txt", ws, extra)
        # 可写区（read_only=False）下只查路径越界；我的空间在 extra_roots 内 → 放行
        check("写链接里的文件（我的空间在合法根内）", r is None, f"被拦：{r}")

        print("\n=== 反向：链接不在 extra_roots 时（我的空间没配）应被拦 ===")
        # 模拟「我的空间未配置」：roots 里没有 user_space，链接 resolve 到外面
        r = guard(f"cat {link}/docs/a.md", ws, [])  # 没有 extra_roots
        check("我的空间不在合法根 → 经链接访问被拦", r is not None, "竟然放行了")

        print("\n=== 对照：会话空间内的普通文件照常放行 ===")
        (session / "local.txt").write_text("x", encoding="utf-8")
        r = guard("cat local.txt", ws, [])
        check("会话空间内相对路径放行", r is None, f"被拦：{r}")
        r = guard(f"cat {session}/local.txt", ws, [])
        check("会话空间内绝对路径放行", r is None, f"被拦：{r}")

        print("\n=== 对照：真正的越界仍被拦 ===")
        r = guard("cat /etc/hosts", ws, extra)
        check("读 /etc/hosts 被拦", r is not None, "竟然放行了")

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
