"""A2A 协议 v1.0.0 数据模型（Pydantic v2）。

- 字段命名遵循规范的 camelCase；序列化用 ``model_dump(by_alias=True)``。
- 方法名采用 v1.0.0 的 PascalCase（SendMessage / GetTask / CancelTask ...）。
- 内容 Part 以 ``text / raw / url / data`` 区分，不再有 TextPart 等独立类型名。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    ROLE_UNSPECIFIED = "ROLE_UNSPECIFIED"
    ROLE_USER = "ROLE_USER"
    ROLE_AGENT = "ROLE_AGENT"


class TaskState(str, Enum):
    TASK_STATE_UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
    TASK_STATE_WORKING = "TASK_STATE_WORKING"
    TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
    TASK_STATE_FAILED = "TASK_STATE_FAILED"
    TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
    TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
    TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


# --------------------------------------------------------------------------- #
# 内容
# --------------------------------------------------------------------------- #
class Part(BaseModel):
    text: Optional[str] = None
    raw: Optional[str] = None          # base64 编码的字节
    url: Optional[str] = None
    data: Optional[Any] = None
    metadata: Optional[Dict[str, Any]] = None
    filename: Optional[str] = None
    mediaType: Optional[str] = None

    model_config = {"extra": "allow"}


class Message(BaseModel):
    messageId: str
    role: Role
    parts: List[Part]
    contextId: Optional[str] = None
    taskId: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    extensions: Optional[List[str]] = None
    referenceTaskIds: Optional[List[str]] = None

    model_config = {"extra": "allow"}


class Artifact(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parts: List[Part] = Field(default_factory=list)
    index: Optional[int] = None
    append: Optional[bool] = None
    lastChunk: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class TaskStatus(BaseModel):
    state: TaskState
    message: Optional[Message] = None
    timestamp: Optional[str] = None

    model_config = {"extra": "allow"}


class Task(BaseModel):
    id: str
    contextId: Optional[str] = None
    status: TaskStatus
    artifacts: Optional[List[Artifact]] = None
    history: Optional[List[Message]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


# --------------------------------------------------------------------------- #
# AgentCard
# --------------------------------------------------------------------------- #
class AgentProvider(BaseModel):
    organization: str
    url: Optional[str] = None

    model_config = {"extra": "allow"}


class AgentCapabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False
    extendedAgentCard: bool = False
    extensions: Optional[List[Any]] = None

    model_config = {"extra": "allow"}


class AgentInterface(BaseModel):
    url: str
    protocolBinding: str
    protocolVersion: str
    tenant: Optional[str] = None

    model_config = {"extra": "allow"}


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    kind: Optional[str] = None
    tags: Optional[List[str]] = None
    examples: Optional[List[str]] = None
    inputModes: Optional[List[str]] = None
    outputModes: Optional[List[str]] = None
    extensions: Optional[List[str]] = None

    model_config = {"extra": "allow"}


class AgentCard(BaseModel):
    name: str
    description: str
    supportedInterfaces: List[AgentInterface]
    version: str
    capabilities: AgentCapabilities
    provider: Optional[AgentProvider] = None
    documentationUrl: Optional[str] = None
    securitySchemes: Optional[Dict[str, Any]] = None
    securityRequirements: Optional[List[Any]] = None
    defaultInputModes: List[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: List[str] = Field(default_factory=lambda: ["text/plain"])
    skills: List[AgentSkill] = Field(default_factory=list)
    iconUrl: Optional[str] = None
    signatures: Optional[List[Any]] = None

    model_config = {"extra": "allow"}


# --------------------------------------------------------------------------- #
# JSON-RPC 2.0 信封
# --------------------------------------------------------------------------- #
class JSONRPCRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str
    params: Optional[Any] = None


class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None


class JSONRPCResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    result: Optional[Any] = None
    error: Optional[JSONRPCError] = None
