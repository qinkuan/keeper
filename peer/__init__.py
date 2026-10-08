"""对外协作：我与对端 agent 之间的链路与往来记录。

与 ``keeper/chat`` 对称：
    chat = 请求方 → 我   （我这侧存 chat_sessions + session_messages）
    peer = 我 → 对端     （我这侧存 threads + agent_messages）
"""
from .service import PeerService

__all__ = ["PeerService"]
