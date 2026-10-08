"""framework 环境接口：检测本机已装的 python / node，并支持按 agent 指定解释器。

设计边界：

- **不安装**：只负责「看见本机有什么」与「为每个 agent 记下用哪个解释器」。
  真正的下载 / 安装不在当前范围。
- 检测逻辑在 ``runtime.detect_runtimes``；per-agent 选择落到 ``config.yaml`` 的
  ``agent_overrides[agent_id]``（``plat.sync`` 装载时会按它解析解释器）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import load_config, save_agent_env
from .runtime import detect_runtimes

router = APIRouter(prefix="/framework", tags=["framework"])


class AgentEnvUpdate(BaseModel):
    """保存某个 agent 的解释器选择；空字符串表示清空（回退到 framework 全局）。"""

    python: str = ""
    node: str = ""


def _build_detected(kind: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in detect_runtimes(kind):
        raw = r.get("version")
        if isinstance(raw, (tuple, list)):
            version = ".".join(str(x) for x in raw)
        else:
            version = str(raw or "")
        out.append(
            {
                "path": str(r["path"]),
                "version": version,
                "version_string": r.get("version_string") or "",
            }
        )
    # detect_runtimes 已按版本降序；这里保持
    return out


def _agent_env(agent_id: str) -> Dict[str, Any]:
    cfg = load_config()
    override = cfg.agent_overrides.get(agent_id) or {}
    py_detected = _build_detected("python")
    node_detected = _build_detected("node")
    # 选择优先级：agent 覆盖 > framework 全局 > 本机检测到的第一个。
    # 应用本身会带环境（后续可能随应用打包），这里默认就选检测到的首个解释器。
    selection = {
        "python": override.get("python")
        or cfg.framework.python
        or (py_detected[0]["path"] if py_detected else None),
        "node": override.get("node")
        or cfg.framework.node
        or (node_detected[0]["path"] if node_detected else None),
    }
    return {
        "agent_id": agent_id,
        "requirements": {
            "python": None,
            "node": None,
            "note": (
                "该 Agent 的依赖（MCP / 插件）需要 Python 与 Node 解释器来执行。"
                "默认选用本机检测到的第一个解释器；如有需要，可改为指定路径"
                "（当前版本不做环境安装）。"
            ),
        },
        "detected": {
            "python": py_detected,
            "node": node_detected,
        },
        "selection": selection,
    }


@router.get("/agents/{agent_id}/env")
def get_agent_env(agent_id: str) -> Dict[str, Any]:
    """读某个 agent 的环境配置：需求说明 + 本机检测到的解释器 + 当前选择。"""
    return _agent_env(agent_id)


@router.put("/agents/{agent_id}/env")
def update_agent_env(agent_id: str, body: AgentEnvUpdate) -> Dict[str, Any]:
    """保存某个 agent 的解释器选择（写到 config.yaml 的 agent_overrides）。"""
    save_agent_env(agent_id, body.python, body.node)
    return _agent_env(agent_id)
