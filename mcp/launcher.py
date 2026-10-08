"""MCP 接入（agent 功能）：按 mcp.json 标准注册表加载 / 连接多个 MCP server。

本文件只负责「标准 MCP 加载」，不含任何进程启动逻辑：
- mcp.json 是市面通用的 MCP 配置，每条 server 是标准格式：
    - 远程 http：{ "url": "http://host:port/mcp", "transport": "http" }
    - stdio：    { "command": "npx", "args": [...], "env": {...}, "transport": "stdio" }
- classify_server() 判别类型，connection_config() 转成 MultiServerMCPClient 连接配置，
  MCPManager 汇总全部 server；加百度网盘等标准 MCP 即插即用，互不拖垮。

这里也不管子进程怎么起停：stdio 型 server 由 MCP 客户端按 command 自动拉起。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def classify_server(cfg: Dict[str, Any]) -> str:
    """判断单条 server 配置的类型，返回 "http" / "stdio"。"""
    cfg = cfg or {}
    transport = (cfg.get("transport") or "").lower()
    if transport in ("http", "sse") or cfg.get("url"):
        return "http"
    if cfg.get("command"):
        return "stdio"
    return "stdio"


def connection_config(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把单条标准 server 配置转换为 MultiServerMCPClient 的连接配置。"""
    kind = classify_server(cfg)
    if kind == "http":
        return {"url": cfg.get("url"), "transport": (cfg.get("transport") or "http")}
    # stdio 型：交给 MultiServerMCPClient 自动拉起
    out: Dict[str, Any] = {
        "command": cfg.get("command"),
        "transport": "stdio",
        # args 必须始终带上：没参数时给空列表。不能写成 if cfg.get("args")——
        # 空列表是 falsy，会被丢掉，而客户端要求 stdio 必须有 args
        # （否则报 "args parameter is required for stdio connection"）。
        "args": cfg.get("args") or [],
    }
    if cfg.get("env"):
        out["env"] = cfg["env"]
    # cwd：平台下发的 mcp 代码已下载到本地，stdio 启动默认以该目录为根
    # （见 agent.config._load_mcp_servers）。langchain_mcp_adapters 会把它透传给
    # StdioServerParameters。
    if cfg.get("cwd"):
        out["cwd"] = cfg["cwd"]
    return out


class MCPManager:
    """按 mcp.json 标准注册表汇总多个 MCP server 的 MultiServerMCPClient 连接配置。

    只负责「连接配置」；启动 / 停止二进制不在本文件职责内。
    """

    def __init__(self, servers: Dict[str, Any]):
        self.servers = servers or {}

    def connection_config(self) -> Dict[str, Dict[str, Any]]:
        return {name: connection_config(name, cfg) for name, cfg in self.servers.items()}
