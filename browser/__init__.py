"""浏览器域 —— 抓取逻辑只有一份，桥接 / HTTP 接口也一并归在这里。

模块划分（按依赖方向，从底层到上层）
------------------------------------
1. :mod:`keeper.browser.cdp` —— **用户可见浏览器**（桌面壳原生子视图 WebContentsView，
   嵌在 keeper 界面里）的 CDP 客户端。能装进界面，但装不了扩展（Electron 不是 Chrome，
   扩展 API 被官方移除）。

2. :mod:`keeper.browser.scrape` —— 内置浏览器的 **HTTP 抓取接口**（``/browser/...``：
   status / targets / text / screenshot）。界面「抓取」按钮与 agent 工具共用同一组接口，
   底层走 1。仅桌面形态可用，不可用时回 503。

3. :mod:`keeper.browser.agent_browser` —— 浏览器**扩展**链路的任务处理：扩展装在用户
   自己的 Chrome 里，用 ``chrome.scripting`` 就地取页面内容，内核只整理数据、全程不碰
   浏览器。

4. :mod:`keeper.browser.agent_bridge` —— 扩展 ↔ 内核的 **WebSocket 桥**（``/agent-bridge``）。
   持有连接、校验 Origin/token，把抓取作业交给 3 的 ``submit_extension_job``、结果经
   ``publish`` 推回；浮球「对这一页提问」也走这里。与 3 是生产者/消费者关系，故同包。

这些 HTTP/WS 接口原先散落在 ``keeper/api/`` 下（``api/browser.py``、``api/agent_bridge.py``），
还会反向 import 本包——已迁到此处并改为包内相对导入，消除 api ↔ browser 的双向耦合。

另外，Playwright 自带的 Chromium（有头独立窗口）那份抓取**已删**：它与 3 的冲突不是
功能重复，而是**够不到**——Playwright 只接在自己启动的进程上，扩展装进用户 Chrome 后
经它取数永远拿不到页面。连带的选页逻辑、僵死自愈、看门狗、profile 锁处理一并移除。
"""

from .cdp import BrowserUnavailable, CdpClient, cdp_endpoint, get_client

__all__ = [
    # 用户可见浏览器（Electron 子视图，经 CDP 操作）
    "BrowserUnavailable",
    "CdpClient",
    "cdp_endpoint",
    "get_client",
]
