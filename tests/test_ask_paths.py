"""两条入口等价性回归：`python keeper/tests/test_ask_paths.py`。

被测的东西
----------
"在浏览器页里发一条消息" 和 "在浏览器浮球里发一条消息"，除了**结果怎么送出去**
（SSE 逐步推 vs WebSocket 回一条最终结果）之外，服务端应该走**同一份**编排。
那份编排在 :func:`keeper.chat.service.run_one_ask` 里。

为什么要有这个脚本
------------------
这份编排曾经被写了两遍（``chat/api.py`` 一份、``api/agent_bridge.py`` 一份），
而且已经因此出过事：浮球那侧把 ``chat.hub.finish`` 的导入名写错，异常在所有
``try`` 之外冒到接收循环，把用户的 WebSocket 关掉了——而 SSE 那侧安然无恙。
**两份代码只有一份会坏，是最坏的组合**：它不报错，只是那条路不通。

更糟的是 ``pyflakes`` 查不出 ``from X import Y`` 里的 Y 不存在（它只管"导入
了没用到"），静态检查全绿、运行时才炸。所以这里必须**真的把两个入口各跑一遍**。

覆盖
----
1. :func:`run_one_ask` 自己的生命周期（对着临时 SQLite 真落库）：
   事件缓冲 / 终态封存 / run 登记与注销 / 停止标记清理 / 异常路径。
2. 两个 HTTP 入口是否都汇到同一个函数（打桩后比对入参）。

**隔离**：``HOME`` 指到临时目录、数据库是临时 SQLite——绝不碰真实的 ``~/.keeper``。

**为什么不打真实 LLM**：本脚本要验的是"两条路子的**编排**一样"，不是"agent 答得
对不对"。真调 LLM 会让这个脚本变成慢且不稳定的集成测试；所以只把最内层的
``ChatService.ask`` 换掉，**它之外的每一环都是真的**（会话落库、run 注册表、
hub 事件缓冲、SSE 帧、WS 应答帧）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须在导入 keeper 之前设好 HOME：模块里多处用 Path.home() 定位 ~/.keeper
_TMP = Path(tempfile.mkdtemp(prefix="keeper-askpaths-"))
os.environ["HOME"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.chat import hub, runs  # noqa: E402
from keeper.chat import service as S  # noqa: E402
from keeper.store import get_session_factory, init_db  # noqa: E402
from keeper.store.models import ChatSession, SessionMessage  # noqa: E402

DB_URL = f"sqlite+aiosqlite:///{_TMP / 'askpaths.db'}"
AGENT_ID = "01TESTAGENT"

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


# ── 替身：只换掉最内层的 ask ─────────────────────────────────────────────────
class _Step:
    """够 ``run_one_ask`` 读属性用的假 ReAct 步骤。"""

    def __init__(self, seq: int) -> None:
        self.thought = f"想一下第 {seq} 步"
        self.tool = None
        self.args = None
        self.observation = None
        self.kind = None


ASK_CALLS: list = []


async def fake_ask(
    self,
    text,
    step_id=None,
    message_id=None,
    user_space_id=None,
    should_stop=None,
    on_step=None,
    on_delta=None,
    attachments=None,
):
    """替身 ask：真落库、真发回调，只是不调 LLM。"""
    # 真 ask 的第一件事就是确保会话存在并写回 self.session_id——替身必须照做，
    # 否则"run_id 为空"那条降级路（不预先 ensure_session_id）会拿到 None。
    await self._ensure_session()
    ASK_CALLS.append(
        {
            "session_id": self.session_id,
            "kind": self.session_kind,
            "text": text,
            "user_space_id": user_space_id,
            "has_should_stop": should_stop is not None,
            "attachments": attachments,
        }
    )
    async with get_session_factory()() as db:
        db.add(
            SessionMessage(
                chat_session_id=self.session_id, seq=1, role="user", content=text
            )
        )
        db.add(
            SessionMessage(
                chat_session_id=self.session_id,
                seq=2,
                role="assistant",
                content=f"答：{text}",
            )
        )
        await db.commit()
        # 真 ask 是由 _append_message 顺带 _auto_title 的；替身要覆盖标题行为，
        # 就必须把这一步也照做（否则下面关于标题的断言全是假的）。
        await self._auto_title(db, self.session_id, text)
    if on_step is not None:
        await on_step(1, _Step(1))
    if on_delta is not None:
        await on_delta("答：")
    return {
        "answer": f"答：{text}",
        "waiting_human": False,
        "session_id": self.session_id,
    }


async def boom_ask(self, text, **kw):
    raise RuntimeError("这一轮炸了")


async def _rows(session_id: str) -> list:
    from sqlalchemy import select

    async with get_session_factory()() as db:
        r = await db.execute(
            select(SessionMessage)
            .where(SessionMessage.chat_session_id == session_id)
            .order_by(SessionMessage.seq)
        )
        return [(m.seq, m.role, m.content) for m in r.scalars().all()]


# ── 1. run_one_ask 自身的生命周期 ───────────────────────────────────────────
async def part_lifecycle() -> None:
    await init_db(DB_URL)
    S.ChatService.ask = fake_ask          # type: ignore[method-assign]
    S.resolve_for_chat = lambda agent_id=None: object()  # type: ignore[assignment]

    # 两个入口**唯一**的差别就是这组参数：kind 决定会话归到哪一类列表里，
    # run_id 一个由前端给、一个由内核生成。下面用它们各自的真实取值各跑一遍。
    ASK_CALLS.clear()
    res_chat = await S.run_one_ask(
        agent_id=AGENT_ID,
        session_id="",
        text="同一个问题",
        run_id="RUN-SSE-0001",
        kind=S.SESSION_KIND_CHAT,
        step_id="",
        user_space_id="",
    )
    res_ball = await S.run_one_ask(
        agent_id=AGENT_ID,
        session_id="",
        text="我在页面「X」（http://x）上提问。\n\n同一个问题",
        run_id="RUN-WS-0001",
        kind=S.SESSION_KIND_BROWSER,
        step_id=None,
        user_space_id="SP1",
        # 浮球传的是**用户原始的问题**：title_text 存在与否，决定会话标题
        # 是"用户问了什么"还是"这个 prompt 的头 18 个字符"。
        title_text="同一个问题",
    )

    check(res_chat["answer"] == "答：同一个问题", "SSE 那一路拿到回答", res_chat)
    # 浮球那路的正文里带着页面上下文（_ball_prompt 拼的），这是**故意**的：
    # 用户在浏览器页要能看见"agent 到底以为我在哪个页面上"。
    check(
        res_ball["answer"].startswith("答：我在页面「X」")
        and res_ball["answer"].endswith("同一个问题"),
        "浮球那一路拿到回答（含页面上下文）",
        res_ball,
    )

    # 落库一致：两轮都是「一条 user + 一条 assistant」，形状完全一样
    rows_chat = await _rows(res_chat["session_id"])
    rows_ball = await _rows(res_ball["session_id"])
    check(
        [r[:2] for r in rows_chat] == [(1, "user"), (2, "assistant")],
        "SSE 那一路的消息形状",
        rows_chat,
    )
    check(
        [r[:2] for r in rows_ball] == [(1, "user"), (2, "assistant")],
        "浮球那一路的消息形状",
        rows_ball,
    )
    check(
        res_chat["session_id"] != res_ball["session_id"],
        "两条路各自建自己的会话（没串到一起）",
    )

    # 会话类别按各自入口落库（这是**故意**不同，不是 bug）
    async with get_session_factory()() as db:
        row_chat = await db.get(ChatSession, res_chat["session_id"])
        row_ball = await db.get(ChatSession, res_ball["session_id"])
    check(row_chat.kind == S.SESSION_KIND_CHAT, "浏览器页那条会话 kind=chat", row_chat.kind)
    check(row_ball.kind == S.SESSION_KIND_BROWSER, "浮球那条会话 kind=browser", row_ball.kind)

    # ── 会话标题 ──
    # 浮球那条路的正文被 _ball_prompt 拼成了「我在页面「X」（URL）上提问。\n\n…」，
    # 而 derive_session_title 只取首行前 18 字符。若不传 title_text，每条浮球会话
    # 的标题都会被裁成同一个样子（真实数据里出现过两条一字不差的会话），自动标题
    # 本该帮用户分辨会话，反而彻底失效。
    #
    # 注意 title_text 只在**新建**时生效（_auto_title 放过用户已改过的标题），
    # 这正是我们想要的：不该半夜把用户起好的名字改回去。
    check(row_chat.title == "同一个问题", "未传 title_text 时标题照旧从正文派生", row_chat.title)
    check(row_ball.title == "同一个问题", "浮球那条路的标题来自 title_text（原始提问）", row_ball.title)
    check(
        "http" not in (row_ball.title or "") and "页面" not in (row_ball.title or ""),
        "浮球会话的标题里不带 URL / 页面前缀",
        row_ball.title,
    )

    # 传给 ask 的入参口径一致
    check(len(ASK_CALLS) == 2, "两次都真的调到了 ask", ASK_CALLS)
    check(
        all(c["has_should_stop"] for c in ASK_CALLS),
        "两条路都带上了 should_stop（停止生成对浮球同样有效）",
        ASK_CALLS,
    )

    # hub 事件缓冲：两条路都得有，浏览器页靠它"刷新后接回来"
    for run_id, label in (("RUN-SSE-0001", "SSE"), ("RUN-WS-0001", "浮球")):
        snap = hub.snapshot(run_id)
        check(snap is not None, f"{label}：hub 有事件缓冲", snap)
        if snap:
            check(
                snap.get("finished") is True and snap.get("done") is not None,
                f"{label}：hub 终态已封存（finished + done）",
                (snap.get("finished"), snap.get("done")),
            )
            check(len(snap.get("steps") or []) == 1, f"{label}：hub 收到 1 个 step", snap.get("steps"))
            # 正文在 hub 里存的是**累积字符串**（text），不是逐条 delta——
            # 那样不会随 token 数把内存撑爆（见 hub.py 的 _MAX_TEXT_CHARS）。
            check(snap.get("text") == "答：", f"{label}：hub 收到正文增量", snap.get("text"))

    # run 注销：两条路都得干净退出，否则会话会永远显示"正在思考"
    check(
        runs.active_run_id(res_chat["session_id"]) is None,
        "SSE 那一轮跑完已注销",
        runs.active_run_id(res_chat["session_id"]),
    )
    check(
        runs.active_run_id(res_ball["session_id"]) is None,
        "浮球那一轮跑完已注销",
        runs.active_run_id(res_ball["session_id"]),
    )
    check(
        not runs.is_stopped("RUN-SSE-0001") and not runs.is_stopped("RUN-WS-0001"),
        "停止标记已清理",
    )

    # 异常路径：hub 终态必须是 error（否则等这一轮的人永远等不到 done），
    # 且异常要**向外抛**——两种入口的报错通道不同，由调用方决定怎么告诉用户。
    S.ChatService.ask = boom_ask  # type: ignore[method-assign]
    raised = ""
    try:
        await S.run_one_ask(
            agent_id=AGENT_ID,
            session_id=res_chat["session_id"],
            text="会炸的一轮",
            run_id="RUN-ERR-0001",
            kind=S.SESSION_KIND_CHAT,
        )
    except Exception as e:  # noqa: BLE001
        raised = str(e)
    check(raised == "这一轮炸了", "异常向外抛给调用方", raised)
    snap = hub.snapshot("RUN-ERR-0001") or {}
    check(
        snap.get("finished") is True and snap.get("error") == "这一轮炸了",
        "异常时 hub 终态已封存（finished + error）",
        (snap.get("finished"), snap.get("error")),
    )
    check(
        runs.active_run_id(res_chat["session_id"]) is None,
        "异常退出也注销了 run（页面不会留下永久的「正在思考」）",
        runs.active_run_id(res_chat["session_id"]),
    )
    check(not runs.is_stopped("RUN-ERR-0001"), "异常退出也清了停止标记")

    # run_id 为空：降级跑完、不碰任何注册表，但**会话 id 照样要通知调用方**
    S.ChatService.ask = fake_ask  # type: ignore[method-assign]
    seen_sessions: list = []

    async def grab(sid):
        seen_sessions.append(sid)

    r = await S.run_one_ask(
        agent_id=AGENT_ID,
        session_id="",
        text="无 run 的一轮",
        run_id="",
        on_session=grab,
    )
    check(r["answer"] == "答：无 run 的一轮", "run_id 为空时照跑", r)
    check(await _rows(r["session_id"]), "run_id 为空时仍然落库")
    # 这条是回归：降级路径一度直接 return，把 on_session 漏掉了。症状不是报错，
    # 而是"新会话发完第一句就刷新 → 找不到那条会话、历史整片消失"——只在 run_id
    # 为空时发作，而 run_id 由前端可选传入，所以很难在正常点击路径上撞到。
    check(
        seen_sessions == [r["session_id"]],
        "run_id 为空时照样回调 on_session（session 帧不能丢）",
        seen_sessions,
    )
    check(
        runs.active_run_id(r["session_id"]) is None,
        "run_id 为空时不往注册表里写任何东西",
        runs.active_run_id(r["session_id"]),
    )


# ── 2. 两个入口是否汇到同一个函数 ───────────────────────────────────────────
# ── 3. 附件：run_one_ask → ask 的透传，与附件 → content part 的转换 ───────────
async def part_attachments() -> None:
    """附件链路的两个断点，都不需要真连厂商：

    1. ``run_one_ask`` 必须把 attachments 原样透传给 ``ask``——这一层断了，
       前端上传成功的文件就根本到不了执行层，而且**没有任何报错**（只是模型看不到图）。
    2. ``_attachment_parts`` 按 provider 转 content block：认得的 provider 转成
       厂商格式，认不出的跳过（换模型后的旧附件属于这种，不该让整轮问题失败）。
    """
    S.ChatService.ask = fake_ask  # type: ignore[method-assign]
    S.resolve_for_chat = lambda agent_id=None: object()  # type: ignore[assignment]
    ASK_CALLS.clear()

    atts = [
        {"id": "01A", "name": "a.png", "mime": "image/png", "size": 100,
         "provider": "deepseek", "model": "deepseek-flash",
         "file_id": "file-api-aaa", "cache_path": "@tmp/uploads/01A/a.png"},
        {"id": "01B", "name": "b.png", "mime": "image/png", "size": 200,
         "provider": "deepseek", "model": "deepseek-flash",
         "file_id": "file-api-bbb", "cache_path": "@tmp/uploads/01B/b.png"},
    ]

    # ① 透传：run_one_ask 的 attachments 必须到达 ask
    await S.run_one_ask(
        agent_id=AGENT_ID, session_id="", text="这两张图有什么不同",
        run_id="RUN-ATT-0001", attachments=atts,
    )
    check(
        ASK_CALLS and ASK_CALLS[-1]["attachments"] == atts,
        "attachments 从 run_one_ask 透传到 ask",
        ASK_CALLS[-1]["attachments"] if ASK_CALLS else None,
    )
    # 不传时是 None 而不是 []：两条路径都要走通，别让"没传"变成"传了空列表"
    # 而在某处被误判成"有附件但为空"
    ASK_CALLS.clear()
    await S.run_one_ask(
        agent_id=AGENT_ID, session_id="", text="没问题的一轮",
        run_id="RUN-ATT-0002",
    )
    check(
        ASK_CALLS and ASK_CALLS[-1]["attachments"] is None,
        "不传 attachments 时落到 ask 的是 None（不是空列表）",
        ASK_CALLS[-1]["attachments"] if ASK_CALLS else "无调用",
    )

    # ② 附件 → content part
    parts = S.ChatService._attachment_parts(atts)
    check(
        parts == [
            {"type": "file", "file_id": "file-api-aaa"},
            {"type": "file", "file_id": "file-api-bbb"},
        ],
        "deepseek 附件转成扁平的 file block",
        parts,
    )
    # 顺序即传入顺序：模型看到图片的先后要与前端展示一致，别重排
    mixed = atts + [
        {"id": "01C", "name": "c.png", "provider": "openai",
         "file_id": "file-ccc", "mime": "image/png", "size": 1},
    ]
    parts2 = S.ChatService._attachment_parts(mixed)
    check(
        len(parts2) == 2 and parts2[0]["file_id"] == "file-api-aaa",
        "认不出的 provider 跳过该条，其余保持顺序",
        parts2,
    )
    check(
        S.ChatService._attachment_parts(None) == []
        and S.ChatService._attachment_parts([]) == []
        and S.ChatService._attachment_parts([{"provider": "deepseek"}]) == [],
        "None / 空 / 缺 file_id 都不产生 part",
    )


def part_routes() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from keeper.api.agent_bridge import router as bridge_router
    from keeper.chat import api as capi
    from keeper.agent import keeper as akeeper

    # 静态断言：chat/api.py 拿到的必须是 service 里那**同一个**函数对象。
    # 这一条挡的是"哪天又有人 copy 了一份过去"。
    check(
        capi.run_one_ask is S.run_one_ask,
        "chat/api.py 用的就是 service.run_one_ask 本尊（不是副本）",
    )

    seen: list = []

    async def recorder(**kw):
        seen.append(kw)
        # 真 run_one_ask 会在会话 id 定下来时回调 on_session；替身不照做的话，
        # SSE 那条路的 session 帧就没了——而那正是"新会话发完第一句立刻刷新"
        # 不丢历史所依赖的那一帧。
        cb = kw.get("on_session")
        if cb is not None:
            await cb("SESS-REC-0001")
        return {"answer": f"答：{kw['text']}", "waiting_human": False}

    # 两个绑定都要换：chat/api.py 是直接 import 的（早绑定），
    # agent_bridge 走 chat_service.run_one_ask（晚绑定，换 service 上的即可）。
    capi.run_one_ask = recorder  # type: ignore[assignment]
    S.run_one_ask = recorder  # type: ignore[assignment]
    S._default_space_id = lambda: _async_val("SP1")  # type: ignore[assignment]
    capi.resolve_for_chat = lambda agent_id: object()  # type: ignore[assignment]
    akeeper.resolve_core_agent_id = lambda: AGENT_ID  # type: ignore[assignment]

    import keeper.api.agent_bridge as bridge

    async def fake_target(agent_id: str, session_id: str) -> str:
        return "SESS-BALL-0001"

    bridge._resolve_ball_target = fake_target  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(capi.router)
    app.include_router(bridge_router)

    q = "同一个问题"

    with TestClient(app) as client:
        r = client.post(
            f"/agents/{AGENT_ID}/chat/stream",
            json={"q": q, "run_id": "RUN-SSE-0002", "kind": "chat"},
        )
        sse_text = r.text
        check(r.status_code == 200, "SSE 入口 200", r.status_code)
        check("event: session" in sse_text, "SSE 推了 session 帧（前端要立刻记 id）", sse_text[:200])
        check("event: done" in sse_text, "SSE 推了 done 帧", sse_text[:200])

        # 回归：客户端没带 run_id 时（run_id 在前端是可选参数），SSE **仍然**必须
        # 推 session 帧。少了它，"新会话发完第一句就刷新"会找不到那条会话。
        r2 = client.post(
            f"/agents/{AGENT_ID}/chat/stream", json={"q": "不带 run 的一轮"}
        )
        check(
            "event: session" in r2.text and "event: done" in r2.text,
            "run_id 为空时 SSE 照样推 session + done 帧",
            r2.text[:200],
        )

        with client.websocket_connect("/agent-bridge") as ws:
            ws.send_json({"cmd": "agent:ask", "id": "m1", "q": q, "page": {}})
            # 收帧要**按内容停**，不能按固定条数：多等一条永远不来的帧时
            # receive_json 会把整个脚本挂死（这坑踩过一次）。
            frames = []
            for _ in range(6):
                f = ws.receive_json()
                frames.append(f)
                if f.get("event") == "result":
                    break
            ws.send_json({"cmd": "shutdown"})

    ev = [f.get("event") for f in frames]
    check("accepted" in ev, "浮球先回 accepted（带 session_id）", frames)
    res_frame = next((f for f in frames if f.get("event") == "result"), None)
    check(res_frame is not None, "浮球回了 result", frames)
    check(
        bool(res_frame) and str(res_frame.get("result", {}).get("text", "")).startswith("答："),
        "result 帧带回了回答（扩展要靠它上屏）",
        res_frame,
    )
    check(
        bool(res_frame)
        and res_frame.get("result", {}).get("session_id") == "SESS-BALL-0001",
        "result 帧带回 session_id（用户点回浏览器页能直接找到这条）",
        res_frame,
    )

    check(len(seen) == 3, "三次调用（SSE 两次 + 浮球一次）都打到同一个函数", [c.get("run_id") for c in seen])
    sse_call = next((c for c in seen if c.get("run_id") == "RUN-SSE-0002"), None)
    # 浮球那路的 run_id 由**内核**生成（new_id），不会是这两个固定值；
    # 「run_id 为空」那一路必须单独排除，否则会被它冒认。
    ws_call = next(
        (c for c in seen if c.get("run_id") not in ("RUN-SSE-0002", "", None)), None
    )
    check(sse_call is not None, "SSE 那一路的入参", seen)
    check(ws_call is not None, "浮球那一路的入参", seen)
    if sse_call and ws_call:
        check(sse_call["agent_id"] == ws_call["agent_id"] == AGENT_ID, "两边 agent_id 一致")
        check(q in sse_call["text"], "SSE 那一路原样把问题交给执行层", sse_call["text"])
        check(q in ws_call["text"], "浮球那一路也把问题交给执行层（另带页面上下文）", ws_call["text"])
        check(sse_call["kind"] == "chat", "SSE 那一路 kind=chat", sse_call["kind"])
        check(ws_call["kind"] == "browser", "浮球那一路 kind=browser", ws_call["kind"])
        check(
            ws_call["user_space_id"] == "SP1" and sse_call["user_space_id"] == "",
            "工作区口径各自按入口来（空串 = 服务端默认）",
            (sse_call["user_space_id"], ws_call["user_space_id"]),
        )
        check(
            sse_call["on_step"] is not None and sse_call["on_delta"] is not None,
            "SSE 那一路带了自己的推送回调",
        )
        # 注意用 .get()：浮球那侧**根本不传**这两个参数（不传才是"我不推 SSE"的
        # 表达方式），recorder 是 **kw 收集的，键压根不存在。
        check(
            ws_call.get("on_step") is None and ws_call.get("on_delta") is None,
            "浮球那一路不带推送回调（只回一条最终结果）",
            sorted(ws_call),
        )


async def _async_val(v):
    return v


def main() -> None:
    asyncio.run(part_lifecycle())
    asyncio.run(part_attachments())
    part_routes()
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
