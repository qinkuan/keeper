"""已注册的子 agent（delegatee）类型与构造工厂。

每个类型对应一个 ``Delegatee`` 子类（如 code → ``CodeDelegatee``）。配置（模型/插件）
来自 ``delegatee_configs`` 表，按 delegatee 名匹配。新增子 agent 只需在这里登记 +
实现 ``Delegatee`` 子类，主循环 / planner / API 零改动。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Type

from .code_delegatee import CodeDelegatee
from .delegatee import Delegatee

# delegatee 名 -> 实现类。新增子 agent 在此登记一处即可。
DELEGATEE_TYPES: Dict[str, Type[Delegatee]] = {
    "code": CodeDelegatee,
}


def list_delegatee_types() -> List[dict]:
    return [
        {"name": name, "description": cls.description}
        for name, cls in DELEGATEE_TYPES.items()
    ]


def build_delegatee(
    name: str, llm, tools=None, max_iter: int = 5
) -> Optional[Delegatee]:
    cls = DELEGATEE_TYPES.get(name)
    if cls is None:
        return None
    return cls(llm=llm, tools=tools, max_iter=max_iter)


def _parse_list(s: Optional[str]) -> List[str]:
    """把存成 JSON 字符串的白名单还原成列表（容错：非 list / 解析失败都返回 []）。"""
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _dump_list(lst) -> Optional[str]:
    if not lst:
        return None
    return json.dumps(list(lst), ensure_ascii=False)


def filter_tool_registry(tools, tools_whitelist, plugins_whitelist):
    """从宿主工具表按白名单筛出子 agent 专属子集，返回新 ToolRegistry。

    ``tools_whitelist`` 工具名精确匹配；``plugins_whitelist`` 插件名前缀匹配
    （工具名形如 ``插件__server__tool``）。两者都空返回 None（调用方回退宿主完整表）。
    """
    from ..tools.base import ToolRegistry

    names = set(tools_whitelist or [])
    plugins = set(plugins_whitelist or [])
    if not names and not plugins:
        return None
    reg = ToolRegistry()
    for t in tools.all():
        tn = getattr(t, "name", "")
        if tn in names:
            reg.register(t)
        elif any(tn.startswith(p + "__") for p in plugins):
            reg.register(t)
    return reg if reg.all() else None
