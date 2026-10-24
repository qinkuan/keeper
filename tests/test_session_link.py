"""session_link 的自测：直接跑 `python keeper/tests/test_session_link.py`。

覆盖：正常建链、幂等、改指向后重建、我的空间未配/不存在、成环防护、
链接真的能读到底层文件、以及**守卫侧**经链接访问是否放行。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 让脚本能直接跑（tests/ 的上两级是项目根）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.zonefilesystem.session_link import (  # noqa: E402
    LINK_NAME,
    ensure_my_space_link,
    my_space_root,
)

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


def write_settings(tmp: Path, space: str) -> None:
    """把 keeper 的家目录指到 tmp，并写一份只含 user_space 的 settings.yaml。"""
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    # user_settings 用 Path.home()，改 HOME 即改家目录（模块内无缓存）
    (home / ".keeper").mkdir(parents=True, exist_ok=True)
    (home / ".keeper" / "settings.yaml").write_text(
        f"user_space:\n  path: {space}\n", encoding="utf-8"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        user_space = tmp / "my-space"
        (user_space / "docs").mkdir(parents=True)
        (user_space / "docs" / "a.md").write_text("hello from my space", encoding="utf-8")
        write_settings(tmp, str(user_space))

        print("\n=== 建链接 ===")
        session = tmp / "workspace" / "session" / "s1"
        link = ensure_my_space_link(session)
        check("返回链接路径", link == session / LINK_NAME, str(link))
        check("目录被创建", session.is_dir())
        check("是软链接", (session / LINK_NAME).is_symlink())
        check(
            "指向我的空间（绝对路径）",
            os.readlink(session / LINK_NAME) == str(user_space),
            os.readlink(session / LINK_NAME),
        )
        check("经链接能读到底层文件", (session / LINK_NAME / "docs" / "a.md").read_text(encoding="utf-8") == "hello from my space")
        check("my_space_root() 读到我配的路径", my_space_root() == user_space, str(my_space_root()))

        print("\n=== 幂等与改指向 ===")
        link2 = ensure_my_space_link(session)
        check("重复调用不报错且路径不变", link2 == link)
        check("仍是同一个链接（没被重建）", (session / LINK_NAME).is_symlink())

        # 用户改了「我的空间」路径 → 再调用应重建指向
        moved = tmp / "my-space-2"
        moved.mkdir()
        write_settings(tmp, str(moved))
        ensure_my_space_link(session)
        check("我的空间路径变了 → 链接跟着重建", os.readlink(session / LINK_NAME) == str(moved), os.readlink(session / LINK_NAME))

        print("\n=== 不该建的情况（都不抛异常） ===")
        # 未配置
        write_settings(tmp, "")
        s2 = tmp / "workspace" / "session" / "s2"
        r = ensure_my_space_link(s2)
        check("我的空间未配置 → 回 None", r is None, str(r))
        check("未配置时也把会话目录建好了", s2.is_dir())
        check("未配置时没建链接", not (s2 / LINK_NAME).exists())

        # 路径不存在
        write_settings(tmp, str(tmp / "not-exist"))
        s3 = tmp / "workspace" / "session" / "s3"
        check("我的空间目录不存在 → 回 None 不抛", ensure_my_space_link(s3) is None)

        # 成环：我的空间 = 会话空间的父目录
        write_settings(tmp, str(tmp / "workspace"))
        s4 = tmp / "workspace" / "session" / "s4"
        r4 = ensure_my_space_link(s4)
        check("目标包含链接所在目录 → 判定成环不建", r4 is None, str(r4))
        check("成环时没留下链接", not (s4 / LINK_NAME).exists())

        # 同名真实文件：不动它
        write_settings(tmp, str(user_space))
        s5 = tmp / "workspace" / "session" / "s5"
        s5.mkdir(parents=True)
        (s5 / LINK_NAME).write_text("用户自己的文件", encoding="utf-8")
        r5 = ensure_my_space_link(s5)
        check("同名真实文件 → 跳过不覆盖", r5 is None and (s5 / LINK_NAME).read_text(encoding="utf-8") == "用户自己的文件")

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
