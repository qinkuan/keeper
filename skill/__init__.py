"""Skill 子包：声明式技能（数据而非代码，便于多语言 / 动态加载）。

对外只回答一件事：**当前有哪些可选 skill**。

skill 的数据源由调用方注入（数据库装配或接口），通过 ``SkillRegistry.load_from_dicts``
登记；这份清单里谁被启用，是调用方在**装载**时决定的，两边不混。
"""
from .base import Skill, SkillRegistry

__all__ = ["Skill", "SkillRegistry"]
