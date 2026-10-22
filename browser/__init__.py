"""浏览器域 —— 桥接 / 扩展链路归在这里。

模块划分（按依赖方向，从底层到上层）
------------------------------------
1. :mod:`keeper.browser.agent_browser` —— 浏览器**扩展**链路的任务处理：扩展装在用户
   自己的 Chrome 里，用 ``chrome.scripting`` 就地取页面内容，内核只整理数据、全程不碰
   浏览器。

2. :mod:`keeper.browser.agent_bridge` —— 扩展 ↔ 内核的 **WebSocket 桥**（``/agent-bridge``）。
   持有连接、校验 Origin/token，把抓取作业交给 1 的 ``submit_extension_job``、结果经
   ``publish`` 推回；浮球「对这一页提问」也走这里。与 1 是生产者/消费者关系，故同包。

这些 WS 接口原先散落在 ``keeper/api/`` 下（``api/agent_bridge.py``），还会反向 import
本包——已迁到此处并改为包内相对导入，消除 api ↔ browser 的双向耦合。
"""
