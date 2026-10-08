"""A2A 协议层：客户端与服务端统一在此包内。

- ``client.py``：A2AClient（本端去调对端）
- ``server/``：A2A 服务端（对端经 A2A 调本端），提供 build_a2a_app / build_agent_card
"""
from .client import A2AClient
from .server import build_a2a_app, build_a2a_router, build_agent_card

__all__ = ["A2AClient", "build_a2a_app", "build_a2a_router", "build_agent_card"]
