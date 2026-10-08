"""平台（keeperplatform）对接：拉取 agent 配置 + 落成本地库。

两个模块的分工：

- ``client``：HTTP 拉取（``fetch_my_agents``）——本地运行时的**上游**；
- ``sync``：把拉到的配置写进本地库（``sync_agent``）——本地运行时的**配置缓存**。

原来这两个文件是 ``keeper/platform_client.py`` / ``keeper/platform_sync.py``，
搬进本包是为了让「与平台有关的代码」集中在一处。调用方统一从
``keeper.plat`` 导入，内部再拆文件不影响外部。
"""

from .client import fetch_my_agents, platform_settings
from .sync import sync_agent

__all__ = ["fetch_my_agents", "platform_settings", "sync_agent"]
