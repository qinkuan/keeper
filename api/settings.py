"""设置：本地可调的用户配置（写回 ``~/.keeper/settings.yaml``）。

这里改的是**用户设置**，不碰元配置 ``config.yaml``——后者是部署时手改的
（端口 / 鉴权 / 用哪个 agent / 默认 LLM），不该被程序回写（pyyaml 会抹掉注释）。
读取时 settings.yaml 覆盖 config.yaml 的同名段，见 ``keeper/setting/user_settings.py``。

**默认值**：找不到用户设置文件（或某项没写）时，一律用 ``config.py`` 里 dataclass
字段的内置默认值——默认值只有那一处来源，运行行为与设置页看到的一致。

按**管理的内容**分段：

- ``context``     ：上下文治理（外部化 / 压缩）
- ``a2a``         ：协作 / 对端调用的超时与熔断
- ``prompt_dump`` ：调试用的完整 prompt 落盘

几段一起读、一起存（设置页分区展示，保存时整体提交），避免「改 A 把 B 冲掉」。
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..a2a import settings as a2a_settings
from ..config import (
    A2ASection,
    ContextSection,
    PromptDumpSection,
    load_config,
    save_settings,
)
from ..setting import invalidate_caches

router = APIRouter(prefix="/manage/settings", tags=["settings"])
logger = logging.getLogger(__name__)


def _default(section_cls: type, name: str):
    """取某段的**内置默认值**（来自 config.py 的 dataclass）。

    默认值只有一处来源：``config.py`` 的 dataclass 字段。这里与下面的 Pydantic
    模型都从它取，避免「两处各写一遍、改了一处忘了另一处」——配置文件不存在时
    用的就是这些值。
    """
    return getattr(section_cls(), name)


class PromptDumpIn(BaseModel):
    """prompt 落盘开关。完整 prompt 动辄几万字，默认关。"""

    enabled: bool = Field(default_factory=lambda: _default(PromptDumpSection, "enabled"))
    dir: str = Field(default_factory=lambda: _default(PromptDumpSection, "dir"))
    min_prompt_tokens: int = Field(
        default_factory=lambda: _default(PromptDumpSection, "min_prompt_tokens"), ge=0
    )
    retain_days: int = Field(
        default_factory=lambda: _default(PromptDumpSection, "retain_days"), ge=0
    )


class A2AIn(BaseModel):
    """A2A 出站（本端 → 对端）的超时与熔断。

    对端不由我们控制，所以每一项都必须有上限：没有超时就会挂死整轮，
    没有熔断就会在每轮都白白等满那个超时。
    """

    request_timeout: float = Field(
        default_factory=lambda: _default(A2ASection, "request_timeout"), ge=1, le=600
    )
    task_timeout: float = Field(
        default_factory=lambda: _default(A2ASection, "task_timeout"), ge=0, le=3600
    )  # 0 = 不限
    poll_interval: float = Field(
        default_factory=lambda: _default(A2ASection, "poll_interval"), ge=0.5, le=60
    )
    cancel_on_timeout: bool = Field(
        default_factory=lambda: _default(A2ASection, "cancel_on_timeout")
    )
    breaker_enabled: bool = Field(
        default_factory=lambda: _default(A2ASection, "breaker_enabled")
    )
    breaker_threshold: int = Field(
        default_factory=lambda: _default(A2ASection, "breaker_threshold"), ge=1, le=100
    )
    breaker_cooldown: float = Field(
        default_factory=lambda: _default(A2ASection, "breaker_cooldown"), ge=5, le=3600
    )


class ContextIn(BaseModel):
    """上下文治理：工具输出外部化 + 自动压缩（见 doc/context-design.md）。"""

    enabled: bool = Field(default_factory=lambda: _default(ContextSection, "enabled"))
    externalize_min_chars: int = Field(
        default_factory=lambda: _default(ContextSection, "externalize_min_chars"),
        ge=0, le=100000,
    )
    never_externalize: str = Field(
        default_factory=lambda: _default(ContextSection, "never_externalize")
    )
    retain_days: int = Field(
        default_factory=lambda: _default(ContextSection, "retain_days"), ge=0, le=365
    )
    preview_head: int = Field(
        default_factory=lambda: _default(ContextSection, "preview_head"), ge=0, le=5000
    )
    preview_tail: int = Field(
        default_factory=lambda: _default(ContextSection, "preview_tail"), ge=0, le=5000
    )
    dir: str = Field(default_factory=lambda: _default(ContextSection, "dir"))
    compact_enabled: bool = Field(
        default_factory=lambda: _default(ContextSection, "compact_enabled")
    )
    model_context_limit: int = Field(
        default_factory=lambda: _default(ContextSection, "model_context_limit"),
        ge=1000, le=2_000_000,
    )
    # 单次输出上限：0 = 不限制（不传给模型，行为与从前一致）
    max_output_tokens: int = Field(
        default_factory=lambda: _default(ContextSection, "max_output_tokens"),
        ge=0, le=200_000,
    )
    compact_ratio: float = Field(
        default_factory=lambda: _default(ContextSection, "compact_ratio"),
        ge=0.1, le=0.95,
    )
    compact_min_steps: int = Field(
        default_factory=lambda: _default(ContextSection, "compact_min_steps"),
        ge=2, le=500,
    )
    compact_min_ratio: float = Field(
        default_factory=lambda: _default(ContextSection, "compact_min_ratio"),
        ge=0.05, le=0.95,
    )
    compact_min_interval: int = Field(
        default_factory=lambda: _default(ContextSection, "compact_min_interval"),
        ge=0, le=100,
    )
    keep_recent_steps: int = Field(
        default_factory=lambda: _default(ContextSection, "keep_recent_steps"),
        ge=1, le=100,
    )
    compact_max_chars: int = Field(
        default_factory=lambda: _default(ContextSection, "compact_max_chars"),
        ge=200, le=20000,
    )
    # 单条输出与步数：决定「一轮最多能涨到多大」
    # 键名沿用 observation_limit（不重命名，避免破坏已有 settings.yaml），
    # 但语义是**内联上限**：超过它先外部化成「预览 + block_id」，外部化不可用
    # 时才硬截断。设置页上的名字已改成「单条输出内联上限」。
    observation_limit: int = Field(
        default_factory=lambda: _default(ContextSection, "observation_limit"),
        ge=200, le=200000,
    )
    max_steps: int = Field(
        default_factory=lambda: _default(ContextSection, "max_steps"), ge=1, le=200
    )
    # 取回与估算
    read_budget: int = Field(
        default_factory=lambda: _default(ContextSection, "read_budget"),
        ge=500, le=200000,
    )
    max_ctx_recall: int = Field(
        default_factory=lambda: _default(ContextSection, "max_ctx_recall"), ge=0, le=50
    )
    chars_per_token: float = Field(
        default_factory=lambda: _default(ContextSection, "chars_per_token"),
        ge=1.0, le=10.0,
    )
    pin_recent_reads: int = Field(
        default_factory=lambda: _default(ContextSection, "pin_recent_reads"),
        ge=0, le=50,
    )
    # ---- 技能 / 工具按需加载 ----
    tool_group_min: int = Field(
        default_factory=lambda: _default(ContextSection, "tool_group_min"),
        ge=1, le=100,
    )
    capability_preload: str = Field(
        default_factory=lambda: _default(ContextSection, "capability_preload")
    )
    cap_small_limit: int = Field(
        default_factory=lambda: _default(ContextSection, "cap_small_limit"),
        ge=0, le=200000,
    )
    tool_parallel: int = Field(
        default_factory=lambda: _default(ContextSection, "tool_parallel"),
        ge=0, le=2,
    )
    tool_parallel_max: int = Field(
        default_factory=lambda: _default(ContextSection, "tool_parallel_max"),
        ge=1, le=20,
    )

    @field_validator("capability_preload")
    @classmethod
    def _check_preload_policy(cls, v: str) -> str:
        """非法值一律回落 auto：写进 settings.yaml 的野值不该让整段配置失效。"""
        return v if v in ("auto", "off", "keyword", "llm") else "auto"


class SettingsIn(BaseModel):
    """设置页整体提交：两段都在（缺哪段就只改哪段）。"""

    prompt_dump: PromptDumpIn
    a2a: A2AIn
    context: ContextIn = Field(default_factory=ContextIn)


def _load_cfg() -> Any:
    """读框架配置；失败返回 ``(None, 错误文本)`` 而不是抛。

    PUT 需要区分「配置真的是默认值」和「配置没读出来」：后一种情况下前端表单里
    装的是默认值，此时允许保存就等于让默认值覆盖掉真实配置。
    """
    try:
        return load_config(), ""
    except Exception as e:  # noqa: BLE001 读不出来要在上层显式处理，不能让接口 500
        return None, str(e)


def _dump() -> Dict[str, Any]:
    """读两段配置 + 当前熔断状态（熔断是运行时状态，不落配置）。

    读不出配置时返回**默认值 + error 字段**——页面照样打得开，但调用方必须看
    ``error``：那时的值不是"真实配置"。
    """
    cfg, err = _load_cfg()
    if cfg is not None:
        return _dump_cfg(cfg)
    logger.warning("读取设置失败，返回默认值（真实配置未读到）: %s", err)
    return {
        "prompt_dump": asdict(PromptDumpSection()),
        "a2a": asdict(A2ASection()),
        "context": asdict(ContextSection()),
        "a2a_breakers": [],
        "error": err,
    }


def _dump_cfg(cfg: Any) -> Dict[str, Any]:
    """把已读到的配置对象摊平成响应体。"""
    try:
        pd = cfg.observability.prompt_dump
        a2a = cfg.a2a
        ctx = cfg.context
        return {
            "prompt_dump": {
                "enabled": pd.enabled,
                "dir": pd.dir,
                "min_prompt_tokens": pd.min_prompt_tokens,
                "retain_days": pd.retain_days,
            },
            "a2a": {
                "request_timeout": a2a.request_timeout,
                "task_timeout": a2a.task_timeout,
                "poll_interval": a2a.poll_interval,
                "cancel_on_timeout": a2a.cancel_on_timeout,
                "breaker_enabled": a2a.breaker_enabled,
                "breaker_threshold": a2a.breaker_threshold,
                "breaker_cooldown": a2a.breaker_cooldown,
            },
            "context": {
                "enabled": ctx.enabled,
                "externalize_min_chars": ctx.externalize_min_chars,
                "never_externalize": ctx.never_externalize,
                "retain_days": ctx.retain_days,
                "preview_head": ctx.preview_head,
                "preview_tail": ctx.preview_tail,
                "dir": ctx.dir,
                "compact_enabled": ctx.compact_enabled,
                "model_context_limit": ctx.model_context_limit,
                "max_output_tokens": ctx.max_output_tokens,
                "compact_ratio": ctx.compact_ratio,
                "compact_min_steps": ctx.compact_min_steps,
                "compact_min_ratio": ctx.compact_min_ratio,
                "compact_min_interval": ctx.compact_min_interval,
                "keep_recent_steps": ctx.keep_recent_steps,
                "compact_max_chars": ctx.compact_max_chars,
                "observation_limit": ctx.observation_limit,
                "max_steps": ctx.max_steps,
                "read_budget": ctx.read_budget,
                "max_ctx_recall": ctx.max_ctx_recall,
                "chars_per_token": ctx.chars_per_token,
                "pin_recent_reads": ctx.pin_recent_reads,
                "capability_preload": ctx.capability_preload,
                "cap_small_limit": ctx.cap_small_limit,
                "tool_group_min": ctx.tool_group_min,
                "tool_parallel": ctx.tool_parallel,
                "tool_parallel_max": ctx.tool_parallel_max,
            },
            "a2a_breakers": a2a_settings.breaker.snapshot(),
        }
    except Exception as e:  # noqa: BLE001
        # 读不出来（元配置缺失 / 用户设置文件不存在）就返回**内置默认值**，
        # 别让设置页打不开。默认值与未配置文件时的运行行为完全一致。
        return {
            "prompt_dump": asdict(PromptDumpSection()),
            "a2a": asdict(A2ASection()),
            "context": asdict(ContextSection()),
            "a2a_breakers": [],
            "error": str(e),
        }


@router.get("", response_model=Dict[str, Any])
async def get_settings() -> Dict[str, Any]:
    """读取当前设置（含 A2A 熔断状态）。"""
    return _dump()


@router.put("", response_model=Dict[str, Any])
async def update_settings(body: SettingsIn) -> Dict[str, Any]:
    """保存设置（立即生效，无需重启）。

    超时是每次调用现读的（A2A 配置 5 秒缓存、落盘开关同理），所以改完最多
    5 秒后生效；已在途的调用不受影响。

    **读不出当前配置时拒绝保存**：那时前端表单里装的是内置默认值，允许保存
    等于让默认值覆盖掉真实配置——实测就这么把落盘开关从 true 写成了 false。
    """
    cfg, err = _load_cfg()
    if cfg is None:
        raise HTTPException(
            status_code=409,
            detail=f"配置读取失败，已拒绝保存（避免用默认值覆盖真实配置）：{err}",
        )
    save_settings(
        prompt_dump=body.prompt_dump.model_dump(),
        a2a=body.a2a.model_dump(),
        context=body.context.model_dump(),
    )
    # 保存即生效：清配置缓存（prompt dump / A2A / 上下文），并把新阈值同步到熔断器
    invalidate_caches()
    try:
        from ..agent.context_store import invalidate_cfg_cache

        invalidate_cfg_cache()
    except Exception:  # noqa: BLE001
        pass
    a2a_settings.sync_from_config()
    return _dump()


@router.post("/reload", response_model=Dict[str, Any])
async def reload_settings() -> Dict[str, Any]:
    """重新加载设置（**手动改了 settings.yaml** 时用，等同于热重载）。

    设置页保存会自动让缓存失效；只有绕过设置页直接改文件时才需要点它。
    """
    invalidate_caches()
    a2a_settings.sync_from_config()
    return _dump()


class BreakerResetIn(BaseModel):
    """重置熔断：不传 peer 表示全部重置。"""

    peer: Optional[str] = None


@router.post("/a2a/breaker/reset", response_model=Dict[str, Any])
async def reset_breaker(body: BreakerResetIn) -> Dict[str, Any]:
    """手动解除熔断（对端已恢复时用，省得等冷却）。"""
    n = a2a_settings.breaker.reset(body.peer)
    return {"reset": n, **_dump()}
