"""知识库能力：把本地资料（当前是 PDF）以**页面截图**的形式交给 agent。

见 :mod:`knowledge.tools` 的说明。
"""
from .tools import MAX_PAGES, build_knowledge_tools, expand_pages, resolve_in_space

__all__ = ["MAX_PAGES", "build_knowledge_tools", "expand_pages", "resolve_in_space"]
