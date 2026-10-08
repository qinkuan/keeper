"""声明式 Skill：以数据（prompt 模板 + tool 列表 + 可选子图）描述能力，而非 Python 代码。

这样 JS 等其它语言运行时也能解释同一份 skill 定义；agent 按名动态加载，不硬编码进节点逻辑。
（对应架构文档第六节 Skill 声明式。）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Skill:
    name: str
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)
    optional_subgraph: Optional[Dict[str, Any]] = None
    # 命中词：系统侧预加载用它们跟用户问题做匹配（可选；不写则从 description 抽）
    keywords: List[str] = field(default_factory=list)
    # 「什么时候用」——L1 概要，常驻上下文；为空意味着作者没写，退化为常驻全文
    # （见 capability-loading-design.md 第九节：能自动降级，就不许能力消失）。
    description: str = ""
    # True = 行为型技能（做事的规矩），正文常驻；False = 任务型，按需加载。
    # 装配层会把「没写 description」的也归一成 True，下游只看这一个字段。
    always: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Skill:
        if "name" not in d:
            raise ValueError(f"skill 定义缺少 name: {d}")
        return cls(
            name=d["name"],
            system_prompt=d.get("system_prompt", ""),
            tools=list(d.get("tools", [])),
            optional_subgraph=d.get("optional_subgraph"),
            keywords=list(d.get("keywords", [])),
            description=str(d.get("description") or "").strip(),
            always=bool(d.get("always") or False),
        )


class SkillRegistry:
    """skill 注册中心：从字典列表加载，按名查找。"""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def load_from_dicts(self, dicts: List[Dict[str, Any]]) -> "SkillRegistry":
        """批量登记；返回 self 便于链式调用（数据源来自数据库装配或接口）。"""
        for d in dicts:
            self.register(Skill.from_dict(d))
        return self

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def all(self) -> List[Skill]:
        return list(self._skills.values())
