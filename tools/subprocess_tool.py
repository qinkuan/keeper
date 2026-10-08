"""插件的「可执行文件」组件：把一个子进程包装成普通工具。

为什么不在主进程里 import 插件代码（见 ``tools/external.py``）：
第三方代码进主进程等于把 keeper 的全部权限交给它——能读所有 agent 的会话、
依赖还可能跟 keeper 自己打架。所以插件默认走**子进程**，语言无关、
崩了也只影响这一次调用。

约定（写给插件作者）：

- 调用：keeper 起进程，stdin 写入**一行** JSON
  ``{"tool": "<工具名>", "args": {...}, "context": {"workspace": "...", "read_only": true}}``
- 返回：**stdout 的最后一行**必须是 JSON
  ``{"ok": true, "result": "..."}`` 或 ``{"ok": false, "error": "..."}``
  （前面允许打日志，所以取最后一行）
- 非零退出码 / 超时 / 输出不是 JSON，都算调用失败。

失败时抛 :class:`SubprocessToolError`，由 ``ProcessorTool.execute`` 兜成
一段错误文本返回给 LLM——**不会**让 agent 崩掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..chat.context import current_workspace
from .base import ProcessorTool

logger = logging.getLogger(__name__)

# 单次调用默认超时（秒）：插件可执行文件的启动 + 执行
DEFAULT_TIMEOUT = 60


class SubprocessToolError(RuntimeError):
    """可执行文件调用失败（进程级错误，区别于工具自己返回的业务错误）。"""


def parse_parameters(schema: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """把 JSON Schema 压成 ``{参数名: 说明}``——``ProcessorTool.parameters`` 的形状。

    兼容两种写法：标准 JSON Schema（``properties`` + ``required``），
    以及偷懒的 ``{参数名: 说明}`` 直给。
    """
    if not schema:
        return {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        required = set(schema.get("required") or [])
        out: Dict[str, str] = {}
        for k, v in props.items():
            desc = v.get("description") if isinstance(v, dict) else str(v)
            out[str(k)] = (desc or "") + ("（必填）" if k in required else "")
        return out
    return {str(k): str(v) for k, v in schema.items() if isinstance(v, str)}


def build_argv(command: str, args: Optional[List[str]]) -> List[str]:
    """拼出 argv：优先把 ``command`` 当成**单个可执行文件**（路径可能含空格）。

    只有它既不是已存在的文件、也不在 PATH 里、却含空格时，才按
    ``"python x.py"`` 这种整串写法拆开——兼顾两种习惯又不误伤带空格的路径。
    """
    extra = [str(a) for a in (args or [])]
    p = Path(command)
    if p.is_file() or p.is_absolute() or shutil.which(command):
        return [command, *extra]
    parts = shlex.split(command)
    if len(parts) > 1:
        return [*parts, *extra]
    return [command, *extra]


def _parse_result(stdout: str) -> Optional[Dict[str, Any]]:
    """取 stdout 最后一行非空内容解析成 JSON；拿不到返回 None。"""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


async def call_subprocess_tool(
    *,
    command: str,
    tool: str,
    args: Optional[dict] = None,
    extra_argv: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    workspace: Optional[Path] = None,
    read_only: bool = True,
    agent_space: Optional[Path] = None,
    # 本工具**自身**是否只读（能否与其它工具并发跑，P1-1）。
    # 与上面的 read_only 是两回事：那个是「工作区只读」，这个是「这个工具不改东西」。
    tool_read_only: bool = False,
) -> str:
    """起一次子进程调用工具，返回 ``result`` 字符串；失败抛 ``SubprocessToolError``。"""
    payload = json.dumps(
        {
            "tool": tool,
            "args": args or {},
            "context": {
                "workspace": str(workspace) if workspace else "",
                "read_only": read_only,
                "agent_space": str(agent_space) if agent_space else "",
            },
        },
        ensure_ascii=False,
    )
    argv = build_argv(command, extra_argv)
    logger.info("插件可执行文件调用：%s (cwd=%s)", " ".join(argv), cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        raise SubprocessToolError(f"无法启动可执行文件（{command}）：{e}") from e

    try:
        out, err = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SubprocessToolError(f"可执行文件调用超时（{timeout}s）：{command}") from None

    text = out.decode("utf-8", "replace")
    err_text = err.decode("utf-8", "replace").strip()
    data = _parse_result(text)

    if data is None:
        if proc.returncode != 0:
            raise SubprocessToolError(
                f"可执行文件退出码 {proc.returncode}；"
                f"stderr={err_text[:300] or '空'}；stdout={text[:300] or '空'}"
            )
        raise SubprocessToolError(
            f"输出不是 JSON（约定 stdout 最后一行是 JSON）：{text[:300] or '空'}"
        )
    if not data.get("ok"):
        raise SubprocessToolError(str(data.get("error") or "工具返回失败"))
    return str(data.get("result", ""))


def make_subprocess_tool(
    *,
    name: str,
    description: str,
    schema: Optional[Dict[str, Any]] = None,
    command: str = "",
    tool: Optional[str] = None,
    extra_argv: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    workspace: Optional[Path] = None,
    read_only: bool = True,
    tool_read_only: bool = False,
) -> ProcessorTool:
    """把一个可执行文件条目包装成 :class:`ProcessorTool`（与主进程工具同构）。

    ``name`` 是**注册名**（带插件前缀，LLM 看到的是它）；``tool`` 是**传给可执行文件
    的名字**（清单里声明的短名）。分开是因为前缀只是 keeper 侧的命名空间——
    可执行文件不该关心自己被装到哪个插件下，它只认自己声明的工具名。

    ``agent_space`` 不在这里固化：bin 工具是**每次调用**起子进程，运行在请求期的
    ReAct 循环里，应当取当时会话的 ``current_workspace().agent_space``（per-session），
    所以放到 ``run`` 闭包里从 ContextVar 解析，而不是用 build 期的稳态值。
    """
    if not command:
        raise SubprocessToolError(f"工具 {name} 缺少 command")
    tool_name = tool or name

    async def run(args: dict) -> str:
        ws = current_workspace()
        agent_space = ws.agent_space if ws else None
        # 工作区根也取请求期的实际用户空间（default 或被绑定的那个），
        # 而不是 build 期的兜底值——否则会话绑了别的用户空间，bin 工具仍会落到 default。
        ws_root = ws.root if ws else workspace
        return await call_subprocess_tool(
            command=command,
            tool=tool_name,
            args=args,
            extra_argv=extra_argv,
            cwd=cwd,
            timeout=timeout,
            workspace=ws_root,
            read_only=read_only,
            agent_space=agent_space,
        )

    return ProcessorTool(
        name=name,
        description=description,
        parameters=parse_parameters(schema),
        run=run,
        read_only=tool_read_only,
        # bin 类工具能执行任意可执行文件、且 cwd 在插件目录，必须过守卫。
        # 之前只把 read_only 塞进子进程 stdin 的 context（那是「信息」，插件爱
        # 写不写），现在在 keeper 侧补上真正的拦截。
        dangerous=True,
    )
