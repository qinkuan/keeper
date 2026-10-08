"""从平台拉取 agent 配置——本地运行时的**上游**。

平台是配置之源：mcp / skill / tool / agent 的定义都在那里，
本地应用凭 token 拉"我已添加"的那批，按平台分配的 agentId 原样装载。

平台不参与运行时协作、不收会话数据，这里也就只有「拉配置」一件事。

配置来源（优先级从高到低）：
- 环境变量 ``KEEPER_PLATFORM_URL`` / ``KEEPER_PLATFORM_TOKEN``
- ``config.yaml`` 的 ``platform:`` 段
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

import httpx

from ..config import KeeperConfig, load_config

logger = logging.getLogger(__name__)


def platform_settings() -> Tuple[str, str]:
    """取平台地址与令牌（env 优先，其次 config.yaml）。"""
    try:
        cfg = load_config()
    except FileNotFoundError:
        cfg = KeeperConfig()
    url = os.getenv("KEEPER_PLATFORM_URL") or cfg.platform.url
    token = os.getenv("KEEPER_PLATFORM_TOKEN") or cfg.platform.token
    return (url or "").rstrip("/"), token or ""


async def fetch_my_agents() -> List[Dict[str, Any]]:
    """拉取"我已添加"的 agent 完整配置（含解析后的 mcp / skill / tool）。

    返回的是 `RuntimeConfigOut.agents` 那一段；每组里的 ``id`` 与平台一致，
    本地**原样使用**，不自行生成——会话历史按它归拢，重部署后仍可对上。
    """
    url, token = platform_settings()
    if not url:
        raise RuntimeError("未配置平台地址（config.yaml platform.url 或 KEEPER_PLATFORM_URL）")
    if not token:
        raise RuntimeError(
            "未配置平台访问令牌（config.yaml platform.token 或 KEEPER_PLATFORM_TOKEN）"
        )

    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.get(
            f"{url}/api/me/agents",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        data = r.json()

    agents = data.get("agents") or []
    logger.info("已从平台拉取 %d 个 agent 配置", len(agents))
    return agents
