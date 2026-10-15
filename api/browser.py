"""内置浏览器抓取接口：当前页面列表 / 抓文字 / 抓截图。

暴露给谁用：前端浏览器面板的「抓取」按钮用它验证链路，后面 agent 的工具也走同一组
接口——**抓取逻辑只有这一处**。人和 agent 各写一份的话，行为迟早不一致（一个抓
innerText、一个抓 textContent 这种差异，调试起来非常费时间）。

为什么用 ``def``（同步）路由而不是 ``async def``
----------------------------------------------
Playwright 的 sync API 绑定单一线程，不能在事件循环里直接用；而写成 ``async def``
却在里面直接调 Playwright，会卡住整个事件循环。普通 ``def`` 路由会被 FastAPI 丢到
线程池执行，等于免费拿到「在别的线程跑」这件事。所以这里是「同步路由 + 内部单
worker 线程池」，两层都不占着 loop。

不可用时回 503 而不是 500：没开桌面壳、或 playwright 没装，都是**部署形态问题**
而非代码错误。503 配一句「该怎么做」，前端能直接显示给用户；500 只会得到无用的
Internal Error。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..browser.cdp import BrowserUnavailable, cdp_endpoint, get_client

router = APIRouter(prefix="/browser", tags=["browser"])
logger = logging.getLogger(__name__)


def _keeper_hosts() -> List[str]:
    """keeper 主界面所在 host：CDP 里它也是一个 target，必须排除。

    否则「抓当前页」会去抓 keeper 自己的对话页——一个看起来能跑通、结果全是错的
    实现，比直接报错更难查。主界面走的是随机端口（sidecar.js 里现找的），所以除
    host 之外还得按端口排。
    """
    hosts = ["127.0.0.1", "localhost"]
    port = (os.getenv("PORT") or "").strip()
    return hosts + ([f":{port}"] if port else [])


def _unavailable(e: BrowserUnavailable) -> HTTPException:
    logger.info("内置浏览器不可用：%s", e)
    return HTTPException(status_code=503, detail=str(e))


class TextIn(BaseModel):
    """抓文字。默认抓当前打开的那个页面。"""

    url_contains: Optional[str] = Field(
        default=None,
        description="页面上要包含的 URL 片段；不填取最近打开的一个",
    )
    max_chars: int = Field(
        default=20000,
        ge=200,
        le=200_000,
        description="截断上限：超长页面全量回给前端会撑爆聊天上下文",
    )
    selector: Optional[str] = Field(
        default=None, description="只取某个容器（如 article、main）；不填取整页可见文本"
    )


class ScreenshotIn(BaseModel):
    """抓截图。"""

    url_contains: Optional[str] = Field(default=None, description="同上：URL 片段")
    full_page: bool = Field(default=True, description="整页还是只当前视口")
    image_format: str = Field(default="jpeg", description="jpeg | png")
    quality: int = Field(default=70, ge=30, le=95)


@router.get("/status")
async def status() -> dict:
    """内置浏览器是否可用（前端据此决定要不要显示抓取入口）。"""
    endpoint = cdp_endpoint()
    return {
        "available": bool(endpoint),
        "endpoint": endpoint,
        "reason": "" if endpoint else "当前不是桌面形态，或桌面壳仍在启动",
    }


@router.get("/targets")
async def list_targets() -> dict:
    """内置浏览器当前打开的页面列表。"""
    try:
        return {"pages": await asyncio.to_thread(get_client().targets, _keeper_hosts())}
    except BrowserUnavailable as e:
        raise _unavailable(e) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("列页面失败")
        raise HTTPException(status_code=500, detail=f"列页面失败：{e}") from e


@router.post("/text")
async def grab_text(body: TextIn) -> dict:
    """抓页面文字（直接可喂 LLM 的那种）。"""
    try:
        return await asyncio.to_thread(
            get_client().text,
            body.url_contains,
            exclude_hosts=_keeper_hosts(),
            max_chars=body.max_chars,
            selector=body.selector,
        )
    except BrowserUnavailable as e:
        raise _unavailable(e) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("抓文字失败")
        raise HTTPException(status_code=500, detail=f"抓文字失败：{e}") from e


@router.post("/screenshot")
async def grab_screenshot(body: ScreenshotIn) -> dict:
    """抓当前页面截图（base64）。"""
    try:
        return await asyncio.to_thread(
            get_client().screenshot,
            body.url_contains,
            exclude_hosts=_keeper_hosts(),
            full_page=body.full_page,
            image_format=body.image_format,
            quality=body.quality,
        )
    except BrowserUnavailable as e:
        raise _unavailable(e) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("抓截图失败")
        raise HTTPException(status_code=500, detail=f"抓截图失败：{e}") from e
