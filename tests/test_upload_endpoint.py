"""上传端点的端到端回归：`python keeper/tests/test_upload_endpoint.py`。

为什么要有这个脚本
----------------
上一个 bug（``cfg = _agent_llm_config(...)`` 漏了 ``await``）**单元测试全绿、
静态检查也过**，直到用户真点了一次上传才炸。原因是它只在真实 HTTP 路径上才
暴露：``async`` 函数不 await 返回的是协程对象，属性访问要到**下一行**才失败。

所以这里用 FastAPI TestClient 真打一次 ``POST /attachments``：厂商那一跳用替身
换掉（不花用户额度），但**路由、参数解析、supports 校验、缓存落盘、响应组装**
全走真实代码。

**隔离**：HOME 与数据库都是临时的。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="keeper-upload-ep-"))
os.environ["HOME"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def main() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from keeper.chat import api as capi
    from keeper.chat.service import ChatService
    from keeper.llm import files as F
    from keeper.store import init_db, get_session_factory
    from keeper.store.models import ChatSession, SessionMessage

    # ── 替身：agent / 模型配置 / 厂商上传，全换成可控的 ──────────────────
    class _FakeAgent:
        """够 resolve_workspace 用的最小 agent（它只读 agent_id / workspace_root）。"""
        agent_id = "core"
        workspace_root = _TMP

    capi._agent = lambda agent_id: _FakeAgent()  # type: ignore[assignment]

    async def fake_cfg(agent_id):
        from keeper.llm.config import LLMConfig
        return LLMConfig(
            model_name="deepseek-flash", provider="deepseek",
            api_key="sk-test", base_url="https://api.deepseek.com",
        )

    capi._agent_llm_config = fake_cfg  # type: ignore[assignment]

    class _Files:
        async def create(self, **kw):
            return type("F", (), {
                "id": "file-api-xyz789", "filename": "shot.png",
                "bytes": len(kw["file"][1]), "object": "file", "expires_at": None,
            })()

    class _Client:
        def __init__(self, **kw):
            self.files = _Files()

    F.AsyncOpenAI = _Client  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(capi.router)
    # 不用绕鉴权：auth_deps 是 main.py 挂上去的（app.include_router(...,
    # dependencies=auth_deps)），router 本身不带——直接 include 就行。

    async def setup():
        await init_db(f"sqlite+aiosqlite:///{_TMP / 't.db'}")
        # 预建一条会话，让 @tmp/ 有确定的归属目录
        svc = ChatService(agent_id="core")
        sid = await svc.ensure_session_id()
        return sid

    sid = asyncio.run(setup())

    with TestClient(app) as client:
        # ── ① 正常上传 ──────────────────────────────────────────────
        r = client.post(
            f"/agents/core/sessions/{sid}/attachments",
            files={"file": ("shot.png", PNG, "image/png")},
        )
        check(r.status_code == 200, "上传返回 200", r.status_code)
        body = r.json()
        att = (body or {}).get("attachment") or {}
        check(att.get("file_id") == "file-api-xyz789", "拿到厂商 file_id", att.get("file_id"))
        check(att.get("provider") == "deepseek", "记录 provider", att.get("provider"))
        check(att.get("model") == "deepseek-flash", "记录 model", att.get("model"))
        check(att.get("mime") == "image/png", "mime 由内容判定", att.get("mime"))
        check(bool(att.get("cache_path", "").startswith("@tmp/uploads/")),
              "缓存路径是 @tmp/ 相对路径", att.get("cache_path"))
        check(body.get("session_id") == sid, "响应带回 session_id", body.get("session_id"))

        # 缓存真的落盘了（在会话私有临时区）
        from keeper.agent.keeper import resolve_core_agent_id  # noqa: F401
        from keeper.chat.service import resolve_workspace
        from keeper.tools.builtin.sandbox import resolve_path

        async def peek():
            ws = await resolve_workspace(_FakeAgent(), sid)
            return resolve_path(ws.root, ws.agent_space, att["cache_path"])

        cached = asyncio.run(peek())
        check(cached.is_file() and cached.read_bytes() == PNG,
              "缓存落在会话临时区且内容一致", cached)

        # ── ② 预览端点：读本地缓存（**不依赖消息**——发送前也要能预览）──────
        r2 = client.get(f"/agents/core/files/attachment/{sid}/{att['id']}")
        check(r2.status_code == 200 and r2.content == PNG,
              "预览端点读到缓存原件（未挂消息也能预览）", (r2.status_code, len(r2.content)))

        r3 = client.get(f"/agents/core/files/attachment/{sid}/{att['id']}?dl=1")
        check(r3.status_code == 200 and b"attachment" in r3.headers.get("content-disposition", "").lower().encode(),
              "dl=1 时作为附件下载", r3.headers.get("content-disposition"))

        r4 = client.get(f"/agents/core/files/attachment/{sid}/不存在的id")
        check(r4.status_code == 404, "附件 id 不存在 → 404", r4.status_code)

        r4b = client.get(f"/agents/core/files/attachment/别的会话/{att['id']}")
        check(r4b.status_code == 404, "会话不对 → 404（拿不到别人的附件）", r4b.status_code)

        # ── ③ 模型不支持：上传前就拒，且不打厂商 ─────────────────────
        async def bad_cfg(agent_id):
            from keeper.llm.config import LLMConfig
            return LLMConfig(model_name="deepseek-chat", provider="deepseek", api_key="sk-x")

        capi._agent_llm_config = bad_cfg  # type: ignore[assignment]
        calls = {"n": 0}

        class _Count:
            async def create(self, **kw):
                calls["n"] += 1
                return type("F", (), {"id": "x", "filename": "x", "bytes": 1,
                                      "object": "file", "expires_at": None})()

        class _CountClient:
            def __init__(self, **kw):
                self.files = _Count()

        F.AsyncOpenAI = _CountClient  # type: ignore[assignment]
        r5 = client.post(
            f"/agents/core/sessions/{sid}/attachments",
            files={"file": ("shot.png", PNG, "image/png")},
        )
        check(r5.status_code == 400, "模型不支持 → 400", r5.status_code)
        check("deepseek-flash" in (r5.json().get("detail") or ""),
              "拒绝原因提到 flash", r5.json().get("detail"))
        check(calls["n"] == 0, "拒绝时没有打厂商", calls["n"])

        # ── ④ 坏类型：本地就拦 ─────────────────────────────────────
        capi._agent_llm_config = fake_cfg  # type: ignore[assignment]
        r6 = client.post(
            f"/agents/core/sessions/{sid}/attachments",
            files={"file": ("evil.png", b"MZ\x90\x00" + b"x" * 40, "image/png")},
        )
        check(r6.status_code == 400 and "JPEG" in (r6.json().get("detail") or ""),
              "非图片被拦（提示支持的格式）", r6.json().get("detail"))

    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
