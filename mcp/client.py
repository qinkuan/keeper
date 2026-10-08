"""MCP 接入（通用）：基于 langchain-mcp-adapters 的 MultiServerMCPClient 连接多个 MCP server。

- 只负责通用能力：连接 / 握手、工具加载、按名调用工具、优雅关闭。
- servers 是「名称 -> 连接配置」字典，天然支持多 MCP（再挂百度网盘只是多写一项）。
- 不绑定任何具体 server 的语义：某类 server 怎么用是 tool / skill 的事，
  这里只做与具体 server 无关的通用加载与调用（见 ``launcher``）。
- langchain-mcp-adapters 惰性依赖：缺失时由调用方降级。
"""
from __future__ import annotations

import ast
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """MCP 连接（进程启动 / 握手 / 调用）失败。"""


# 不内置任何具体 server：连哪些、怎么连，全部由 mcp.json 决定（见 launcher）。
DEFAULT_SERVERS: Dict[str, Dict[str, Any]] = {}


def _caused_by_oserror(e: BaseException) -> bool:
    """异常链里有没有 ``OSError``（含被 wrap 过一层的情况）。

    子进程起不来时底层抛的是 ``OSError``（errno 8 Exec format error = 二进制
    架构不符、errno 2 = 文件没了、errno 13 = 没 +x），但 LangChain 那层会把它
    包进自己的异常类型，所以不能只看最外层——得顺着 ``__cause__/__context__`` 找。
    """
    cur: Optional[BaseException] = e
    while cur is not None:
        if isinstance(cur, OSError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class MCPSession:
    """封装 MultiServerMCPClient 会话，支持多 MCP server、可扩展。

    用法：
        mcp = MCPSession(servers={"my_server": {"command": "my-server",
                                               "args": ["serve"], "transport": "stdio"}})
        await mcp.start()                 # 自动拉起进程 + 建立连接 + 加载工具
        tools = mcp.get_tools()           # LangChain Tool 列表
        ...
        await mcp.stop()                  # 关闭会话、终止子进程
    """

    def __init__(
        self,
        servers: Optional[Dict[str, Dict[str, Any]]] = None,
        primary: str = "",
    ):
        raw = servers if servers is not None else DEFAULT_SERVERS
        # keeper_protocol 是给 keeper 自己看的标记：先记下来，再从连接配置里摘掉。
        # 留着会被底层当未知参数拒绝（实测直接 MCPError）。
        self.keeper_peers: Dict[str, bool] = {}
        self._all: Dict[str, Dict[str, Any]] = {}      # 配置里的全部 server
        self.servers: Dict[str, Dict[str, Any]] = {}    # 已连上的
        self._missing: Dict[str, Dict[str, Any]] = {}   # 连不上的，留待重连
        for name, cfg in (raw or {}).items():
            c = dict(cfg)
            if c.pop("keeper_protocol", None):
                self.keeper_peers[name] = True
            self._all[name] = c

        # 没配任何 server 时取空串，不要在这里抛 StopIteration——
        # 空 MCP 是合法配置（agent 靠 LLM 也能跑）
        self.primary = primary if primary in self._all else next(iter(self._all), "")
        # 逐 server 各建一个 client：LangChain 的 Tool 对象不带「来自哪个 server」
        # 的信息（metadata / tags 均为 None），只有分开取才知道归属
        self.clients: Dict[str, Any] = {}
        self.tools: List[Any] = []
        self._tool_map: Dict[str, Any] = {}  # 全名 server__tool -> Tool
        self._by_name: Dict[str, List[Any]] = {}  # 原始名 -> Tool 列表（兼容旧调用）

    # ---- 生命周期 ----
    async def start(self) -> MCPSession:
        """逐个 server 建连接并加载工具。

        不用一次性 MultiServerMCPClient(servers)：那样拿到的 Tool 分不清属于谁，
        而几个 keeper 都暴露 send 是很常见的。逐 server 取天然知道归属，顺手把
        归属固化进工具全名（server__tool），调用方就不会串台。
        """
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as e:
            raise MCPError(f"langchain-mcp-adapters 未安装，无法连接 MCP server: {e}")

        working: Dict[str, Dict[str, Any]] = {}
        prefixed: Dict[str, Any] = {}
        by_name: Dict[str, List[Any]] = {}

        for name, cfg in (self._all or {}).items():
            client = None
            logger.info(
                "连接 MCP server %s：command=%s transport=%s cwd=%s",
                name,
                cfg.get("command"),
                cfg.get("transport", "stdio"),
                cfg.get("cwd"),
            )
            try:
                client = MultiServerMCPClient({name: cfg})
                tools = await client.get_tools()
            except Exception as e:
                # 命令本身的错（架构不符 Exec format error / 文件没了 / 没 +x）
                # 重试多少次都不会好，而且每次重试都要等一遍超时。这里分两类：
                # OSError 视为永久失败，不进重试队列；其余仍按老逻辑稍后重试。
                permanent = _caused_by_oserror(e)
                logger.warning(
                    "MCP server [%s] 连接失败%s: %s（type=%s）",
                    name,
                    "，属永久失败（命令无法执行），不再重试" if permanent else "，稍后重试",
                    e, type(e).__name__,
                )
                if client is not None:
                    await self._safe_close_client(client)
                if not permanent:
                    self._missing[name] = cfg
                continue
            logger.info("MCP server [%s] 连接成功，工具数=%d", name, len(tools or []))
            working[name] = cfg
            self.clients[name] = client
            for t in tools or []:
                tname = getattr(t, "name", None) or ""
                if not tname:
                    continue
                key = self.prefixed(name, tname)
                if key in prefixed:
                    logger.warning(
                        "工具 %s 重复登记（server %s），后者覆盖前者", key, name
                    )
                prefixed[key] = t
                by_name.setdefault(tname, []).append(t)

        if not working:
            raise MCPError(
                "所有 MCP server 均连接失败（检查各 server 的 command/url/launch 配置）"
            )

        self.servers = working  # 收窄到可用的
        self.tools = list(prefixed.values())
        self._tool_map = prefixed
        self._by_name = by_name
        if self.primary not in self.servers:
            self.primary = next(iter(self.servers), self.primary)
        logger.info(
            "MCP 会话已建立，servers=%s，工具=%d 个（全名 server__tool）",
            list(self.servers),
            len(prefixed),
        )
        return self

    @staticmethod
    def prefixed(server: str, tool: str) -> str:
        """工具全名。多个 keeper 都有 send 时靠它区分归属。"""
        return f"{server}__{tool}"

    @staticmethod
    async def _safe_close_client(client) -> None:
        aexit = getattr(client, "__aexit__", None)
        if aexit is not None:
            try:
                await aexit(None, None, None)
            except Exception:
                pass

    async def _safe_close(self) -> None:
        for client in self.clients.values():
            await self._safe_close_client(client)
        self.clients = {}
        self.tools = []
        self._tool_map = {}
        self._by_name = {}

    async def stop(self) -> None:
        await self._safe_close()

    # ---- 工具暴露（供 LangGraph / worker 使用）----
    def get_tools(self) -> List[Any]:
        """返回全部 LangChain Tool（已自动路由到各自 server）。"""
        return self.tools

    def get_named_tools(self) -> List[tuple]:
        """返回 ``(全名, Tool)`` 列表，全名形如 ``server__tool``。

        注册到 ToolRegistry 时要用全名：Tool 对象自己的 name 不含来源，多个
        server 暴露同名工具（几个 keeper 都有 send）时后注册的会把先注册的顶掉。
        """
        return list(self._tool_map.items())

    def get_tool(self, name: str) -> Any:
        """按名取工具。

        优先匹配全名 ``server__tool``；退一步也接受原始名——有的调用方只有原始名。
        同名工具分布在多个 server 时，
        原始名有歧义，这里明确报错要求改用全名，不静默挑一个。
        """
        if name in self._tool_map:
            return self._tool_map[name]

        candidates = self._by_name.get(name) or []
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            owners = ", ".join(
                self._owner_of(t) for t in candidates
            )
            raise MCPError(
                f"工具 '{name}' 在多个 server 上都存在（{owners}），"
                f"请用 'server__{name}' 形式指定"
            )
        raise MCPError(f"工具 '{name}' 不存在，可用: {sorted(self._tool_map)}")

    def _owner_of(self, tool: Any) -> str:
        """反查某 Tool 属于哪个 server（用于报错提示）。"""
        for name, t in self._tool_map.items():
            if t is tool:
                return name.split("__", 1)[0]
        return "?"

    def list_tool_names(self) -> List[str]:
        return sorted(self._tool_map)

    def tools_by_server(self) -> Dict[str, List[Dict[str, str]]]:
        """按 server 分组返回工具清单（原始名 + 描述），供配置面板展示。

        键为 server 名，值为该 server 下工具列表；每个工具含 ``name``（原始名，
        不含 server 前缀）与 ``description``。仅包含已连上、成功登记进
        ``_tool_map`` 的工具；连不上的 server 不出现在这里。
        """
        out: Dict[str, List[Dict[str, str]]] = {}
        for full, tool in self._tool_map.items():
            server, _, action = full.partition("__")
            if not action:
                continue
            out.setdefault(server, []).append(
                {
                    "name": action,
                    "description": getattr(tool, "description", "") or action,
                }
            )
        return out

    def is_keeper(self, name: str) -> bool:
        """该 server 是否按 keeper 协议实现（配置里标了 keeper_protocol）。

        决定调用走哪条路：
        - True  → 走 PeerService：有 send / get_thread，要落 threads + agent_messages，
                  可能挂起等对方的人，失败要能补拉
        - False → 普通 MCP 工具：输入输出一次了结，react_steps 记账就够
        """
        return bool(self.keeper_peers.get(name))

    def keeper_names(self) -> List[str]:
        """按 keeper 协议实现的 server 名单。"""
        return sorted(self.keeper_peers)

    def missing_names(self) -> List[str]:
        """还没连上的 server 名单（多半是对端还没部署起来）。"""
        return sorted(self._missing)

    async def reconnect_missing(self) -> List[str]:
        """重试之前连不上的 server，返回本次新连上的名字。

        对端常常比本实例晚起来——部署顺序不可控，不该要求谁先谁后。
        连上后工具进 ``_tool_map``，下次提问时 ``_collect_tools()`` 会把它们
        连同其它来源一起装配给 agent，agent 那侧不用改任何东西。
        """
        if not self._missing:
            return []
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            return []

        connected: List[str] = []
        for name, cfg in list(self._missing.items()):
            client = None
            try:
                client = MultiServerMCPClient({name: cfg})
                tools = await client.get_tools()
            except Exception as e:
                logger.debug("MCP server [%s] 仍未就绪: %s", name, e)
                if client is not None:
                    await self._safe_close_client(client)
                continue

            self.servers[name] = cfg
            self.clients[name] = client
            for t in tools or []:
                tname = getattr(t, "name", None) or ""
                if not tname:
                    continue
                self._tool_map[self.prefixed(name, tname)] = t
                self._by_name.setdefault(tname, []).append(t)
            del self._missing[name]
            connected.append(name)

        if connected:
            self.tools = list(self._tool_map.values())
            logger.info("MCP 对端重连成功: %s", connected)
        return connected

    async def reconnect_server(self, name: str) -> bool:
        """重建某个**已连过**的 server 的连接（它对端中途重启了），成功返回 True。

        和 reconnect_missing 的区别：这个 server 之前是连上的，工具已经注册进
        了 _tool_map，所以重点是**替换** Tool 对象——新连接产生新的 Tool，
        旧的握着已经断开的会话，留着只会继续失败。
        """
        if name not in self._all:
            return False
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            return False

        cfg = self._all[name]

        # 先丢掉旧连接，别让它占着
        old = self.clients.pop(name, None)
        if old is not None:
            await self._safe_close_client(old)

        try:
            client = MultiServerMCPClient({name: cfg})
            tools = await client.get_tools()
        except Exception as e:
            logger.warning("MCP server [%s] 重连失败: %s", name, e)
            self.servers.pop(name, None)
            self._missing[name] = cfg
            return False

        self.clients[name] = client
        self.servers[name] = cfg
        for t in tools or []:
            tname = getattr(t, "name", None) or ""
            if tname:
                self._tool_map[self.prefixed(name, tname)] = t

        # _by_name 从 _tool_map 反推，避免残留已失效的旧对象
        self._by_name = {}
        for t in self._tool_map.values():
            tname = getattr(t, "name", None) or ""
            if tname:
                self._by_name.setdefault(tname, []).append(t)

        self.tools = list(self._tool_map.values())
        self._missing.pop(name, None)
        logger.info("MCP server [%s] 已重连", name)
        return True

    async def health(self) -> List[str]:
        """健康检查：返回当前可用工具名。"""
        return self.list_tool_names()

    # ---- 底层调用 ----
    @staticmethod
    def _join_text(items: Any) -> str:
        """从 MCP content 列表中抽取所有 text 段并拼接。"""
        if not isinstance(items, list):
            return ""
        return "\n".join(
            it.get("text", "")
            for it in items
            if isinstance(it, dict) and it.get("type") == "text"
        )

    @staticmethod
    def _extract_text(result: Any) -> str:
        """把 MCP 工具返回解析为纯文本。

        部分 MCP server 经 langchain-mcp-adapters 返回的是 MCP content 列表：
        ``[{'type':'text','text':'<payload>'}, {'id': ...}]``；个别情况下也可能是
        该列表的字符串形式（JSON 或 Python repr）。这里统一剥掉包裹，取出 text
        部分；payload 本身是否为 JSON，交给 call_tool_json 进一步判断。
        """
        if isinstance(result, list):
            return MCPSession._join_text(result)
        if not isinstance(result, str):
            return str(result)
        s = result.strip()
        for parser in (json.loads, ast.literal_eval):
            try:
                obj = parser(s)
            except Exception:
                continue
            if isinstance(obj, list):
                return MCPSession._join_text(obj)
            if isinstance(obj, dict) and "text" in obj:
                return obj["text"]
        return s

    async def invoke_tool(
        self, tool: Any, arguments: Optional[Dict[str, Any]] = None
    ) -> str:
        """直接调用 Tool 对象，不走名字查找。

        已经拿到 Tool 本体时这条路最稳：名字可能在多个 server 上重复，而对象自己
        知道该往哪个 server 发。
        """
        return self._extract_text(await tool.ainvoke(arguments or {}))

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """按名调用一个 MCP 工具，返回拼接后的纯文本结果（自动路由到所属 server）。"""
        tool = self.get_tool(name)
        result = await tool.ainvoke(arguments or {})
        return self._extract_text(result)

    async def call_tool_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        raw = await self.call_tool(name, arguments)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
