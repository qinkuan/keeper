"""keeper 存储层：会话与步骤的持久化（SQLite + SQLAlchemy 异步）。

除会话外，这里也存**能力市场的资源定义**——现在只有插件（plugin），
一个包 + 一份清单，统一了原 mcp / skill / tool 三类；它们是装配一个
agent 的原料，与会话的写入互不相干。

设计见 keeper/doc/session-design.md。
"""
from .database import (
    DEFAULT_DB_PATH,
    db_url,
    dispose_engine,
    get_engine,
    get_session_factory,
    init_db,
)
from .models import (
    Agent,
    AgentLLMBinding,
    LLMProfile,
    ModelPrice,
    LLMCall,
    ToolCall,
    CapabilityLoad,
    ReactParseStat,
    AgentMessage,
    AgentCapabilityOverride,
    AgentPeer,
    A2ATask,
    A2AOutboundTask,
    Base,
    ChatSession,
    UserSpace,
    ReactStep,
    SessionMessage,
    Task,
    TaskItem,
    Thread,
    USER_SPACE_DEFAULT_NAME,
    new_id,
)

__all__ = [
    # 引擎
    "DEFAULT_DB_PATH",
    "db_url",
    "get_engine",
    "get_session_factory",
    "init_db",
    "dispose_engine",
    # 模型：会话与执行
    "Base",
    "ChatSession",
    "SessionMessage",
    "UserSpace",
    "ReactStep",
    "Task",
    "TaskItem",
    "Thread",
    "AgentMessage",
    "AgentCapabilityOverride",
    "AgentPeer",
    "A2AOutboundTask",
    # 模型：能力市场（资源定义）
    # 模型：agent 与其装配关系
    "Agent",
    "AgentLLMBinding",
    "LLMProfile",
    "ModelPrice",
    "LLMCall",
    "ToolCall",
    "CapabilityLoad",
    "ReactParseStat",
    "USER_SPACE_DEFAULT_NAME",
    "new_id",
]
