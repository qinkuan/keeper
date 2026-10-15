"""LLM 子包：配置结构 + 最小接口 + 工厂。

模型**绑定在 agent 上**（``agent_llm_binding``），装配期解析一次、长期复用，
所以这里只有一次性构建的 :func:`get_llm`，没有实例缓存。
"""
from .config import DEFAULT_MODELS, PROVIDER_MAP, LLMConfig, Provider
from .client import LangChainLLM, LLM, build_model, get_llm

__all__ = [
    "LLMConfig", "Provider", "PROVIDER_MAP", "DEFAULT_MODELS",
    "LLM", "LangChainLLM", "build_model", "get_llm",
]
