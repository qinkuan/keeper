"""P2 验证：子会话树 + 整合计费（独立脚本，无需 pytest）。

运行：python keeper/tests/test_sub_session_billing.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from keeper.store import (
    ChatSession,
    get_session_factory,
    init_db,
    db_url,
    new_id,
)
from keeper.chat import context as ctx_mod
from keeper.chat import service as svc_mod
from keeper.observability import recording
from keeper.observability import stats
from keeper.observability import budget
from keeper.observability import timeline


async def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    url = db_url(tmp)
    await init_db(url)

    factory = get_session_factory()

    # 1) 建表 / 迁移：parent_id 列存在
    async with factory() as db:
        cols = (await db.execute(text("PRAGMA table_info(chat_sessions)"))).fetchall()
        col_names = {r[1] for r in cols}
        assert "parent_id" in col_names, f"缺少 parent_id 列: {col_names}"

        # 2) 主会话 + 子 agent 子会话（parent_id 指向主会话）
        main_id, sub_id = new_id(), new_id()
        db.add(ChatSession(id=main_id, title="主任务", kind="task", agent_id="a1"))
        db.add(
            ChatSession(
                id=sub_id,
                title="子 agent: code",
                kind="delegatee",
                agent_id="delegatee:code",
                parent_id=main_id,
            )
        )
        await db.commit()

    # 3) override_session_id：切到子会话、退出还原
    ctx_mod.set_session_id(main_id)
    assert ctx_mod.current_session_id() == main_id
    async with ctx_mod.override_session_id(sub_id):
        assert ctx_mod.current_session_id() == sub_id
    assert ctx_mod.current_session_id() == main_id

    # 4) 子树聚合：主会话子树含子会话；子会话子树只有自己
    async with factory() as db:
        sids = await stats._session_subtree_ids(db, main_id)
        assert set(sids) == {main_id, sub_id}, sids
        sids2 = await stats._session_subtree_ids(db, sub_id)
        assert sids2 == [sub_id], sids2

    # 5) record_llm_call 在 override 下记到子会话
    ctx_mod.set_agent("a1", "core")
    msg_id = new_id()
    ctx_mod.set_message_id(msg_id)

    class FakeLLM:
        model_name = "gpt-x"
        provider = "openai"
        profile_id = None

    async with ctx_mod.override_session_id(sub_id):
        await recording.record_llm_call(
            FakeLLM(), prompt_tokens=100, completion_tokens=50, duration_ms=10
        )
    async with ctx_mod.override_session_id(main_id):
        await recording.record_llm_call(
            FakeLLM(), prompt_tokens=200, completion_tokens=80, duration_ms=20
        )

    # 6) usage_timeseries(主会话)：整合计费，应包含子会话的调用
    series = await stats.usage_timeseries(session_id=main_id)
    tp = sum(b["prompt_tokens"] for b in series["points"])
    tc = sum(b["completion_tokens"] for b in series["points"])
    assert tp == 300, tp  # 100(子) + 200(主)
    assert tc == 130, tc  # 50(子) + 80(主)

    # 7) 反向：按子会话查，只含自己的 100/50
    series_sub = await stats.usage_timeseries(session_id=sub_id)
    sp = sum(b["prompt_tokens"] for b in series_sub["points"])
    assert sp == 100, sp

    # 8) aggregate(主会话)：整合计费 + 主/子会话拆分
    agg = await budget.aggregate(session_id=main_id)
    assert agg["calls"] == 2, agg["calls"]
    assert agg["prompt_tokens"] == 300, agg["prompt_tokens"]  # 100(子) + 200(主)
    assert agg["completion_tokens"] == 130, agg["completion_tokens"]
    bd = {b["session_id"]: b for b in agg["session_breakdown"]}
    assert set(bd) == {main_id, sub_id}, bd.keys()
    assert bd[main_id]["prompt_tokens"] == 200 and not bd[main_id]["is_delegatee"]
    assert bd[sub_id]["prompt_tokens"] == 100 and bd[sub_id]["is_delegatee"]

    # 9) 子会话不进会话列表（list_sessions 按 kind != delegatee 过滤）
    async with factory() as db:
        from sqlalchemy import select as _sel

        shown = (
            await db.execute(
                _sel(ChatSession).where(
                    ChatSession.kind != svc_mod.SESSION_KIND_DELEGATEE
                )
            )
        ).scalars().all()
        shown_ids = {s.id for s in shown}
        assert sub_id not in shown_ids, "子会话不应出现在会话列表"
        assert main_id in shown_ids

    # 10) 详情时间线含子 agent 拆分（按 message_id 归并 delegatee 的调用）
    tl = await timeline.message_timeline(msg_id)
    assert tl["summary"]["calls"] == 2, tl["summary"]["calls"]  # 主 + 子 都在本轮
    sa = {s["session_id"]: s for s in (tl.get("subagents") or [])}
    assert sub_id in sa, sa.keys()
    assert sa[sub_id]["calls"] == 1 and sa[sub_id]["is_delegatee"]
    assert sa[sub_id]["prompt_tokens"] == 100

    print("P2 验证全部通过 ✅")
    print(f"  parent_id 列存在（建表/迁移）: ✅")
    print(f"  override_session_id 切换与还原: ✅")
    print(f"  子树聚合(主含子): ✅")
    print(f"  record_llm_call 子会话归属: ✅")
    print(f"  usage_timeseries 整合计费 token=300/130: ✅")


if __name__ == "__main__":
    asyncio.run(main())
