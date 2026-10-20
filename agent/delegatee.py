"""子 agent（Delegatee）抽象：主 ReAct 循环在「自己干不了」时把任务委托给它。

一个 Delegatee 是一个**有自己内部循环**的能力——例如 ``code``：写代码 → 跑 →
看输出 → 迭代；``video``：规划 → 生成 → 合成。它与工具（单步 ``ProcessorTool``）
的区别在于多做几步、有自己的推理。主循环通过协议级 ``TASK`` 动作触达它，结果
（``DelegateResult``）当 observation 回喂主循环，主循环据此继续或给 FINAL。

Delegatee 注册在独立的 ``DelegateeRegistry``，**不进工具清单**——模型不会把它
当成普通工具去 ``ACTION:`` 调用，只能走 ``TASK:`` 这个专门动作。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class Requirement:
    """委托给子 agent 的任务。"""

    text: str  # 要求（自然语言）
    context: Optional[str] = None  # 已知信息（可选；大对象走共享工作区里的文件）


@dataclass
class Ctx:
    """子 agent 运行时的共享上下文。"""

    workspace: Any  # WorkspaceCtx（current_workspace）
    call_tool: Callable[[str, dict], Awaitable[str]]  # 内核工具桥（已屏蔽再委托）
    budget: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DelegateResult:
    answer: str  # 最终给主循环的结果文本
    artifacts: List[str] = field(default_factory=list)  # 产物路径（视频 / 文件等）
    ask: Optional[str] = None  # 非交互模式下一般 None；信息不足时以文本冒泡
    steps: list = field(default_factory=list)


class Delegatee:
    """子 agent 基类。具体能力（code / video / ...）实现 ``run``。"""

    name: str = ""
    description: str = ""

    async def run(self, req: Requirement, ctx: Ctx) -> DelegateResult:
        raise NotImplementedError


class DelegateeRegistry:
    """子 agent 注册表，独立于工具注册表（TASK 动作不进「可用工具」清单）。"""

    def __init__(self) -> None:
        self._d: Dict[str, Delegatee] = {}

    def register(self, d: Delegatee) -> None:
        self._d[d.name] = d

    def get(self, name: str) -> Optional[Delegatee]:
        return self._d.get(name)

    def all(self) -> List[Delegatee]:
        return list(self._d.values())

    def names(self) -> List[str]:
        return list(self._d)

    def describe(self) -> str:
        if not self._d:
            return "（当前没有可用子 agent）"
        return "\n".join(f"- {d.name}：{d.description}" for d in self._d.values())
