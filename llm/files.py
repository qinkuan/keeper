"""LLM 厂商的**文件上传能力**（Files API）。

为什么单独一个文件，而不是塞进 ``llm/client.py``
------------------------------------------------
``client.py`` 只负责「把 messages 发给模型」，而文件上传是**另一条链路**：
它要 multipart 直连厂商、拿回一个 ``file_id``，之后这个 id 才作为 content
block 出现在 messages 里。两件事的失败模式、重试策略、参数形状都不同，
混在一起会让 ``client.py`` 变成一个既发消息又管上传的杂合体。

更要紧的是：**文件能力是按厂商分的，不是按「协议兼容」分的**。
``llm/config.py`` 里 ``deepseek`` / ``siliconflow`` 都映射到 langchain 的
``model_provider="openai"``（端点兼容），但它们的 Files API 完全不是一回事：

==============  ==========================  ======================  ==============
provider        上传端点                    引用格式               额外约束
==============  ==========================  ======================  ==============
openai          ``/files``                  ``{"file":{"file_id"}}`` 嵌套
deepseek        ``/files``                  ``{"file_id":...}``    扁平 + 只图片
anthropic       ``/v1/files``               document part          beta 头
siliconflow     未确认                      未确认                  ——
ollama          无                          无                     ——
==============  ==========================  ======================  ==============

所以这里用一张**按 keeper 自己的 Provider 枚举注册**的能力表，而不是按
``PROVIDER_MAP`` 的 langchain 归类分派。

目前只实现 deepseek
-------------------
其余 provider 一律走 :meth:`uploader_for` 返回 ``None`` → 调用方提示
「当前模型不支持文件上传」。这是**有意的收敛**：一次只做一条能跑通的路，
比铺开五个半成品更容易验证，也更容易在出问题时定位。

调用方拿到的 :class:`UploadedFile` 与本地缓存无关——本地那份是给前端预览和
重新上传用的，agent 永远只认 ``file_id``（见 ``chat/service.py`` 的注入处）。
"""
from __future__ import annotations

import os  # noqa: F401  （留作后续 provider 读环境变量兜底；当前只用 _expand_env）
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Tuple

from .config import LLMConfig

# 复用它而不是自己再写一份：api_key / base_url 在 llm_profiles 里可以写成
# ${ENV_VAR} 引用环境变量（llm/client.py 的既有约定）。上传这条路同样吃这个
# 约定，否则字面量 "${DEEPSEEK_KEY}" 会被当成真 key 发出去。
from .client import _expand_env

# DeepSeek 的 Files API 是 OpenAI 兼容的，直接用官方 SDK（字段名 bytes / object /
# purpose 与它期望的一致）。做 try 兜底：openai 平时只是 langchain-openai 的间接
# 依赖，万一没装，也不能让整个 llm 包导入失败——那会连带搞垮 chat / agent 全链路。
try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - 环境没装 openai
    AsyncOpenAI = None  # type: ignore[assignment]

# DeepSeek 的 OpenAI 兼容端点默认地址。cfg.base_url 为空时用它。
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"

# 文档明确：只有 deepseek-flash 能用文件。换成别的模型不是"降级"，是直接 4xx。
DEEPSEEK_FILE_MODELS = frozenset({"deepseek-flash"})

# 单张上限 64 MiB（文档）。超过就在本地拦掉，不浪费一次上传往返。
DEEPSEEK_MAX_BYTES = 64 * 1024 * 1024

# 只支持这四种图片。格式由**内容**判定，不信扩展名——文档也是这么说的，
# 而且扩展名用户可以随便改，拿它做依据等于没有依据。
DEEPSEEK_ALLOWED_MIME = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


