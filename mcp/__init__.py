"""MCP 子包：按 mcp.json 标准注册表连接多个 MCP server，支持 http / stdio 两种标准类型。

- client.py  ：MCP 会话与工具调用；
- launcher.py：把 server 配置转成连接参数。

这里不认识任何具体 server（不知道什么是 codebase），语义封装应放在各自的
tool / skill 里。
"""
from .client import DEFAULT_SERVERS, MCPSession, MCPError
from .launcher import MCPManager, classify_server, connection_config

__all__ = [
    "MCPSession", "MCPError", "DEFAULT_SERVERS",
    "MCPManager", "classify_server", "connection_config",
]
