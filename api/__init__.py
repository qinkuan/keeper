"""keeper 自带的运行时管理接口。

与平台的分工：mcp / skill / tool / agent 的**定义与增删改都在平台**，
keeper 只负责运行。因此这里只保留「把平台上的配置装载成本地实例 / 卸载」：

- `/manage/agents/{id}/load`   装载（从平台拉配置 → 落本地库 → 装配）
- `/manage/agents/{id}/unload` 卸载（释放实例，配置缓存保留）

会话与对话接口在 `keeper.chat.api`（挂在 /agents/{agent_id} 下）。
"""
