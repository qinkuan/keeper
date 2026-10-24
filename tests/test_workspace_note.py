"""环境说明注入的自测：直接跑 `python keeper/tests/test_workspace_note.py`。

覆盖「my-space 是用户的地盘，改动前先确认」这条提示的三个关键点：

1. **只在链接真实存在时注入**——我的空间没配 / 建链失败时不能提它，否则模型
   会去找一个不存在的东西（说假话比不说更糟）。
2. **位置正确**——在「工作准则」之后、「skill 额外要求」之前，且**不能**盖过
   末尾的「输出格式」（那是引擎契约）。
3. **措辞到位**——必须明确写出「写入/改名/删除前先确认」。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# tests/ → keeper/ → 项目根（keeper 包的父目录）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from keeper.agent.planner import ReActPlanner  # noqa: E402
from keeper.agent.process import _workspace_note  # noqa: E402
from keeper.chat.context import WorkspaceCtx, set_workspace  # noqa: E402
from keeper.zonefilesystem.session_link import LINK_NAME  # noqa: E402

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


def main() -> int:
    planner = ReActPlanner()

    print("\n=== 没有工作空间上下文 ===")
    set_workspace(None)
    check("无上下文 → 返回空", _workspace_note() == "")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        session = tmp / "session"
        session.mkdir(parents=True)

        print("\n=== 有会话空间，但没建链接 ===")
        ws = WorkspaceCtx(
            root=session, read_only=False, agent_space=session, user_space=None
        )
        set_workspace(ws)
        check("没链接 → 返回空", _workspace_note() == "", repr(_workspace_note()))

        print("\n=== 链接存在 ===")
        target = tmp / "my-space"
        target.mkdir()
        os.symlink(str(target), str(session / LINK_NAME))
        note = _workspace_note()
        check("有链接 → 有说明文字", bool(note), repr(note[:40]))
        check("提到了链接名", LINK_NAME in note)
        check("提到了会话空间", "会话空间" in note)
        check("写了改动前先确认", "确认" in note)
        check("写了删除也要确认", "删除" in note)

        print("\n=== 注入到 system prompt ===")
        sp = planner.system_prompt(
            tools_desc="(tools)",
            skill_desc="(skills)",
            agent_name="keeper",
            workspace_note=note,
        )
        i_rules = sp.find("# 工作准则")
        i_env = sp.find("## 当前环境")
        i_extra = sp.find("# 当前技能的额外要求")
        i_fmt = sp.find("# 输出格式")
        check("环境说明已注入", i_env > 0, sp[:200])
        check("在「工作准则」之后", i_rules > 0 and i_env > i_rules)
        check("在「输出格式」之前", i_fmt > 0 and i_env < i_fmt)
        # skill 额外要求缺失时，环境说明仍要在输出格式之前
        check(
            "没 skill 额外要求时也在输出格式之前",
            i_extra < 0 and i_env < i_fmt,
        )

        print("\n=== 不注入时不影响原有结构 ===")
        sp2 = planner.system_prompt(tools_desc="(tools)")
        check("没有「当前环境」节", "## 当前环境" not in sp2)
        check("输出格式仍在最后", sp2.rfind("# 输出格式") > sp2.rfind("# 工作准则"))

    set_workspace(None)
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
