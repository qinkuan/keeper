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

            try:
                # 内核不碰浏览器：结果由 submit_extension_job 直接 publish 回来
                await asyncio.to_thread(submit_extension_job, msg)
            except Exception as e:  # noqa: BLE001
                await _send(ws, {"event": "result", "id": msg.get("id"), "error": str(e)})
    except WebSocketDisconnect:
        logger.info("扩展断开")
    except Exception:  # noqa: BLE001
        logger.debug("扩展连接异常", exc_info=True)
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
