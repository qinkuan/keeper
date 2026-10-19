"""入站认证：一个随机 token，要求请求带上它。

为什么必须有
------------
内核里有**能执行命令**的路径（``POST /agents/{id}/chat`` → agent 循环 → bash 工具 →
``subprocess``）。一旦本地 HTTP 接口裸奔，这条路径就是敞开的，而它有两个现实的打开方式：

1. **同网段的任何人**：服务端如果绑 ``0.0.0.0``，同一 WiFi 下的设备直接访问即可。
2. **你打开的任意网页**：同源策略（CORS）只拦"读响应"，**不拦"发请求"**。而且
   ``GET`` 属于简单请求，连预检都不触发 —— 也就是 CORS 配置得再严也没用。
   换句话说，一个恶意网页可以 ``fetch("http://127.0.0.1:<port>/agents/<id>/chat?q=...")``，
   请求会真的到达内核、真的执行，只是网页读不到结果。而我们要的从来不是读结果。

token 把这两条路都关上
---------------------
* 恶意网页**猜不到** token。它只能发不带自定义头的简单请求，而带自定义头的请求
  必须先过 CORS 预检 —— 预检由浏览器发出，网页 JS 读不到结果，也就无法把 token
  填进请求头。
* 同网段设备扫到端口也只会收到 401。

token 从哪来
------------
* **桌面形态**（``desktop/sidecar.js``）：每次启动现生成 32 字节随机值，经环境变量
  ``KEEPER_TOKEN`` 交给内核，同时由 preload 桥交给前端。用户不需要配置任何东西，
  也就不会不小心把它提交进仓库。
* **命令行形态**：在 ``config.yaml`` 里写 ``server.auth.token``，或设同名环境变量。
  两者都没有时**不启用认证**，但启动会打一条醒目的警告 —— 这种部署通常是有意为之
  （容器内网、CI），静默通过反而会让人误以为自己安全。

为什么不用 cookie / session
---------------------------
cookie 会被浏览器**自动附带**，那等于把 token 又送回给每个跨站请求，CSRF 立刻回来。
自定义头不会被浏览器自动带上，这正是我们要的性质。

关于 WebSocket
--------------
CORS 对 WebSocket **不生效**，所以将来加 ws 路由时必须显式校验：拿到
:func:`current_token` 自己比对 Origin 和 query 里的 token。HTTP 中间件压根不经过
WebSocket 握手，所以本模块不自动替 WS 兜底 —— 这个坑留给加 WS 的人写注释提醒。
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

#: 请求头名。刻意不用 Authorization —— 那是给"用户身份"用的，我们这只是本地进程间
#: 的入场券，用一个不会和任何反向代理规则混淆的名字更清楚。
TOKEN_HEADER = "X-Keeper-Token"

#: 查询参数里的同义入口。给无法自定义头的场景兜底（<img>、EventSource 之类）。
TOKEN_QUERY = "keepertoken"

_token: Optional[str] = None
_token_source: str = ""


def configure(token: Optional[str], source: str = "") -> None:
    """由内核启动流程调用一次，注入本次进程使用的 token。未传 = 不启用认证。"""
    global _token, _token_source
    t = (token or "").strip()
    _token = t or None
    _token_source = source if t else ""


def current_token() -> Optional[str]:
    """本次进程的 token；未启用认证时返回 None。"""
    return _token


def is_enabled() -> bool:
    return _token is not None


def describe() -> str:
    """给启动日志用的一句话摘要。**不要**把 token 本身打出来。"""
    if not _token:
        return "未启用（任何能访问该端口的进程都能调用全部接口）"
    return f"已启用（来源：{_token_source}）"


def _extract(request: Request) -> str:
    return (
        request.headers.get(TOKEN_HEADER)
        or request.query_params.get(TOKEN_QUERY)
        or ""
    ).strip()


def check(request: Request) -> None:
    """校验请求带的 token；不对就 401。未启用认证时直接放行。

    用 ``compare_digest`` 而不是 ``==``：后者按字符短路比较，理论上能逐字节试探。
    token 是本地进程间凭据、攻击面很小，但这个成本是零。
    """
    if not _token:
        return
    provided = _extract(request)
    if provided and hmac.compare_digest(provided, _token):
        return
    raise HTTPException(
        status_code=401,
        detail=(
            "缺少或无效的访问令牌。请通过桌面壳启动 keeper，"
            "或在 config.yaml 的 server.auth.token 里配置后用同名环境变量传入。"
        ),
    )


def resolve_token(cfg) -> tuple[Optional[str], str]:
    """决定本次进程用哪个 token，返回 ``(token, 来源说明)``。

    优先级：环境变量 ``KEEPER_TOKEN`` > ``config.yaml`` 的 ``server.auth.token``。
    环境变量优先，是因为桌面壳走的就是这条（每次启动随机生成，不落盘）。
    """
    env = (os.getenv("KEEPER_TOKEN") or "").strip()
    if env:
        return env, "环境变量 KEEPER_TOKEN"
    auth = getattr(cfg, "auth", None)
    if auth is not None:
        raw = (getattr(auth, "token", "") or "").strip()
        if raw:
            return raw, "config.yaml server.auth.token"
    return None, ""
