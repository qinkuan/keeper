"""浏览器能力 —— 现在只剩一份。

1. :mod:`keeper.browser.agent_browser` —— 浏览器**扩展**那条链路。扩展装在用户自己的
   Chrome 里，用 ``chrome.scripting`` 就地取页面内容，内核只整理数据。全程不碰浏览器。

2. :mod:`keeper.browser.cdp` —— **用户可见浏览器**（桌面壳的原生子视图 WebContentsView，
   嵌在 keeper 界面里）。能装进界面，但装不了扩展（Electron 不是 Chrome，扩展 API
   被官方移除）。

曾经还有第三份：Playwright 自带的 Chromium（有头独立窗口、可上网装扩展）。**已删** ——
它与 1 的冲突不是功能重复，而是**够不到**：Playwright 只接在它自己启动的那个进程上，
用户把扩展装进自己的 Chrome 之后，浮球经它取数就永远拿不到页面。连带的选页逻辑、
僵死自愈、看门狗、profile 锁处理一并移除。
"""

from .cdp import BrowserUnavailable, CdpClient, cdp_endpoint, get_client

__all__ = [
    # 用户可见浏览器（Electron 子视图，经 CDP 操作）
    "BrowserUnavailable",
    "CdpClient",
    "cdp_endpoint",
    "get_client",
]
