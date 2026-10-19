"""agent 内核：装配 + 执行（与聊天/页面无关）。

- ``keeper.py``  ：**装配**——配置 → 实例 → 可执行体（Process），以及资源生命周期
- ``executor.py``：**执行**——一轮对话怎么跑完（就绪守卫、逐步落库、挂起恢复）
- ``process.py`` ：``ReactProcess``（普通模式）及**三模式共享**的 ``Step`` /
  ``ProcessResult`` 与记账函数（``check_budget_scope`` / ``log_usage``）
- ``simple_process.py`` ：``SimpleProcess``（快捷模式，一次性任务）；（``PtcProcess``
  程序化模式待加，届时同样独立成文件）
- ``planner.py`` ：``ReActPlanner``，把工具清单+问题喂给 LLM 并解析其决策

会话/落库/页面相关逻辑在 ``keeper.chat`` 包，不在本包。
"""
from .executor import AgentExecutor
from .keeper import (
    KeeperAgent,
    get_keeper,
    list_keepers,
    register_keeper,
    unregister_keeper,
    build_all_agents,
    shutdown_all,
)
from .planner import Decision, ReActPlanner
from .process import ReactProcess, ProcessResult, Step
from .simple_process import SimpleProcess

__all__ = [
    "KeeperAgent",
    "AgentExecutor",
    "get_keeper",
    "register_keeper",
    "unregister_keeper",
    "list_keepers",
    "build_all_agents",
    "shutdown_all",
    "ReactProcess",
    "SimpleProcess",
    "ProcessResult",
    "Step",
    "Decision",
    "ReActPlanner",
]
