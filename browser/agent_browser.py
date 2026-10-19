"""浏览器扩展 ↔ 内核这条链路。

这个模块曾经是整个 Playwright 浏览器的家：启动一个自带 Chromium、管理 profile、
选标签页、在页面里求值、截图，再加上一整套僵死自愈。**现在全删了**，只留下浮球
这一段。

为什么删
--------
浮球的取数改成了**扩展自己动手**（``chrome.scripting`` 在用户正在看的那一页里读文字、
截图，随请求一起送来），内核只负责把数据整理成前端要的形状。于是内核不再需要"进入
一个浏览器进程"这个能力。

而"进入浏览器进程"正是那一长串毛病的唯一根源 —— 内核的 Playwright 只接在 keeper
自带的那个 Chromium 上，用户把扩展装进自己的 Chrome 之后它就永远够不到那个进程，
于是浮球点下去是「没匹配上任何标签页」，退而求其次去挑一个 ``chrome://`` 页求值，
又正好卡在"内核页面不执行注入脚本"上，45 秒不返回，最终报 504。以及「打开新页面后
第一次抓取抓到上一个页面」、「某个死锁标签页把整个浏览器拖死」—— 全是同一个源头。

现在用户的浏览需求由「自己的 Chrome + keeper 扩展」满足，agent 侧不再需要"进入一个
浏览器进程"这个能力，所以这整套 Playwright 代码连同那些自救补丁一并移除。

**它和 :mod:`keeper.browser.cdp` 是并行的两条路，不是替代关系。** 那一条服务的是
桌面壳的内置浏览器（Electron 子视图），由接口层主动调用；这一条服务的是浮球，数据由
扩展随请求一起送来。两者互不调用，也互不知道对方存在。

剩下的两个函数
--------------
* :func:`_answer_self_served` —— 把扩展传来的数据整理成前端面板要的形状
* :func:`submit_extension_job` —— 入口：整理完直接 ``publish`` 回去

**全程不碰浏览器、不入队、不占 runner 线程**。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 文本截断上限。扩展那边不截（它不知道用户要多少），截断统一在这里做。
_TEXT_LIMIT_DEFAULT = 20000
_TEXT_LIMIT_MAX = 200_000


def _answer_self_served(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """扩展已经用 ``chrome.scripting`` 取好了页面数据 → 直接组装结果。

    **全程不碰浏览器**：不选页、不求值、不进任何工作线程。这是「扩展装在用户自己的
    Chrome 里」这条路上最关键的一环 —— 内核的 Playwright 够不到那个进程，所以取数
    必须已经完成；到这一步只剩下"把数据整理成前端要的形状"。

    返回 ``None`` 表示"这条请求没带自取数据"，调用方据此报错。**不再退回内核去
    浏览器里找** —— 那条路够不到用户 Chrome，而它曾经的表现是静默抓错页面、或在
    ``chrome://`` 上求值卡死 45 秒最后报 504。

    字段与前端 ``BrowserPanel.tsx`` 的渲染代码一一对应（``r.text`` / ``r.full_chars`` /
    ``r.truncated`` / ``r.data_url`` / ``r.mime``）。少一个字段的表现是面板显示
    "undefined 字"，而不是报错，很难定位。
    """
    if msg.get("self_served") is not True:
        return None
    data = msg.get("page_data")
    if not isinstance(data, dict):
        return None
    op = str(msg.get("op") or "text")
    url = str(data.get("url") or msg.get("url") or "")

    if op == "shot":
        data_url = str(data.get("data_url") or "")
        if not data_url:
            return None          # 截图没取到 → 明确报错，不给半张图
        b64 = data_url.split(",", 1)[1] if "," in data_url else ""
        mime = "image/png" if data_url.startswith("data:image/png") else "image/jpeg"
        return {
            "kind": "image",
            "url": url,
            "title": str(data.get("title") or ""),
            "mime": mime,
            "image_base64": b64,
            "data_url": data_url,
            "bytes": (len(b64) * 3) // 4,
            # 扩展只能截当前可见的视口（chrome.tabs.captureVisibleTab 的硬限制），
            # 这里如实回报 false，不假装支持整页截图。
            "full_page": bool(data.get("full_page")),
            "source": "extension",
        }

    if op == "eval":
        return {
            "kind": "value",
            "value": data.get("value"),
            "url": url,
            "source": "extension",
        }

    if op != "text":
        return None

    limit = max(200, min(int(msg.get("max_chars") or _TEXT_LIMIT_DEFAULT),
                         _TEXT_LIMIT_MAX))
    raw = data.get("value")
    text = raw if isinstance(raw, str) else ""
    return {
        "kind": "text",
        "url": url,
        "title": str(data.get("title") or ""),
        "text": text[:limit],
        "truncated": len(text) > limit,
        "full_chars": len(text),
        "source": "extension",
    }


def submit_extension_job(msg: Dict[str, Any]) -> str:
    """扩展通过 WebSocket 提交抓取请求。**同步接口，工作线程里调用。**

    消息形状（由扩展的 ``background.js`` 发出）::

        {"id": "r3", "op": "text", "self_served": true, "page_data": {...}}

    没有 ``page_data`` 就是**明确失败**。这里不再有任何回退路径：曾经的去向是"让
    内核用 Playwright 去浏览器里找那一页"，而它够不到用户 Chrome，表现是挑一个相邻
    标签页静默抓它、或者在 ``chrome://`` 上卡死 45 秒最后 504。用户看到"抓错页"或
    "504"却不知道发生了什么，比明明白白一句"这个页面读不了"糟糕得多。
    """
    answered = _answer_self_served(msg)
    if answered is None:
        url = str(msg.get("url") or "")
        reason = (
            "浏览器内核页面（chrome://、扩展页、应用商店）无法注入脚本"
            if not url.startswith(("http://", "https://"))
            else "扩展没能取到页面内容"
        )
        raise RuntimeError(
            f"这个页面读不了（{reason}）。"
            "常见原因：Chrome 应用商店、PDF 阅读页、这个标签页正开着开发者工具(F12)，"
            "以及刚打开、扩展脚本还没注入进去的那一瞬间。"
        )

    from .agent_bridge import publish

    publish({"event": "result", "id": msg.get("id"), "result": answered})
    logger.info(
        "浮球自取数据，直接返回：op=%s %s",
        msg.get("op"), answered.get("full_chars", ""),
    )
    return "self-served"
