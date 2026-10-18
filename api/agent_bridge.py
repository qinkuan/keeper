"""扩展 ↔ 内核的桥接通道（WebSocket）。

分工
----
::

    content script（页面 UI，隔离世界）
        │ chrome.runtime.sendMessage
        ▼
    background / service worker（唯一持有 WS）
        │ ws://127.0.0.1:<port>/agent-bridge?token=...
        ▼
    本模块（校验 token/Origin → 交给 submit_extension_job → 结果推回去）

为什么是 WebSocket，而不是内核往页面推
------------------------------------
内核曾经试过两条路，都不行：

* ``expose_binding``：内核往页面挂一个函数。但 content script 跑在**隔离世界**，
  够不到主世界挂的函数。
* 内核 ``page.evaluate`` 往页面推：同样跨不了世界；更糟的是页面脚本能伪造
  ``postMessage``，等于让网页自己编造「内核返回的结果」。

WebSocket 由扩展的 service worker 自己发起，与页面世界无关，也没有可伪造的入口。

页面数据从哪来：**扩展自己取**
--------------------------------
扩展用 ``chrome.scripting`` 在用户正在看的那一页里就地取到文字/截图，随请求一起送来，
**内核全程不碰浏览器**。这是关键——内核的 Playwright 只接在 keeper 自带的那个
Chromium 上，用户装在自己 Chrome 里时它根本够不到那个进程；曾经那条「内核去浏览器里
找页面」的路，正是「抓到上一个页面 / 504 / 找不到可抓取页面」这一串症状的来源。

同步 / 异步边界
--------------
:func:`~keeper.browser.agent_browser.submit_extension_job` 在普通线程里跑（由
``asyncio.to_thread`` 调用），而 ``WebSocket.send`` 是 async 的、只能在事件循环线程里
await。两侧靠 :func:`publish`（同步，任意线程可调）→ ``loop.call_soon_threadsafe``
→ 事件循环里的常驻 task 发出去。**不要**在工作线程里直接 await。
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..api import auth as auth_mod

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-bridge"])


class _Hub:
    """全局连接中枢：事件循环、待发队列、在线连接。"""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.conns: List[WebSocket] = []
        self.drain: Optional[asyncio.Task] = None   # 全局唯一的发送 task

    def register(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.conns.append(ws)
        # 全局只起一个发送 task：每条连接各起一个的话，多个 task 竞争同一个 queue，
        # 消息会被随机某个 task 取走再广播——功能上碰巧没错，但纯属浪费，且一旦
        # 有人改成"只发给自己那条连接"就会立刻变成偶发丢消息。
        if self.drain is None or self.drain.done():
            self.drain = loop.create_task(_drain_loop())

    def unregister(self, ws: WebSocket) -> None:
        if ws in self.conns:
            self.conns.remove(ws)


_hub = _Hub()


def publish(payload: Dict[str, Any]) -> None:
    """内核 → 扩展 的主动推送。**同步接口，任意线程可调**（作业泵用它）。

    没有连接就丢弃：扩展还没接上时提交的任务本来也无处可回。
    """
    loop = _hub.loop
    if loop is None or not _hub.conns:
        logger.debug("没有扩展连接，丢弃推送：%s", payload.get("event"))
        return
    try:
        loop.call_soon_threadsafe(_hub.queue.put_nowait, payload)
    except RuntimeError:
        logger.debug("事件循环已关闭，丢弃推送", exc_info=True)


def _origin_ok(ws: WebSocket, ext_id: str) -> bool:
    """Origin 必须是本扩展，或者干脆没有。

    浏览器发起的 WS 一定带 ``Origin: chrome-extension://<id>``；网页脚本发起的带自己
    的 origin（拒掉）；裸 TCP 客户端（curl / node）不带 Origin —— 那类调用方本来
    就有本机文件访问能力，靠 token 兜住，所以放行「无 Origin」。
    """
    origin = (ws.headers.get("origin") or "").strip()
    if not origin:
        return True
    if not origin.startswith("chrome-extension://"):
        return False
    if not ext_id:
        # 没登记扩展 id（内核早期启动失败）时退化成「必须是某个扩展 origin」，
        # 仍然挡得住普通网页。
        return True
    return origin == f"chrome-extension://{ext_id}"


async def _drain_loop() -> None:
    """常驻 task：把队列里的消息发给所有在线连接，顺带清掉死连接。"""
    while True:
        payload = await _hub.queue.get()
        dead: List[WebSocket] = []
        for ws in list(_hub.conns):
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            _hub.unregister(ws)


async def _send(ws: WebSocket, payload: Dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 浮球对话（agent:ask / agent:sessions）
# ---------------------------------------------------------------------------
#
# 这两条与上面的「抓取」链路**完全分开**：抓取走 submit_extension_job，只整理
# 扩展自取的数据；这里要真的跑一轮 agent（几十秒），因此必须自己管超时、定向回包
# 和后台任务，不能塞进那条 to_thread 的路里。
#
# 三件事决定了它的形状：
#
# 1. **必须定向回，不能 publish**：publish 是广播给所有在线连接的，而结果属于
#    某一个标签页的浮球。用 _send(ws, ...) 直接发给发起那条连接。
# 2. **必须先 ack 再跑**：agent 一轮 30~120 秒。不先回一个 accepted，浮球就只能
#    干等超时，用户会以为按钮坏了。
# 3. **必须放后台任务**：主循环还要收 ping 和后续消息。挂在 await 上会把这条
#    连接彻底堵死。
#
# 不做流式：浮球只显示最新一条最终答案，SSE 那套（hub / delta / abort）在这里全是
# 复杂度换不来东西。


def _ball_prompt(q: str, page: Dict[str, Any]) -> str:
    """把「问了什么」+「在哪一页问的」拼成一条用户消息。

    为什么拼进 content 而不是新开一列
    ------------------------------
    这段文字会进 agent 的 prompt，所以「agent 知道这条来自浮球、来自哪一页」靠它
    就够了，不需要 schema 变更，也不会出现「某一层忘了读那个字段」的静默失效。

    代价是浏览器页里看到的就是这段原文。我认为这个代价划算：用户需要看到
    「agent 到底以为我在哪个页面上」，这句话本身就是有用的确认。

    没选区时不给选区块——**这里必须让用户看见"没带内容"**。内核的 Playwright 够不
    到用户 Chrome 那一页（见 :mod:`keeper.browser.agent_browser`），所以没带选区的
    提问里 agent 基本是盲的，它只会凭 URL 猜。留个空段等于骗用户 agent 看得到。
    """
    title = str(page.get("title") or "").strip()
    url = str(page.get("url") or "").strip()
    sel = str(page.get("selection") or "").strip()

    head = ""
    if title or url:
        where = f"页面「{title}」" if title else "当前页面"
        # 只给标题，不带 URL。
        #
        # URL 对 agent 基本没有信息量：搜索结果页、带会话参数的地址动辄几百字符，
        # 全是 rsv_bp / rsv_idx 这类与内容无关的参数，token 全花在解析噪音上；
        # 而它唯一真有用的场景（agent 想再打开这一页）内核本来也做不到——
        # Playwright 够不到用户 Chrome 的那一页（见 keeper.browser.agent_browser）。
        # 反过来它的代价是实打实的：这段原文用户看得见，一个撑满两屏的地址会把
        # "agent 以为我在哪一页"这个有用的确认彻底淹掉。
        head = f"我在{where}上提问。\n\n"

    parts = []
    if head:
        parts.append(head.rstrip("\n"))
    if sel:
        # 选区可能很长（全选整页）。截断写进消息里，好过让一个超长块挤掉真正的
        # 问题——顺序上问题在前，这里是补充材料。
        body = sel[:BALL_SELECTION_LIMIT]
        more = f"…（选区共 {len(sel)} 字，已截断）" if len(sel) > BALL_SELECTION_LIMIT else ""
        parts.append(f"页面选中内容：\n{body}{more}")
    # 「问题：」后面换行：q 里可能带着若干条引用（浮球那边会把它们标成无序
    # 列表项并各自成行）。标题与内容挤在同一行时，第一行读起来是「问题：- 模型…」，
    # 既难看也容易让 agent 把标题当成列表的一部分。
    parts.append(f"问题：\n{q}")
    return "\n\n".join(parts)


#: 选区送进 prompt 的字数上限。够覆盖「选中一段话/几段话来问」这个主场景，
#: 又不至于把问题本身挤出上下文。
BALL_SELECTION_LIMIT = 4000


async def _resolve_ball_target(agent_id: str, want_sid: str) -> str:
    """定下这一轮记到哪条会话：用户指定的那条，或浮球自己的默认会话。

    指定的那条查不到时**退回默认会话而不是报错**：浮球是随手点一下的地方，
    而「刚才选的那条会话被删了」是用户在另一个界面的操作导致的——在这里弹错等于
    让用户为别的界面的事负责。
    """
    from ..chat import service as chat_service

    if want_sid:
        svc = chat_service.ChatService(agent_id=agent_id, session_id=want_sid)
        try:
            await svc.verify_session()
            return want_sid
        except ValueError:
            logger.info("浮球指定的会话 %s 不可用，退回浮球默认会话", want_sid)
    return await chat_service.get_or_create_ball_session(agent_id)


async def _ball_latest(ws: WebSocket, msg: Dict[str, Any]) -> None:
    """补取某条会话的**最新一条回答**。浮球每次打开面板时问一次。

    为什么需要它
    ------------
    MV3 的 service worker 空闲约 30 秒就被 Chrome 杀掉，浮球那一轮可能要跑很久。
    SW 要是死了，content script 里那个 Promise 随端口一起消失，浮球只会显示
    「连接断了」——但内核侧那一轮**照跑不误**，而且结果已经落进会话了。

    于是这里提供一条与 SW 存活**无关**的补取路径：下次打开面板，问一次"那条会话
    最后说了什么"。浮球本来就只显示最新一条，所以这个形状和它的显示逻辑正好对上，
    不需要保存任何一份"上一次显示到哪"的状态（除了一条消息 id 用来判重）。

    还在跑时如实回 ``in_progress``，不假装有答案——浮球据此显示「正在思考…」并继续
    轮询，这样"跨过一次 SW 重启"的这一轮也能看着它走完。
    """
    from ..agent.keeper import resolve_core_agent_id
    from ..chat import service as chat_service

    agent_id = resolve_core_agent_id()
    want = str(msg.get("session_id") or "").strip()
    sid = want or (await chat_service.find_ball_session(agent_id) if agent_id else None)
    empty = {"session_id": sid or "", "text": "", "in_progress": False, "message_id": ""}
    if not agent_id or not sid:
        await _send(ws, {"event": "result", "id": msg.get("id"), "result": empty})
        return

    svc = chat_service.ChatService(agent_id=agent_id, session_id=sid)
    try:
        await svc.verify_session()
    except ValueError:
        # 用户在浏览器页把它删了。回空而不是报错：浮球接下来会重新拉列表并落到
        # 默认会话，这里报错的唯一效果是面板上冒一条看不懂的提示。
        await _send(ws, {"event": "result", "id": msg.get("id"), "result": empty})
        return

    # 直接查表取最后一条 assistant，不走 load_messages（理由见
    # chat/service.py::latest_ball_answer 的 docstring）。
    last = await chat_service.latest_ball_answer(sid)
    await _send(ws, {
        "event": "result",
        "id": msg.get("id"),
        "result": {
            "session_id": sid,
            "text": last["text"],
            "in_progress": last["in_progress"],
            "message_id": last["message_id"],
        },
    })


async def _ball_sessions(ws: WebSocket, msg: Dict[str, Any]) -> None:
    """列浏览器会话，供浮球选「这一轮记到哪条名下」。

    只回 id 与标题两个字段：浮球拿到 id 是为了路由，拿到标题是为了显示，别的都用
    不上。排序沿用后端的 ``updated_at DESC``（最近更新的在前），所以不必把时间戳
    也传过去——浮球自己不再排一次序。
    """
    from ..agent.keeper import resolve_core_agent_id
    from ..chat import service as chat_service

    agent_id = resolve_core_agent_id()
    if not agent_id:
        await _send(ws, {
            "event": "result", "id": msg.get("id"),
            "error": "内核还没有可用的智能体，先在 keeper 里装载一个。",
        })
        return
    items = await chat_service.ChatService(agent_id=agent_id).list_sessions(
        limit=50, kind=chat_service.SESSION_KIND_BROWSER
    )
    # 用 event:"result" 而不是自造一个 "sessions" 事件：这条查询**本身就是**一次请求的
    # 终点。扩展侧只需一条 result 通路（按 id 兑现 promise），中间态另外走 onNote。
    # 混用两套会给「哪个事件会兑现 promise」留下一个需要读者自己推导的约定。
    await _send(ws, {
        "event": "result",
        "id": msg.get("id"),
        "result": {
            "sessions": [
                {"id": s["id"], "title": s.get("title") or chat_service.DEFAULT_SESSION_TITLE}
                for s in items
            ],
            # 默认那条**单独给**，不放进条目里：浮球得知道"不选就发哪条"。如果让它靠
            # "列表第一条"来判断，用户在浏览器页新建一条会话后，浮球下一次就会静默往
            # 那条里追加——而用户从没表达过这个意图。
            "default_id": await chat_service.find_ball_session(agent_id),
        },
    })


async def _ball_ask(ws: WebSocket, msg: Dict[str, Any]) -> None:
    """浮球「对这一页提问」：立刻 ack，真正跑 agent 放后台任务。

    run_id 由**内核**生成而不是扩展给：这一路的 run 只需要被"服务端自己"和
    "浏览器页那个界面"看到，扩展全程只等最终一条结果，看不到过程也不需要 run_id。
    让内核生成省掉了「扩展没带 run_id 时怎么办」这个分支——那种情况下 run 无法
    登记，整轮会既不可见也不可停。
    """
    req_id = msg.get("id")
    q = str(msg.get("q") or "").strip()
    # 纯图片提问（q 为空但有附件）也允许：用户拖张图进来直接问"这是什么"。
    if not q and not (msg.get("attachments") or []):
        await _send(ws, {"event": "result", "id": req_id, "error": "问题不能为空"})
        return

    from ..agent.keeper import resolve_core_agent_id
    from ..chat import service as chat_service
    from ..store.models import new_id

    agent_id = resolve_core_agent_id()
    if not agent_id:
        await _send(ws, {
            "event": "result", "id": req_id,
            "error": "内核还没有可用的智能体，先在 keeper 里装载一个。",
        })
        return

    page = msg.get("page") if isinstance(msg.get("page"), dict) else {}
    # 「新对话」是浮球横条上显式的第一个选项，选它就必须**真的新建一条**。
    #
    # 关键：不能用「session_id 传空」来表达新建——空的语义在
    # :func:`_resolve_ball_target` 里是「用浮球默认会话」（见那里的回退分支）。
    # 于是点了「新对话」、发出去、却记进了上一条：用户选的是新会话，得到的是旧会话，
    # 而界面上没有任何迹象能让他发现这件事。所以新建必须由扩展显式说一遍。
    fresh = bool(msg.get("new_session"))
    try:
        sid = "" if fresh else await _resolve_ball_target(
            agent_id, str(msg.get("session_id") or "").strip()
        )
    except Exception as e:  # noqa: BLE001
        await _send(ws, {"event": "result", "id": req_id, "error": str(e)})
        return

    # 浮球附件（图片）：把 WS 内联的 base64 落成附件，交给 run_one_ask。
    # 走与 HTTP 上传端点同一套落库（_store_agent_attachment），复用厂商上传 +
    # 缓存逻辑，不另写。仅图片、体积上限 ~6MB（与前端 5MB 拦一道双保险）。
    ball_atts = []
    raw_atts = msg.get("attachments") or []
    if raw_atts:
        try:
            import base64

            from ..chat.api import _store_agent_attachment

            for a in raw_atts[:8]:
                a = a or {}
                du = a.get("data_url") or ""
                if not du.startswith("data:"):
                    continue
                try:
                    _, b64 = du.split(",", 1)
                    blob = base64.b64decode(b64)
                except Exception:
                    continue
                if len(blob) > 6 * 1024 * 1024:
                    continue
                try:
                    att = await _store_agent_attachment(
                        agent_id, sid, a.get("name") or "image", blob
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("浮球附件落库失败，跳过该张: %s", e)
                    continue
                ball_atts.append(
                    {"file_id": att["file_id"], "provider": att["provider"], "name": att["name"]}
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("浮球附件处理异常: %s", e)

    # 会话定下来之后才 ack：浮球显示的「已收到」顺便可以带上"记到哪条会话"，
    # 用户点回浏览器页就能直接找到它，不用自己猜。
    #
    # 新建时 sid 是空的（会话还没建），真实 id 要等 run_one_ask 里
    # ensure_session_id() 定下来 —— 那时由 on_session 回调补发一个 accepted。
    # 这里**绝不能**改发 result：result 是兑现 promise 的那一个（扩展那边
    # request() 收到它就 resolve 了），提前发等于把整条请求的回答吞掉。
    if sid:
        await _send(ws, {"event": "accepted", "id": req_id, "session_id": sid})
    content = _ball_prompt(q, page)

    async def run() -> None:
        # run_id 由**内核**生成：这一路的 run 只需要被"服务端自己"和"浏览器页那个
        # 界面"看到，扩展全程只等最终一条结果，看不到过程也不需要 run_id。让内核
        # 生成省掉了「扩展没带 run_id 时怎么办」这个分支——那种情况下 run 无法登记，
        # 整轮会既不可见也不可停。
        run_id = new_id()
        try:
            async def note_session(real_sid: str) -> None:
                """新建时补发 accepted：真实 id 只有 ``run_one_ask`` 里
                ``ensure_session_id()`` 定下来才知道，而扩展要靠它把这条新会话
                立刻加进横条——否则用户发完消息，横条上还是那一条旧的，
                看起来就像"新对话没生效"。"""
                await _send(ws, {"event": "accepted", "id": req_id, "session_id": real_sid})

            # 执行走的是和浏览器页**同一份** run_one_ask（见 chat/service.py）：
            # run 的登记、停止检查、事件缓冲、注销，全在那边。这里的差别只有两处——
            # 不推 SSE 队列（浮球只回一条最终结果），以及工作区取默认值。
            resp = await chat_service.run_one_ask(
                agent_id=agent_id,
                session_id=sid,
                text=content,
                attachments=ball_atts if ball_atts else None,
                run_id=run_id,
                kind=chat_service.SESSION_KIND_BROWSER,
                # 工作区：只在**新建**那一次生效（已有会话忽略），但每次都传是安全的
                # ——与 new_session 同一口径，省得浮球会话的工作区规则和别处不一致。
                user_space_id=await chat_service._default_space_id(),
                on_session=note_session if fresh else None,
                # 标题用**用户原始的问题**，而不是 content 里拼好的那段 prompt：
                # 后者开头是「我在页面「X」（URL）上提问。」，而 derive_session_title
                # 只取首行前 18 字符 —— 用它做标题的结果是每条浮球会话都长一个样，
                # 完全分辨不出。页面信息该进 prompt（agent 要看见），不该进标题
                # （用户要看见的是自己问了什么）。
                title_text=q,
            )
            if resp.get("error"):
                text = f"（这一轮没跑完：{resp['error']}）"
            elif resp.get("waiting_human"):
                text = resp.get("answer") or "这个问题需要你补充点信息才能继续。"
            else:
                text = resp.get("answer") or ""
            await _send(ws, {
                "event": "result", "id": req_id,
                # 新建那一轮 sid 本来是空的，取 run_one_ask 补出来的真实 id ——
                # 扩展靠它刷新横条、回填选中项。
                "result": {"kind": "text", "text": text,
                           "session_id": resp.get("session_id") or sid,
                           "url": page.get("url") or ""},
            })
        except Exception as e:  # noqa: BLE001
            # run_one_ask 内部已经把 hub 终态封成 error 了，这里不重复做。
            logger.warning("浮球提问失败: %s", e, exc_info=True)
            await _send(ws, {"event": "result", "id": req_id, "error": str(e)})

    asyncio.create_task(run())


@router.websocket("/agent-bridge")
async def agent_bridge(ws: WebSocket) -> None:
    """扩展接入端点。"""
    from ..browser.agent_browser import submit_extension_job

    # 先 accept 再关：直接抛异常的话客户端只看到 "WebSocket connection failed"，
    # 完全看不出是 token 不对。这里至少能带上一句原因。
    await ws.accept()

    ext_id = (ws.query_params.get("ext") or "").strip()
    if not _origin_ok(ws, ext_id):
        await _send(ws, {"event": "denied", "reason": "origin"})
        await ws.close(code=4403)
        logger.warning("拒绝扩展连接：Origin 不符（%s）", ws.headers.get("origin"))
        return

    token = (ws.query_params.get("token") or "").strip()
    expected = auth_mod.current_token()
    if expected and not (token and hmac.compare_digest(token, expected)):
        await _send(ws, {"event": "denied", "reason": "token"})
        await ws.close(code=4401)
        logger.warning("拒绝扩展连接：token 不符")
        return

    loop = asyncio.get_running_loop()
    _hub.register(ws, loop)
    logger.info("扩展已接入（Origin=%s）", ws.headers.get("origin") or "无")

    try:
        # 这里**不下发** config（地址 / token）。扩展的地址与 token 是用户在 keeper
        # 「浏览器」页复制连接码、填进扩展选项页存下来的 —— 内核不再用 Playwright 伸手
        # 进那个浏览器替它写（那条路只对 keeper 自带的 Chromium 成立，用户装在自己
        # Chrome 里时内核根本够不到那个 service worker）。
        #
        # 也因此这里**没有浏览器是否在跑的判断**：这条链路上内核全程不碰浏览器。
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            cmd = str(msg.get("cmd") or "")

            if cmd == "ping":
                await _send(ws, {"event": "pong"})
                continue
            if cmd == "shutdown":
                break

            # 浮球对话：自己管生命周期（先 ack、后台跑、定向回包）。
            # 必须排在下面的抓取之前 —— 抓取那条路是无脑把消息丢给 submit_extension_job，
            # 它只认 op=text/shot/eval，浮球消息会被当成一个看不懂的 op 然后报错。
            #
            # 每个分支都单独包一层 try：**处理器的异常会一路冒到下面的
            # ``except``，那里是关连接的**。也就是说任何一个分支出错，用户看到的
            # 都是「连接断了」，而不是「哪一步失败了」——他既看不到原因，也没法只
            # 重试这一条。而这里任何一个分支都有可能在某次调用里抛（模块 import、
            # 目标会话刚被删、agent 装配异常…），所以不能指望"代码没写错"。
            if cmd in ("agent:sessions", "agent:latest", "agent:ask"):
                handler = {
                    "agent:sessions": _ball_sessions,
                    "agent:latest": _ball_latest,
                    "agent:ask": _ball_ask,
                }[cmd]
                try:
                    await handler(ws, msg)
                except Exception as e:  # noqa: BLE001
                    # 记 warning 而不是 debug：这一层的异常**关掉了用户的连接**，
                    # 只记 debug 等于"发生了什么永远查不到"。
                    logger.warning("处理 %s 失败（已转为错误应答，连接保持）: %s",
                                   cmd, e, exc_info=True)
                    await _send(ws, {
                        "event": "result", "id": msg.get("id"),
                        "error": f"{cmd} 处理失败：{e}",
                    })
                continue

            try:
                # 内核不碰浏览器：结果由 submit_extension_job 直接 publish 回来
                await asyncio.to_thread(submit_extension_job, msg)
            except Exception as e:  # noqa: BLE001
                await _send(ws, {"event": "result", "id": msg.get("id"), "error": str(e)})
    except WebSocketDisconnect:
        logger.info("扩展断开")
    except Exception:  # noqa: BLE001
        # 到这里已经不是"某条消息处理失败"，而是连接本身出问题了（读帧、协议层）。
        # 仍然要记 warning —— 这类问题唯一的诊断线索就是这条日志。
        logger.warning("扩展连接异常（连接将被关闭）", exc_info=True)
    finally:
        _hub.unregister(ws)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def connected_count() -> int:
    """当前有多少条扩展连接在线。给诊断接口用。"""
    return len(_hub.conns)


def connected_ids() -> List[str]:
    """在线扩展的 id 列表（从连接时登记的 query 参数 ``ext`` 汇总）。

    服务 worker 可能连了不止一条（MV3 的SW 被杀后重连、或用户开了两个窗口），
    所以按 id 去重——界面上要给用户一个「连上了吗」的确切答案。
    """
    out: List[str] = []
    for ws in list(_hub.conns):
        ext = (ws.query_params.get("ext") or "").strip()
        if ext and ext not in out:
            out.append(ext)
    return out


@router.get("/agent-bridge/info")
async def agent_bridge_info(request: Request) -> Dict[str, Any]:
    """**把连接需要的东西一次性告诉用户**：内核地址、token、该装出来的扩展 id。

    为什么需要这个接口（而不是让内核继续用 Playwright 写配置）
    -------------------------------------------------------
    扩展装在**用户的日常 Chrome** 里之后，内核再也够不到它的 service worker
    （那是另一个进程、另一个 profile），于是变成"扩展要连内核得先有 token，而要拿到
    token 内核得先控制那个浏览器"的死结。打破它的办法只有一条：把 token 显式交给
    用户，由用户自己填进扩展。这个接口就是把那份显式信息摆到界面上。

    安全口径
    --------
    这里返回的就是能连内核的凭据本身，所以**必须过认证**（``auth.check``）：
    ``auth.py`` 开头那节说得明白，同源策略只拦读响应不拦发请求，任何被用户打开的
    网页都能盲打本地端口——一旦这个接口不校验 token，一个恶意页面就能 ``fetch``
    拿到 token，接着就能用扩展的通道把用户浏览器的页面内容读走。

    ``token`` 为空（命令行形态没配认证）时照常返回，此时内核本身也是裸奔的，
    由 :func:`auth.is_enabled` 告诉界面，可以给出更强的警告文案。
    """
    from ..browser import extensions as ext_mod

    auth_mod.check(request)

    # 强制用 127.0.0.1：扩展只能连本机，而前端可能是在局域网地址下打开的
    # （比如从另一台设备访问），照抄 Host 会让用户拿到一个连不上的地址。
    host = request.headers.get("host") or "127.0.0.1"
    port = host.rsplit(":", 1)[-1] if ":" in host else "80"
    kernel_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/agent-bridge"

    token = auth_mod.current_token() or ""
    ext_path = str(ext_mod.SYSTEM_EXTENSIONS_ROOT)
    ext_id = ext_mod.extension_id_for_path(ext_path)

    payload = {
        "kernel_url": kernel_url,
        "ws_url": ws_url,
        "token": token,
        "auth_enabled": auth_mod.is_enabled(),
        "extension_id": ext_id,
        "extension_path": ext_path,
        "connected": connected_count(),
        "connected_ids": connected_ids(),
    }

    # 连接码：把上面几项打成一串，让用户只复制一次。
    # 用 urlsafe base64（无 padding）——省掉 `=` 在复制粘贴里被吃掉/换行的老问题。
    raw = json.dumps(
        {"u": kernel_url, "w": ws_url, "t": token, "e": ext_id},
        ensure_ascii=False,
    ).encode("utf-8")
    payload["connect_code"] = "keeper1:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return payload
