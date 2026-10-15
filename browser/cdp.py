"""桌面壳内置浏览器的抓取通道：用 Playwright **连接**桌面壳已经在跑的那个浏览器。

这条通道服务的是 **keeper 界面里那个内嵌浏览器**（Electron 原生子视图，见
``desktop/browser.js``），由接口层主动调用。与浮球那条链路
(:mod:`keeper.browser.agent_browser`) 互不相干 —— 那边数据由扩展自取，从不经过这里。

为什么不自己再开一个 Chromium
----------------------------
Chromium 的 user-data-dir 有单实例锁（``~/.keeper/chrome/userdata/SingletonLock``）。
同一个目录不能被两个 Chromium 进程同时打开：第二个要么去复用第一个的实例
（Playwright 的 launch 语义直接失败），要么并发写 Cookies / LevelDB 把登录态写坏。
所以想要"用真实浏览器访问 + 还能被程序操作"，只有一条路：**程序去连浏览器**。

（内核自己那个 Playwright 浏览器曾经就是"launch 出来的那一个"，它已随扩展取数改造
整块删除。所以现在的选择是二选一，没有第三条路：要么这个子视图，要么用户的 Chrome。）

连上之后拿到的是**同一个浏览器实例**：同一份 cookie 与登录态、用户能在窗口里看见
点击与跳转、``page.route`` 拦请求 / ``expect_response`` 等网络等待全都可用。而且用的是
Electron 自带的 Chromium，**不需要 ``playwright install``** 下载几百 MB 浏览器。

一个必须知道的限制：新标签页开不了
--------------------------------
``context.new_page()`` 在这里会抛 ``Target.createTarget: Not supported``——
Electron 关掉了这个 CDP 方法，因为窗口与视图由主进程管理，不允许调试协议凭空造
一个。所以本页所有抓取都是**作用于用户已经打开的那个页面**（或 keeper 自己那个被
排除的主界面）。要"程序自己开一个页面"，只能由主进程走 ``desktop/browser.js`` 的
``open(url)``，那是另一条链路，不在抓取模块的职责里。

线程模型
--------
Playwright 的 sync API 绑定创建它的线程，不能跨线程使用。而内核是 FastAPI async，
没有"只用一次就扔"的干净位置来跑它。因此这里用**单 worker 线程池**：连接在这个
worker 里建立，之后所有操作都 submit 回同一个线程——既满足线程亲和，又能让 async
接口用 ``run_in_executor`` 同步等待它。

没开桌面壳时（``./start_keeper.sh``）本模块直接不可用，接口回明确原因，而不是抛一堆
连接错误。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# CDP 端口由桌面壳（desktop/main.js）在启动内核时写进环境变量
CDP_PORT_ENV = "KEEPER_CDP_PORT"


class BrowserUnavailable(RuntimeError):
    """内置浏览器不可用：没开桌面壳 / 端口没监听 / playwright 没装。

    单独一个异常类型，是为了让接口能把它回成 503 并带上**可执行的**原因——
    "抓取失败"对用户毫无信息量，"当前是命令行形态，未开桌面壳"才有。
    """


def cdp_endpoint() -> Optional[str]:
    """CDP 地址；没有端口（命令行形态）或端口未监听时返回 None。"""
    port = (os.getenv(CDP_PORT_ENV) or "").strip()
    if not port:
        return None
    ep = f"http://127.0.0.1:{port}"
    # 桌面壳的调试端口可能比内核早，也可能晚（内核启动要几十秒）。这里探测一次，
    # 免得把"还没监听"报成"连接失败"
    try:
        with urllib.request.urlopen(f"{ep}/json/version", timeout=1):
            return ep
    except (urllib.error.URLError, OSError, ValueError):
        logger.debug("CDP 端口 %s 未响应（桌面壳可能还在启动）", port)
        return None


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


def _safe_title(page: Any) -> str:
    try:
        return page.title()
    except Exception:  # noqa: BLE001
        return ""


class CdpClient:
    """长连接 + 单 worker 线程池。对外只暴露"抓取"这类同步方法。"""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="keeper-cdp")
        self._browser: Any = None  # Playwright Browser，**只在 worker 线程里碰**
        self._pw: Any = None  # 同上：sync_playwright 上下文
        self._endpoint: Optional[str] = None

    # ---------- 连接 ----------

    def _connect_locked(self, endpoint: str) -> Any:
        """在 worker 线程里建立/复用连接。"""
        if self._browser is not None:
            return self._browser
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(endpoint)
        except Exception:
            pw.stop()
            raise
        self._pw = pw
        self._browser = browser
        self._endpoint = endpoint
        logger.info("已连上内置浏览器（CDP %s）", endpoint)
        return browser

    def _drop_locked(self) -> None:
        """丢弃坏连接。必须在 worker 线程里执行。"""
        browser, pw = self._browser, self._pw
        self._browser = None
        self._pw = None
        self._endpoint = None
        for obj, meth in ((browser, "close"), (pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass

    def _call(self, fn: Callable[[Any], Any], *, timeout: float = 30.0) -> Any:
        """把一段「用 browser 干活」的代码丢到 worker 线程执行，带连接自愈与重试。"""
        endpoint = cdp_endpoint()
        if not endpoint:
            raise BrowserUnavailable(
                "内置浏览器未就绪：当前不是桌面形态（命令行启动的内核没有 "
                f"{CDP_PORT_ENV}），或桌面壳仍在启动。抓取请用 desktop/start_desktop.sh 启动。"
            )
        if not _playwright_available():
            raise BrowserUnavailable(
                "缺少 playwright 依赖：在 keeper 的 .venv 里执行 pip install playwright"
            )

        def runner() -> Any:
            # 首次连接可能撞上"浏览器刚起来、CDP 还没完全就绪"，重试几次
            last: Optional[Exception] = None
            for _ in range(3):
                try:
                    return fn(self._connect_locked(endpoint))
                except BrowserUnavailable:
                    raise
                except Exception as e:  # noqa: BLE001
                    last = e
                    self._drop_locked()
            raise last  # type: ignore[misc]

        return self._pool.submit(runner).result(timeout=timeout)

    def shutdown(self) -> None:
        """进程退出时收干净。没连过就不碰线程池。"""
        if self._browser is not None:
            try:
                self._pool.submit(self._drop_locked).result(timeout=5)
            except Exception:  # noqa: BLE001
                logger.debug("断开 CDP 连接失败（忽略）", exc_info=True)
        self._pool.shutdown(wait=False)

    # ---------- 选页 ----------

    @staticmethod
    def _pages(browser: Any) -> List[Any]:
        """所有可操作 page。

        CDP 会把 keeper 主界面也列进来（它同样是这个 Chromium 里的一个 target），
        所以调用方必须显式排除——否则"抓当前页"会去抓 keeper 自己。
        """
        pages: List[Any] = []
        for ctx in browser.contexts:
            pages.extend(ctx.pages)
        return pages

    def _pick(
        self,
        browser: Any,
        url_contains: Optional[str],
        exclude_hosts: List[str],
    ) -> Any:
        cands = [
            p
            for p in self._pages(browser)
            if str(p.url).startswith(("http://", "https://"))
            and not any(h in p.url for h in exclude_hosts)
        ]
        if url_contains:
            hit = [p for p in cands if url_contains in p.url]
            if hit:
                return hit[-1]
            raise BrowserUnavailable(
                f"内置浏览器里没有匹配 {url_contains!r} 的页面（现有："
                + "、".join(str(p.url) for p in cands[:8])
                + "）"
            )
        if not cands:
            raise BrowserUnavailable("内置浏览器当前没有打开任何网页")
        # 不指定就取最后打开的那个：用户在地址栏敲的地址通常是最近一次
        return cands[-1]

    # ---------- 抓取 ----------

    def targets(self, exclude_hosts: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """当前内置浏览器打开的页面（给前端下拉选"抓哪个"用）。"""

        def run(browser: Any) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for p in self._pages(browser):
                u = str(p.url)
                if not u.startswith(("http://", "https://")):
                    continue
                if exclude_hosts and any(h in u for h in exclude_hosts):
                    continue
                out.append({"url": u, "title": _safe_title(p)})
            return out

        return self._call(run)

    def text(
        self,
        url_contains: Optional[str] = None,
        *,
        exclude_hosts: Optional[List[str]] = None,
        max_chars: int = 20000,
        selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        """抓页面文字。

        文字比截图**优先**：OCR / 视觉模型要花钱要时间，而大多数页面的信息在 DOM 里
        就是纯文本，这一路抓到的可以直接喂 LLM。
        """
        limit = max(200, min(int(max_chars), 200_000))

        def run(browser: Any) -> Dict[str, Any]:
            page = self._pick(browser, url_contains, exclude_hosts or [])
            # innerText 取渲染后的可见文本（script/style/隐藏元素都被排除），
            # 比 textContent 更接近"人眼看到的"。
            #
            # selector 必须用 **json 序列化**，不能用 Python 的 repr：repr(None)
            # 会把 None 原样写进 JS 源码，于是报
            # "ReferenceError: None is not defined" —— 与"选择器没匹配到"长得几乎
            # 一致，排查时极易被带偏（而 selector 缺省恰恰是最常见的调用）。
            js = (
                "(() => {"
                f" const sel = {json.dumps(selector)};"
                " const el = sel ? document.querySelector(sel) : null;"
                " return (el || document.body || document.documentElement).innerText;"
                "})()"
            )
            raw = page.evaluate(js)
            text = raw if isinstance(raw, str) else ""
            return {
                "url": str(page.url),
                "title": _safe_title(page),
                "text": text[:limit],
                "truncated": len(text) > limit,
                "full_chars": len(text),
            }

        return self._call(run)

    def screenshot(
        self,
        url_contains: Optional[str] = None,
        *,
        exclude_hosts: Optional[List[str]] = None,
        full_page: bool = True,
        image_format: str = "jpeg",
        quality: int = 70,
    ) -> Dict[str, Any]:
        """抓当前视口（或整页）截图，base64 返回。

        走 Playwright 的截图 = 走 CDP 合成器，**macOS 上不需要申请屏幕录制权限**，
        比 ``screencapture`` 省事得多。

        两个已知的天花板（平台级，不是实现问题）：
          * DRM / Widevine 保护的内容（Netflix、各家付费视频）会输出黑屏；
          * ``<video>`` 抓到的是**播放中那一帧**，不是完整视频。要连续抓帧得走
            ``Page.startScreencast``（视频流翻译那期做）。
        """
        fmt = "jpeg" if image_format.lower() in ("jpeg", "jpg") else "png"
        q = max(30, min(int(quality), 95))

        def run(browser: Any) -> Dict[str, Any]:
            page = self._pick(browser, url_contains, exclude_hosts or [])
            data = page.screenshot(full_page=bool(full_page), type=fmt, quality=q)
            return {
                "url": str(page.url),
                "title": _safe_title(page),
                "mime": f"image/{fmt}",
                "image_base64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data),
            }

        return self._call(run, timeout=60.0)


# 进程级单例：CDP 是长连接，重连代价高（会丢 target 状态）
_client: Optional[CdpClient] = None
_client_lock = threading.Lock()


def get_client() -> CdpClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = CdpClient()
        return _client