def sniff_image_mime(data: bytes) -> Optional[str]:
    """按文件头判定是不是受支持的图片；不是则返回 None。

    为什么不看扩展名：用户可以把 ``evil.exe`` 改名成 ``cat.png``。厂商侧也会
    自己判（文档说"格式由文件实际内容判断"），我们提前判一遍只是为了在**上传前**
    就给人话提示，而不是等一个看不懂的 4xx。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    # WebP: "RIFF" + 4 字节长度 + "WEBP"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass
class UploadedFile:
    """一次成功上传的结果。**只存厂商侧事实**，不存本地缓存路径。

    本地缓存的路径由 ``chat`` 层记在消息的 attachments JSON 里（相对路径
    ``@tmp/...``，可迁移）；这里存的是厂商账号维度的事实，两者刻意分开——
    混在一起的话，换机器时厂商侧信息会被本地路径污染。
    """

    file_id: str
    name: str
    mime: str
    size: int
    provider: str
    model: str
    #: 过期时间（Unix 秒）；None = 永久。上传时不设 TTL 就是 None。
    expires_at: Optional[int] = None
    #: 厂商原始响应，排查用（不落库、不下发前端）
    raw: Dict[str, Any] = field(default_factory=dict)


class FileUploader(Protocol):
    """每个 provider 的文件能力。"""

    provider: str

    def supports(self, cfg: LLMConfig) -> Tuple[bool, str]:
        """这个配置能不能用本上传器。

        第二个返回值是**说给人听的原因**：不支持时必须能直接显示给用户，
        而不是一个干巴巴的 False——用户需要知道"那我该怎么办"。
        """
        ...

    async def upload(self, cfg: LLMConfig, data: bytes, name: str) -> UploadedFile:
        """上传并返回 file_id。失败要抛异常，消息由调用方包装。"""
        ...

    def ref_part(self, file_id: str) -> Dict[str, Any]:
        """构造 messages 里的 content block。"""
        ...


class _DeepSeekUploader:
    """DeepSeek Files API（OpenAI 兼容端点）。"""

    provider = "deepseek"

    def supports(self, cfg: LLMConfig) -> Tuple[bool, str]:
        if (cfg.provider or "").lower() != self.provider:
            return False, f"当前模型是 {cfg.provider or '未知'}，文件上传只支持 deepseek"
        # 文档硬约束：只有 deepseek-flash。不提前拦的话，用户会等到 API 报错，
        # 而那个报错（4xx JSON）对用户没有任何可操作性。
        if (cfg.model_name or "") not in DEEPSEEK_FILE_MODELS:
            return False, (
                f"文件上传只支持 deepseek-flash，当前是 {cfg.model_name or '未指定'}"
            )
        # base_url 指向 Anthropic 兼容端点时，上传路径（/files）、鉴权头
        # （x-api-key + anthropic-beta）、字段命名全是另一套——我们的实现
        # 一样都不适用。这里直接拒绝，别让请求打到错的端点上还以为是自己网络问题。
        base = (cfg.base_url or "").rstrip("/").lower()
        if "/anthropic" in base:
            return False, "当前 base_url 是 Anthropic 兼容端点，文件上传暂只支持 OpenAI 兼容端点"
        return True, ""

    async def upload(self, cfg: LLMConfig, data: bytes, name: str) -> UploadedFile:
        # 本地先拦两道：大小与格式。厂商也会拦，但它的 4xx 是英文 JSON，
        # 用户看不懂；我们拦下来可以直接说"超过 64 MiB" / "只支持 PNG/JPEG/GIF/WebP"。
        if len(data) > DEEPSEEK_MAX_BYTES:
            raise ValueError(
                f"文件 {len(data) / 1024 / 1024:.1f} MiB，超过 64 MiB 上限"
            )
        mime = sniff_image_mime(data)
        if mime is None or mime not in DEEPSEEK_ALLOWED_MIME:
            raise ValueError("只支持 JPEG / PNG / GIF / WebP 图片")

        api_key = _expand_env(cfg.api_key)
        if not api_key:
            raise ValueError("模型配置里没有 API key，无法上传文件")
        base_url = _expand_env(cfg.base_url) or DEEPSEEK_DEFAULT_BASE_URL

        # 用 SDK 而不是手写 multipart：DeepSeek 的 OpenAI 兼容端点字段
        # （bytes / object / purpose）与 SDK 期望完全一致，手写只会多出 bug 面。
        #
        # 放在模块级 import 而不是函数内：函数内 import 每次上传都走一遍
        # sys.modules 查找（小事），更要紧的是它让测试**无法替换**这个依赖——
        # 打补丁打在模块属性上，函数内 import 会原封不动从 openai 包里拿真货，
        # 于是测试真的连出去了。可测试性在这里不是锦上添花，是必须。
        if AsyncOpenAI is None:  # pragma: no cover - 环境没装 openai
            raise RuntimeError("openai SDK 未安装，无法上传文件（pip install openai）")

        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=120)
        # purpose 必须为 user_data（文档）。不设 expires_after → 永久有效。
        created = await client.files.create(
            file=(name or "upload", data, mime),
            purpose="user_data",
        )
        return UploadedFile(
            file_id=created.id,
            name=getattr(created, "filename", None) or name,
            mime=mime,
            size=int(getattr(created, "bytes", None) or len(data)),
            provider=self.provider,
            model=cfg.model_name,
            expires_at=getattr(created, "expires_at", None),
            raw={"object": getattr(created, "object", None)},
        )

    def ref_part(self, file_id: str) -> Dict[str, Any]:
        # 注意这是 DeepSeek 的**扁平**格式：{"type":"file","file_id":...}。
        # OpenAI 原生是嵌套的 {"type":"file","file":{"file_id":...}}——照抄
        # OpenAI 的写法在 DeepSeek 上会静默失效（不报错，但模型看不到图）。
        return {"type": "file", "file_id": file_id}


_UPLOADERS: Dict[str, FileUploader] = {
    "deepseek": _DeepSeekUploader(),  # type: ignore[dict-item]
}


def uploader_for(provider: str) -> Optional[FileUploader]:
    """按 provider 取上传器；没有实现（或暂不支持）返回 None。

    返回 None 而不是抛异常：调用方要把它变成一句「当前模型不支持文件上传」，
    那是**预期的用户场景**（大部分 provider 就是没有这能力），不是错误。
    """
    return _UPLOADERS.get((provider or "").lower())
