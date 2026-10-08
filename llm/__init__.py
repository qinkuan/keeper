"""LLM 子包：配置结构 + 最小接口 + 工厂。"""
from .config import DEFAULT_MODELS, PROVIDER_MAP, LLMConfig, Provider
from .client import LangChainLLM, LLM, build_model, get_llm

__all__ = [
    "LLMConfig", "Provider", "PROVIDER_MAP", "DEFAULT_MODELS",
    "LLM", "LangChainLLM", "build_model", "get_llm",
]
