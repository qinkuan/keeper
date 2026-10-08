"""LLM 配置结构 —— 参数与 app/llm/config.py 的 LLMConfig 保持一致。

字段：
    model_name / provider / api_key / base_url / temperature / timeout / max_retries

provider 支持 openai / deepseek / siliconflow / anthropic / claude / ollama；
其中 deepseek、siliconflow 是 OpenAI 兼容端点，统一映射到 openai 并配合 base_url 使用。

说明：keeper 可单独打包（docker），故自带一份配置结构，不依赖 app 包。
"""
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel


class Provider(str, Enum):
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    SILICONFLOW = "siliconflow"
    ANTHROPIC = "anthropic"
    CLAUDE = "claude"
    OLLAMA = "ollama"


# provider 别名 -> langchain model_provider
PROVIDER_MAP: Dict[str, str] = {
    "openai": "openai",
    "deepseek": "openai",       # DeepSeek 官方 OpenAI 兼容
    "siliconflow": "openai",    # 硅基流动 OpenAI 兼容
    "anthropic": "anthropic",
    "claude": "anthropic",
    "ollama": "ollama",         # 本地 Ollama / vLLM
}


# 未显式指定 model_name 时，按 provider 取默认模型
DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
    "anthropic": "claude-3-5-sonnet-latest",
    "claude": "claude-3-5-sonnet-latest",
    "ollama": "qwen2.5",
}


class LLMConfig(BaseModel):
    model_name: str
    provider: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.2
    timeout: int = 120
    max_retries: int = 3
