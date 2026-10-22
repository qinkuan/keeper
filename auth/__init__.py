"""入站认证：一个随机 token，要求请求带上它。

从 ``keeper.api.auth`` 迁来——它是**跨切**的基础设施（``main.py`` 的 ``auth_deps``、
扩展桥的 WS 校验都要用），不属于任何单一业务域，所以独立成包。HTTP 依赖与 WS 校验
都从这里取 ``check`` / ``current_token`` / ``is_enabled`` / ``configure``。

为什么必须有 token：内核里有能执行命令的路径（chat → agent 循环 → bash 工具 →
subprocess）。本地 HTTP 一旦裸奔，同网段设备、或用户打开的任意网页（同源策略只拦
读响应、不拦发请求，GET 简单请求连预检都不触发）都能驱动它。token 把这两条路都关上。

为什么不用 cookie：cookie 会被浏览器自动附带给每个跨站请求，CSRF 立刻回来；自定义
本模块不自动替 WS 兜底。
"""
