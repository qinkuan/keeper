"""正在跑的流式运行注册表：``session_id -> run_id``。

为什么需要它
------------
「停止生成」靠 ``run_id``，而 ``run_id`` 是**前端生成**的（``newRunId()``）。于是：

- 用户点「停止生成」时，前端手上正好有这个 run_id → 停得掉（正常路径）；
- 但**整页刷新 / 关标签页再打开**之后，前端内存全空，不知道服务端那边这次运行
  的 run_id 是什么 → 停不掉。

而刷新后我们又确实需要能停：一个跑了 20 步还没结束的长任务，用户刷新回来发现
还在跑，想让它停下是再自然不过的操作。

所以由服务端记一份「这个会话当前正在跑哪个 run_id」，让 ``load_messages`` 把它
带出去（它已经在合成「进行中」气泡），前端刷新后照样能停。

生命周期
--------
- **注册**：``runner`` 起跑后、第一次 ``on_step`` 时（新会话的 id 是服务端创建的，
  那之前还没有 key 可用）
- **注销**：``runner`` 的 finally——正常跑完 / 出错 / 被停止都会走到
- **兜底**：进程重启后注册表是空的（纯内存态）。此时刷新回来会看到「没有可停的」，
  这是对的：进程都没了，那轮请求自然也已经结束

并发约定
--------
同一会话一次只跑一个 ask（``ChatService._session_lock``），所以一个 session 键上
最多只有一个活跃 run，dict 足够；但注销时会比对 run_id，避免**上一轮的 finally
迟到把下一轮的登记抹掉**。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# session_id -> 正在跑的 run_id
_active_runs: Dict[str, str] = {}

# 被用户要求停止的 run_id 集合。执行循环每步 / 每块都问一次「该停了吗」。
#
# 为什么不住在 ``chat/api.py`` 里：这条集合现在**两个模块**要用——流式对话，以及
# 浮球的「对这一页提问」（见 api/agent_bridge.py::_ball_ask）。私藏在 api.py 里
# 就得跨模块 import 一个下划线开头的名字，那是明摆着的「它本来不该在这儿」。
_stop_runs: set = set()


def request_stop(run_id: Optional[str]) -> None:
    """请求停止某次运行。只置标记，不 kill——执行循环在下一个检查点优雅停下。"""
    if run_id:
        _stop_runs.add(run_id)


def is_stopped(run_id: Optional[str]) -> bool:
    """这个 run 该停了吗？"""
    return bool(run_id and run_id in _stop_runs)


def clear_stop(run_id: Optional[str]) -> None:
    """清掉停止标记。**必须在 runner 的 finally 里调**，否则同一 run_id 被复用时
    会带着上一次的停止标记出生——而 run_id 由前端生成，正常不会复用；但内核侧
    生成的（浮球那条路）就必须自己保证干净。"""
    if run_id:
        _stop_runs.discard(run_id)


def register_run(session_id: Optional[str], run_id: Optional[str]) -> None:
    """登记「这个会话正在跑这个 run」。

    两个参数任一为空就什么都不做：新会话在第一条消息落库前没有 id、客户端也可能
    没带 run_id（非流式的降级路径），这两种情况都无从登记。
    """
    if not session_id or not run_id:
        return
    _active_runs[session_id] = run_id


def unregister_run(session_id: Optional[str], run_id: Optional[str]) -> None:
    """注销。**只在 run_id 匹配时删**。

    比对是必须的：``runner`` 的 finally 可能比下一轮的注册晚到（客户端断开后我们
    不再杀它，它跑完才进 finally；这期间用户可能已经发了新一轮）。无脑 ``pop``
    会把新一轮的登记误删，导致「刷新后停不掉」偶发失效。
    """
    if not session_id or not run_id:
        return
    if _active_runs.get(session_id) == run_id:
        _active_runs.pop(session_id, None)


def active_run_id(session_id: Optional[str]) -> Optional[str]:
    """这个会话当前正在跑的 run_id；没有则 None。

    供 ``load_messages`` 塞进「进行中」气泡，前端据此在刷新后仍能停。
    """
    if not session_id:
        return None
    return _active_runs.get(session_id)
