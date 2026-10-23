"""LLM 客户端：最小接口 + 基于 langchain ChatModel 的实现 + 工厂。

Keeper 用 LLM 把 MCP 抽出的代码结构润色成记忆叙事；无 LLM 时降级为占位（纯索引模式）。
配置参数与 app/llm/config.py 的 LLMConfig 对齐：
    provider / model_name / api_key / base_url / temperature / timeout / max_retries
api_key / base_url 支持写成 ${ENV_VAR} 引用环境变量，避免密钥落到配置文件。
langchain 相关依赖惰性导入，未安装也能正常 import 本模块。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional, Protocol, Tuple

from .config import DEFAULT_MODELS, PROVIDER_MAP, LLMConfig

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"^\$\{(\w+)\}$")


@dataclass
class TokenUsage:
    """LLM 调用次数与 token 用量累计（跨多轮对话累加）。

    calls    : LLM 调用次数（无论 provider 是否返回用量都会累加）
    reported : 其中真正返回了 token 用量的调用次数
               为 0 说明该 provider 不上报用量，只能看调用次数
    cached_tokens      : 命中缓存的输入 token 累计
    cache_write_tokens : 写入缓存的输入 token 累计（Anthropic 有，DeepSeek 一般无）

    本类是**累计值**，只能表达「上报了 0」；「provider 未上报」的区分在单次调用
    层面由 ``_extract_usage`` 返回 None 表达（落库表 ``llm_calls.cached_tokens``
    可空正是为此）。
    """

    calls: int = 0
    reported: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率（命中占输入的比例）；无输入时为 0。"""
        if not self.prompt_tokens:
            return 0.0
        return self.cached_tokens / self.prompt_tokens

    def add(
        self,
        prompt: int,
        completion: int,
        cached: Optional[int] = None,
        cache_write: Optional[int] = None,
        reasoning: Optional[int] = None,
    ) -> None:
        self.calls += 1
        if prompt or completion:
            self.reported += 1
            self.prompt_tokens += prompt
            self.completion_tokens += completion
        if cached:
            self.cached_tokens += cached
        if cache_write:
            self.cache_write_tokens += cache_write
        if reasoning:
            self.reasoning_tokens += reasoning

    def copy(self) -> "TokenUsage":
        return TokenUsage(
            self.calls,
            self.reported,
            self.prompt_tokens,
            self.completion_tokens,
            self.cached_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )

    def __sub__(self, other: "TokenUsage") -> "TokenUsage":
        """与某个历史快照做差，得到「这段时间内的增量」。"""
        return TokenUsage(
            self.calls - other.calls,
            self.reported - other.reported,
            self.prompt_tokens - other.prompt_tokens,
            self.completion_tokens - other.completion_tokens,
            self.cached_tokens - other.cached_tokens,
            self.cache_write_tokens - other.cache_write_tokens,
            self.reasoning_tokens - other.reasoning_tokens,
        )


def _tool_args_chars(resp: Any) -> int:
    """模型以 tool_calls 输出时，其入参（如写入的游戏代码）的累计字符数。

    这些字符是**模型生成**的，应计入 completion_tokens。单独统计出来用于和
    usage 对照：若「工具入参 17889 字符」却只记了 60 completion token，
    就说明 provider/框架没把 tool_calls 的 token 算进 completion（漏记）。
    """
    total = 0
    for tc in getattr(resp, "tool_calls", None) or []:
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
        if args is None:
            continue
        try:
            total += len(json.dumps(args, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            total += len(str(args))
    return total


def _extract_usage(
    resp: Any,
) -> Tuple[int, int, Optional[int], Optional[int], Optional[int]]:
    """从 LangChain AIMessage 里取 ``(prompt, completion, cached, cache_write, reasoning)``。

    ``cached`` / ``cache_write`` / ``reasoning`` 为 ``None`` 表示 **provider 未上报**，
    ``0`` 表示「上报了但确实是 0」——必须区分，否则会把「没统计到」误读成
    「没命中」。

    ``reasoning`` 是**思维链 token**，是 ``completion`` 的**子集**（包含在输出 token
    内，不另计入 total），只有 reasoning 模型（o1/o3/deepseek-reasoner）才会非 0。

    不同 provider / 版本位置不一样，兼容：
    - `usage_metadata`（langchain-core 新标准；缓存在其
      `input_token_details.cache_read` / `cache_creation`）
    - `response_metadata.token_usage`（OpenAI 兼容，含 deepseek）
    - `response_metadata.usage`

    DeepSeek 走 OpenAI 兼容端点，缓存字段是**非标准**的
    ``prompt_cache_hit_tokens``（及其对应 miss），需单独探测；且
    ``prompt_tokens = hit + miss``，即命中部分已包含在 prompt_tokens 内。
    """
    prompt = completion = 0
    # 缓存命中 / 写入：收集所有来源里的非 None 值再取 max——避免某个字段报 0
    # 把另一个字段里的真实命中覆盖掉（比如 DeepSeek 在 prompt_cache_hit_tokens
    # 报命中、但 input_tokens_details.cached_tokens 报 0 的情况）。
    # 全部为 None => 未上报；全部为 0 => 真·没命中；有正值 => 取最大命中数。
    cache_candidates: list[int] = []
    cache_write_candidates: list[int] = []
    # 思维链 token（reasoning 模型才有）：同样收集所有来源取 max，是 completion
    # 的子集，不计入 total。None=未上报，0=非 reasoning 模型，正值=思考 token 数。
    reasoning_candidates: list[int] = []

    def _pick(d: dict, *keys: str) -> Optional[int]:
        for k in keys:
            v = d.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return None

    um = getattr(resp, "usage_metadata", None)
    if isinstance(um, dict):
        prompt = int(um.get("input_tokens") or 0)
        completion = int(um.get("output_tokens") or 0)
        # langchain 标准：input_token_details.cache_read / cache_creation。
        # 不同版本 key 写法有出入（也有 input_tokens_details / cached_tokens），多探几种
        for key in ("input_token_details", "input_tokens_details"):
            details = um.get(key)
            if isinstance(details, dict):
                c = _pick(details, "cache_read", "cached_tokens")
                if c is not None:
                    cache_candidates.append(c)
                cw = _pick(details, "cache_creation")
                if cw is not None:
                    cache_write_candidates.append(cw)
                break
        # 思维链 token：usage_metadata 标准位置是 output_token_details.reasoning
        for key in ("output_token_details", "output_tokens_details"):
            details = um.get(key)
            if isinstance(details, dict):
                r = _pick(details, "reasoning_tokens", "reasoning")
                if r is not None:
                    reasoning_candidates.append(r)
                break

    # 不能在这里 return：usage_metadata 里读不到 DeepSeek 的缓存命中
    # （它放在 response_metadata.token_usage.prompt_cache_hit_tokens），
    # OpenAI 某些版本也只在 response_metadata 上报——统一在下方兜底，
    # 否则 DeepSeek 缓存永远解析不到、表现为恒 0 / 未上报。
    rm = getattr(resp, "response_metadata", None)
    if isinstance(rm, dict):
        tu = rm.get("token_usage") or rm.get("usage") or {}
        if isinstance(tu, dict):
            # 没有 usage_metadata 时，prompt/completion 也得从这里补
            if not isinstance(um, dict):
                prompt = int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                completion = int(
                    tu.get("completion_tokens") or tu.get("output_tokens") or 0
                )
            # OpenAI 的缓存明细有**两种**写法，都要认：
            # - Chat Completions：prompt_tokens_details.cached_tokens
            # - Responses API   ：input_tokens_details.cached_tokens
            for key in ("input_tokens_details", "prompt_tokens_details"):
                d = tu.get(key)
                if isinstance(d, dict):
                    c = _pick(d, "cached_tokens")
                    if c is not None:
                        cache_candidates.append(c)
            # DeepSeek 特有（OpenAI 兼容端点下的非标准字段）
            c = _pick(tu, "prompt_cache_hit_tokens")
            if c is not None:
                cache_candidates.append(c)
            cw = _pick(tu, "cache_creation_input_tokens", "cache_write_tokens")
            if cw is not None:
                cache_write_candidates.append(cw)
            # 思维链 token：Chat Completions 在 completion_tokens_details.reasoning_tokens；
            # Responses API 在 output_tokens_details.reasoning_tokens
            for key in (
                "completion_tokens_details",
                "output_tokens_details",
                "output_token_details",
            ):
                d = tu.get(key)
                if isinstance(d, dict):
                    r = _pick(d, "reasoning_tokens", "reasoning")
                    if r is not None:
                        reasoning_candidates.append(r)
                    break

    cached = max(cache_candidates) if cache_candidates else None
    cache_write = max(cache_write_candidates) if cache_write_candidates else None
    reasoning = max(reasoning_candidates) if reasoning_candidates else None

    # 调试：原样打印模型返回的 usage，确认缓存字段（prompt_cache_hit_tokens /
    # input_token_details.cache_read 等）是否真的被返回、值是多少。
    # 缓存恒为 0% 或 token 统计对不上时，优先看这条日志里的 raw token_usage。
    try:
        logger.info(
            "[llm-usage] raw usage_metadata=%s | token_usage=%s | "
            "-> prompt=%s completion=%s cached=%s cache_write=%s reasoning=%s",
            getattr(resp, "usage_metadata", None),
            (rm.get("token_usage") or rm.get("usage") if isinstance(rm, dict) else None),
            prompt, completion, cached, cache_write, reasoning,
        )
    except Exception:
        pass
    return prompt, completion, cached, cache_write, reasoning


def _chunk_text(content: Any) -> str:
    """把消息内容压成纯文本（LangChain v1 的 content 可能是内容块列表）。

    流式分片与一次性响应都用它：保证「输出多少字符 / 落盘看到什么」不会因为
    content 是 list 而变成 ``['...']`` 这种 JSON 数组。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text")
                parts.append(str(t) if t else json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "".join(parts)
    return str(content)


class LLM(Protocol):
    """LLM 最小接口（用于润色方法/链路/业务叙事，以及 agent 自主规划多轮对话）。"""

    async def complete(self, prompt: str) -> str:
        ...

    async def chat(
        self, messages: list[dict], system: str = ""
    ) -> str:
        """多轮对话：messages 为 [{"role": "...", "content": "..."}]，
        role 支持 system / user / assistant / tool。供 processor 自主规划循环回传
        observation（工具结果）使用。不强制依赖原生 tool-calling，所有 chat 模型通用。
        """
        ...

    async def astream_chat(
        self, messages: list[dict], system: str = ""
    ) -> AsyncIterator[str]:
        """流式多轮对话：逐块产出文本增量（可选能力，用于前端打字机效果）。

        不支持流式的实现可以不提供该方法——调用方用 ``getattr(llm, "astream_chat", None)``
        判断，拿不到就退回一次性 ``chat``。
        """
        ...


def _has_file_blocks(msgs: list) -> bool:
    """这批发给模型的消息里有没有 file 附件块。

    只看 ``HumanMessage`` 且 content 是列表的那种：附件只可能出现在第一轮
    user 消息上（chat/service.py 注入），后续 ReAct 轮次都是纯文本。
    """
    for m in msgs or []:
        content = getattr(m, "content", None)
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "file":
                return True
    return False


def _lc_role_to_openai(m: Any) -> str:
    """langchain 消息类型 → OpenAI role。"""
    t = getattr(m, "type", None) or m.__class__.__name__.lower()
    return {"human": "user", "ai": "assistant", "system": "system"}.get(t, t)


class LangChainLLM:
    """用 langchain ChatModel 实现的 LLM 封装。"""

    def __init__(
        self,
        model: Any,
        system: str = "",
        *,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        profile_id: Optional[str] = None,
        cfg: Optional["LLMConfig"] = None,
    ):
        self._model = model
        self._system = system
        # 原始配置：只在「带附件的轮次绕开 langchain 直连」时用得到（见
        # _direct_chat）。不从 self._model 反查 api_key/base_url——那些是
        # langchain-openai 的私有属性，版本一升级就可能改名。
        self.cfg = cfg
        # 可观测：记录用量时要标明走的哪个模型 / provider / 预设——成本换算靠
        # 它们去 llm_profiles 匹配单价（见 doc/observability-design.md）。
        self.provider = provider
        self.model_name = (
            model_name
            or getattr(model, "model_name", None)
            or getattr(model, "model", None)
        )
        self.profile_id = profile_id
        # token 用量累计（跨轮累加）；agent 每轮结束后据此打印本轮 + 累计
        self.usage = TokenUsage()
        # provider 不接受输出上限参数时置位：降级一次就别再试了（见 _ainvoke）
        self._no_output_cap = False
        # 同理，provider 不接受思考参数（reasoning_effort / thinking）时置位
        self._no_reasoning = False

    async def _record_usage(
        self,
        resp: Any,
        *,
        duration_ms: Optional[int] = None,
        ttft_ms: Optional[int] = None,
        is_stream: bool = False,
        kind: str = "other",
        # 仅用于「规模 vs usage」对照日志，不参与记账
        messages: Optional[list] = None,
        out_chars: Optional[int] = None,
        tool_args_chars: Optional[int] = None,
        # 落盘用：流式时 resp 只是某个分片（content 常为空），必须用累积的正文
        resp_text: Optional[str] = None,
        tool_args_text: Optional[str] = None,
    ) -> None:
        """记录一次调用的用量：累加到实例 usage，并落一条 ``llm_calls``。

        provider 不上报时只累加调用次数；落库失败只记 debug 日志——
        **统计绝不能影响主流程**。
        """
        try:
            prompt, completion, cached, cache_write, reasoning = _extract_usage(resp)
        except Exception as e:  # 用量统计失败不能影响主流程
            logger.debug("解析 token 用量失败: %s", e)
            return
        self.usage.add(prompt, completion, cached, cache_write, reasoning)
        # 诊断：流式调用却没抓到任何 token（usage 为 0/0），通常是 stream_usage
        # 未生效（未重启后端 / langchain-openai 版本未转发该参数）。该调用的 token
        # 会漏记，导致「后台几十万、keeper 接近 0」。明确告警而非静默丢失。
        if is_stream and not prompt and not completion:
            logger.warning(
                "[llm-usage] 流式调用未抓到 token（prompt=0 completion=0），"
                "疑似 stream_usage 未生效：请确认已重启后端；若已重启仍如此，"
                "说明该 langchain-openai 版本未把 stream_usage 转发给 SDK，需改用 "
                "model_kwargs 的 stream_options 兜底。"
            )
        # 对照日志：把这次调用的「输入 / 输出规模」与 usage 打到同一行，
        # 用于核对 token 统计是否合理（如输出上万字符却只记几十 token）。
        try:
            in_chars = sum(
                len(str(getattr(m, "content", "") or "")) for m in (messages or [])
            )
            logger.info(
                "[llm-usage] 输入 %s 字符（%s 条消息）| 输出 %s 字符%s | "
                "-> prompt=%s completion=%s cached=%s cache_write=%s reasoning=%s",
                in_chars,
                len(messages or []),
                out_chars if out_chars is not None else "?",
                f"（工具入参 {tool_args_chars} 字符）" if tool_args_chars else "",
                prompt,
                completion,
                cached,
                cache_write,
                reasoning,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..observability import record_llm_call

            await record_llm_call(
                self,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                cache_write_tokens=cache_write,
                reasoning_tokens=reasoning,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                is_stream=is_stream,
                kind=kind,
            )
        except Exception as e:  # 记账失败同样不能影响主流程
            logger.debug("落库 LLM 用量失败: %s", e)

        # 调试开关：完整 prompt 落盘（默认关，见 config.yaml observability.prompt_dump）。
        # **必须丢到后台线程**：写几万字的文件 + 读配置都是同步 IO，直接在事件
        # 循环里做会阻塞整个进程（表现为请求卡住不返回）。to_thread 会复制
        # contextvars，所以归属信息（session / message / step）仍能读到。
        try:
            from ..observability import dump_llm_call

            await asyncio.to_thread(
                dump_llm_call,
                msgs=messages,
                resp=resp,
                usage=(prompt, completion, cached, cache_write, reasoning),
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                is_stream=is_stream,
                model=getattr(self, "model_name", None),
                resp_text=resp_text,
                tool_args_text=tool_args_text,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("prompt dump 失败: %s", e)

    async def complete(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        msgs = []
        if self._system:
            msgs.append(SystemMessage(content=self._system))
        msgs.append(HumanMessage(content=prompt))
        t0 = time.perf_counter()
        resp = await self._model.ainvoke(msgs)
        await self._record_usage(
            resp,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            messages=msgs,
            out_chars=len(_chunk_text(getattr(resp, "content", ""))),
            resp_text=_chunk_text(getattr(resp, "content", "")),
        )
        return getattr(resp, "content", str(resp))

    def _build_messages(self, messages: list[dict], system: str = "") -> list:
        """把 [{"role","content","name"}] 拼成 langchain 消息（一次性与流式共用）。

        tool 结果不依赖原生 tool-calling，统一视为带前缀的 HumanMessage，
        对任意 chat 模型都兼容（ReAct 文本协议）。
        """
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        sys = system or self._system
        msgs = [SystemMessage(content=sys)] if sys else []
        for m in messages:
            role = (m.get("role") or "user").lower()
            content = m.get("content", "")
            name = m.get("name")
            if role == "system":
                msgs.append(SystemMessage(content=content))
            elif role in ("assistant", "ai"):
                msgs.append(AIMessage(content=content))
            elif role == "tool":
                tag = f"[工具 {name} 返回结果]\n" if name else "[工具返回结果]\n"
                msgs.append(HumanMessage(content=tag + content))
            else:
                msgs.append(HumanMessage(content=content))
        return msgs

    # ---- 输出上限（context.max_output_tokens）----
    # 为什么在**调用时**传，而不是 build_model 时写进 ChatOpenAI：
    #   模型实例是 agent 启动时建一次就复用的（keeper.py 里 get_llm 只在 build /
    #   重连时调），写死在实例上就意味着「改完设置要重启才生效」，而设置页承诺
    #   的是立即生效。ctx_cfg 带 TTL + 文件指纹，改完几秒内自动跟上（保存时
    #   还会主动失效缓存）。
    def _output_kwargs(self) -> Dict[str, Any]:
        """本次调用要附加的输出上限参数；未配置返回空 dict。

        只在 chat / astream_chat 上加，**不**动 complete()：complete 是记忆润色、
        压缩摘要这类短文本，截断它们只会留下半句话的坏数据；而 ReAct 的
        ACTION_INPUT（整份文件 / 代码塞进参数）才是真正会撞上限的那一处。
        """
        if self._no_output_cap:
            return {}
        try:
            from ..agent.context_store import ctx_cfg

            n = int(getattr(ctx_cfg(), "max_output_tokens", 0) or 0)
        except Exception as e:  # noqa: BLE001 配置读不到就按「不限制」处理
            logger.debug("读取 max_output_tokens 失败，按不限制处理: %s", e)
            return {}
        return {"max_tokens": n} if n > 0 else {}

    def _thinking_kwargs(self) -> Dict[str, Any]:
        """本次调用要附加的思考参数（reasoning_effort + thinking）；未开启返回空 dict。

        读 ContextVar（chat.context.current_thinking），与 _output_kwargs 同一思路：
        思考是**会话级**的，而模型实例跨会话复用，所以只能调用时按当前请求注入。
        DeepSeek 风格：thinking 开启 → ``extra_body={"thinking": {"type": "enabled"}}``；
        ``reasoning_effort``（low/high/max）作为顶层参数透传。
        """
        if self._no_reasoning:
            return {}
        try:
            from ..chat.context import current_thinking

            t = current_thinking()
        except Exception as e:  # noqa: BLE001 读不到就按不启用处理
            logger.debug("读取思考模式失败，按不启用处理: %s", e)
            return {}
        if t is None or not t.enabled:
            return {}
        kw: Dict[str, Any] = {"extra_body": {"thinking": {"type": "enabled"}}}
        if t.effort:
            kw["reasoning_effort"] = t.effort
        return kw

    async def _ainvoke(self, msgs: list[Any]) -> Any:
        """ainvoke + 输出上限；provider 不认这个参数时退回不带参数的调用。

        参数名各 provider 不统一（OpenAI 系是 ``max_tokens``，Ollama 是
        ``num_predict``），所以必须能失败降级——不能为了一个上限把整轮对话搞挂。
        """
        kw = {**self._output_kwargs(), **self._thinking_kwargs()}
        if kw:
            try:
                return await self._model.ainvoke(msgs, **kw)
            except (TypeError, ValueError) as e:
                # 只在第一次降级时告警，否则每步都刷日志。两个附加参数一起降级：
                # 拆开重试会多打一次 API，而它们本就是"锦上添花"，一起退更稳妥。
                logger.warning(
                    "[llm] 当前 provider 不接受附加参数（输出上限/思考，%s），"
                    "本次起不再下发这些参数", e,
                )
                self._no_output_cap = True
                self._no_reasoning = True
        return await self._model.ainvoke(msgs)

    def _direct_client(self):
        """附件轮专用的 openai 客户端。cfg 缺失时抛异常（说明装配路径漏传了）。"""
        if self.cfg is None:
            raise RuntimeError("带附件的轮次需要原始 LLM 配置（cfg），但构造时没传进来")
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url,
            timeout=self.cfg.timeout,
            max_retries=self.cfg.max_retries,
            # 绕开 httpx2 + 老 brotli 的解压崩溃（见 _http_default_headers）
            default_headers=_http_default_headers(),
        )

    def _direct_payload(self, msgs: list) -> list:
        """langchain 消息 → OpenAI messages，content **原样透传**。

        不经过 ``_format_message_content``：那样会把 file 块改写成 DeepSeek 不
        认识的嵌套形状（见 _direct_chat_stream 的说明）。
        """
        return [
            {"role": _lc_role_to_openai(m), "content": getattr(m, "content", "")}
            for m in msgs
        ]

    async def _direct_chat_once(self, msgs: list) -> str:
        """带附件的轮次：绕开 langchain，一次性拿完整回答。

        为什么必须绕
        ------------
        langchain-openai 会把 content 里的 file 块一律标准化成 OpenAI 的**嵌套**
        格式（``_format_message_content`` → ``convert_to_openai_data_block``）::

            {"type": "file", "file_id": "x"}  →  {"type": "file", "file": {"file_id": "x"}}

        而 DeepSeek 的 Files API 要的是**扁平**格式（见其文档的示例）。它收到
        嵌套时报::

            .messages[1]: file must have a file_id or file_data

        ——因为它在**顶层**找那两个键，嵌套把它们藏进 ``file`` 里了。这是两家厂商
        的真实差异（OpenAI 文档嵌套、DeepSeek 扁平），langchain 只实现前者且没有
        开关能关。改不了它，就只能绕开。

        只在有附件的轮次走这条路：其余调用继续用 langchain（流式降级、usage
        细分、输出上限那些逻辑都在那边，没必要为附件复制一遍）。
        """
        client = self._direct_client()
        import time as _t

        t0 = _t.perf_counter()
        resp = await client.chat.completions.create(
            model=self.cfg.model_name,
            messages=self._direct_payload(msgs),
            **self._output_kwargs(),
            **self._thinking_kwargs(),
        )
        u = getattr(resp, "usage", None)
        if u:
            self.usage.add(u.prompt_tokens or 0, u.completion_tokens or 0)
        duration_ms = int((_t.perf_counter() - t0) * 1000)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        # 带附件的一次性路径也必须落用量 + dump，否则「查看详情 / dump」整段缺失
        # （注释见 _direct_chat_stream）。dump 用原始 langchain msgs（含 SystemMessage），
        # 不能用 _direct_payload 的纯 dict。裸 OpenAI resp 没有 usage_metadata，
        # 所以照流式路径合成 usage_metadata 再喂给 _record_usage。
        try:
            um: dict = {}
            if u is not None:
                um["input_tokens"] = int(getattr(u, "prompt_tokens", 0) or 0)
                um["output_tokens"] = int(getattr(u, "completion_tokens", 0) or 0)
            from types import SimpleNamespace

            synth_resp = SimpleNamespace(usage_metadata=um)
            await self._record_usage(
                synth_resp,
                duration_ms=duration_ms,
                is_stream=False,
                kind="other",
                messages=msgs,
                out_chars=len(text or ""),
                tool_args_chars=0,
                resp_text=text or "",
                tool_args_text="",
            )
        except Exception as e:  # noqa: BLE001 记账绝不能影响主流程
            logger.debug("带附件一次性落盘用量失败: %s", e)
        return text

    async def _direct_chat_stream(self, msgs: list):
        """同上，但流式逐块 yield。与一次性那条分开写，不是为了复用：

        Python 的 async generator 不能 ``return value``，混在一个函数里会直接
        SyntaxError。拆开是语言层面的必然，不是风格选择。

        注意：这条路径**必须**在收尾时调一次 ``_record_usage`` 落用量 + 落盘——
        否则带附件的轮次（网页自动化每步都带截图）既不会写 ``llm_calls`` 行、
        也不会落 dump 文件，导致「查看详情 / 查看 dump」只剩汇总、看不到这一步。
        """
        import time as _time

        from types import SimpleNamespace

        client = self._direct_client()
        resp = await client.chat.completions.create(
            model=self.cfg.model_name,
            messages=self._direct_payload(msgs),
            stream=True,
            **self._output_kwargs(),
            **self._thinking_kwargs(),
        )
        t0 = _time.perf_counter()
        ttft_ms = None
        out_parts: list = []
        out_chars = 0
        last_usage = None
        n_chunks = 0
        first_chunk = None
        last_finish = None
        async for chunk in resp:
            n_chunks += 1
            if first_chunk is None:
                first_chunk = chunk
            # usage 可能落在倒数第二个分片（DeepSeek 的已知行为，见 astream_chat）
            u = getattr(chunk, "usage", None)
            if u is not None:
                last_usage = u
                self.usage.add(u.prompt_tokens or 0, u.completion_tokens or 0)
            # API 层错误（如余额/权限/参数）常以 chunk.error 形式返回，且 content 为空；
            # 不单独处理就会被下面「content 为空就不 yield」吞掉，表现为「模型返回空」。
            err = getattr(chunk, "error", None)
            if err is not None:
                logger.warning("[AUTOMATION] 带附件流式 chunk 报错: %r", err)
            for choice in chunk.choices or []:
                delta = getattr(choice, "delta", None)
                piece = getattr(delta, "content", None) if delta else None
                fr = getattr(choice, "finish_reason", None)
                if fr is not None:
                    last_finish = fr
                if piece:
                    if ttft_ms is None:
                        ttft_ms = int((_time.perf_counter() - t0) * 1000)
                    out_parts.append(piece)
                    out_chars += len(piece)
                    yield piece
        duration_ms = int((_time.perf_counter() - t0) * 1000)
        if out_chars == 0:
            logger.warning(
                "[AUTOMATION] 带附件流式返回空文本！n_chunks=%d finish_reason=%r first_chunk=%r",
                n_chunks, last_finish, first_chunk,
            )
        # 把 OpenAI 的 usage 转成 _extract_usage 认的 usage_metadata，再落盘 + 记账。
        um: dict = {}
        if last_usage is not None:
            um["input_tokens"] = int(getattr(last_usage, "prompt_tokens", 0) or 0)
            um["output_tokens"] = int(getattr(last_usage, "completion_tokens", 0) or 0)
            ptd = getattr(last_usage, "prompt_tokens_details", None)
            if ptd is not None:
                cr = getattr(ptd, "cache_read", None) or getattr(ptd, "cached_tokens", None)
                if cr is not None:
                    um["input_token_details"] = {"cache_read": int(cr)}
            ctd = getattr(last_usage, "completion_tokens_details", None)
            if ctd is not None:
                rs = getattr(ctd, "reasoning_tokens", None)
                if rs is not None:
                    um["output_token_details"] = {"reasoning": int(rs)}
            # DeepSeek 扁平字段兜底
            hit = getattr(last_usage, "prompt_cache_hit_tokens", None)
            if hit is not None:
                um.setdefault("input_token_details", {})["cache_read"] = int(hit)
        synth_resp = SimpleNamespace(usage_metadata=um)
        try:
            await self._record_usage(
                synth_resp,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms if ttft_ms is not None else duration_ms,
                is_stream=True,
                kind="other",
                # 注意：dump 必须吃**原始 langchain msgs**（含 SystemMessage 实例），
                # 不能用 self._direct_payload(msgs)——那是喂给 API 的纯 dict（role 是
                # 字符串、SystemMessage 被展平成 {"role":"system"}），dump_llm_call 的
                # _system_of 只认 SystemMessage 实例，吃 dict 会得到空 system；且消息
                # 角色也会错乱。网页 Agent 每步都带截图附件走这条直连路径，所以只有
                # 它的 dump 会变成「system + messages 全空」，普通对话（langchain 流式
                # 路径）不受影响。API 调用那边仍用 _direct_payload(msgs)（见上）。
                messages=msgs,
                out_chars=out_chars,
                tool_args_chars=0,
                resp_text="".join(out_parts),
                tool_args_text="",
            )
        except Exception as e:  # noqa: BLE001 记账绝不能影响主流程
            logger.debug("带附件流式落盘用量失败: %s", e)

    async def chat(self, messages: list[dict], system: str = "") -> str:
        t0 = time.perf_counter()
        msgs = self._build_messages(messages, system)
        # 带附件的轮次走直连（见 _direct_chat 的理由：langchain 会把 file 块
        # 改写成 DeepSeek 不认识的形状）。没有附件时一切照旧。
        if _has_file_blocks(msgs):
            return await self._direct_chat_once(msgs)
        resp = await self._ainvoke(msgs)
        await self._record_usage(
            resp,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            messages=msgs,
            out_chars=len(_chunk_text(getattr(resp, "content", ""))),
            tool_args_chars=_tool_args_chars(resp),
            resp_text=_chunk_text(getattr(resp, "content", "")),
        )
        return getattr(resp, "content", str(resp))

    async def astream_chat(
        self, messages: list[dict], system: str = ""
    ) -> AsyncIterator[str]:
        """流式多轮对话：逐块 yield 文本增量。

        模型 / 封装不支持 astream 时降级为一次性产出整段——流式只是体验增强，
        不该让整轮对话失败。
        """
        msgs = self._build_messages(messages, system)
        t0 = time.perf_counter()
        ttft_ms: Optional[int] = None
        # 带附件：绕开 langchain 直连（见 _direct_chat 的说明）
        if _has_file_blocks(msgs):
            async for piece in self._direct_chat_stream(msgs):
                yield piece
            return
        try:
            last = None
            # 带 usage_metadata 的分片。**不能只取最后一个 chunk**：实测 DeepSeek
            # 的 usage 常落在**倒数第二个**分片上，最后一个只是空收尾分片
            # （content 空、无 usage），只取 last 会把整次调用的 token 漏记为 0。
            usage_msg = None
            out_chars = 0
            tool_args_chars = 0
            n_chunks = 0
            # 累积正文与工具入参：流式下单个分片的 content 只是「一段增量」，
            # 落盘与用量记录都必须用拼接后的完整内容（否则 dump 里响应是空的）。
            out_parts: list = []
            tool_args_parts: list = []
            # 输出上限同样下发到流式：不加的话前端打字机这条主路径完全不受限，
            # 而它恰恰是最容易写出超长 ACTION_INPUT 的那条（要边生成边显示）。
            # 参数不被接受时下面的 except 会兜住，降级成一次性调用。
            kw = {**self._output_kwargs(), **self._thinking_kwargs()}
            async for chunk in self._model.astream(msgs, **kw):
                if ttft_ms is None:  # 首字延迟：第一个 chunk 到达的时间
                    ttft_ms = int((time.perf_counter() - t0) * 1000)
                n_chunks += 1
                last = chunk
                content = getattr(chunk, "content", "")
                if content:
                    if isinstance(content, str):
                        out_parts.append(content)
                        out_chars += len(content)
                    else:  # v1 的内容块列表：压成文本，保证落盘可读
                        s = _chunk_text(content)
                        out_parts.append(s)
                        out_chars += len(s)
                    yield content
                # 逐块找 usage，命中就记录（取最后一个带 usage 的分片：
                # 多个分片都带时它是最终累计值；不做 + 合并，避免重复累加）。
                if getattr(chunk, "usage_metadata", None):
                    usage_msg = chunk
                # 工具入参是**增量**到达的（tool_call_chunks），必须逐块累计：
                # 「写 17889 字符游戏」这类输出的大头就在工具入参里，不累计就会低估
                for tcc in getattr(chunk, "tool_call_chunks", None) or []:
                    a = (
                        tcc.get("args")
                        if isinstance(tcc, dict)
                        else getattr(tcc, "args", None)
                    )
                    if a:
                        tool_args_chars += len(a)
                        tool_args_parts.append(a)
            # 流式**成功**路径原先漏了用量记录（既有 bug）：只在异常回退到
            # ainvoke 时才记，导致前端打字机这条主要路径的用量恒为 0。
            # 用量优先取「带 usage_metadata 的分片」，取不到才退回最后一个分片
            # （此时只累加调用次数，不臆造 token 数）。
            logger.info(
                "[dbg] 流式结束：chunks=%s out_chars=%s 有usage分片=%s",
                n_chunks,
                out_chars,
                usage_msg is not None,
            )
            target = usage_msg if usage_msg is not None else last
            if target is not None:
                await self._record_usage(
                    target,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    ttft_ms=ttft_ms,
                    is_stream=True,
                    messages=msgs,
                    out_chars=out_chars,
                    tool_args_chars=tool_args_chars,
                    resp_text="".join(out_parts),
                    tool_args_text="".join(tool_args_parts),
                )
            logger.info("[dbg] 用量记录完成，astream_chat 退出")
        except Exception as e:
            logger.warning("LLM 流式失败，退回一次性调用: %s", e)
            # 走 _ainvoke：输出上限仍然生效（流式那次失败可能只是 provider
            # 不接受该参数，不能因此让降级调用也变成无限制）
            resp = await self._ainvoke(msgs)
            await self._record_usage(
                resp,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            messages=msgs,
            out_chars=len(_chunk_text(getattr(resp, "content", ""))),
            tool_args_chars=_tool_args_chars(resp),
            resp_text=_chunk_text(getattr(resp, "content", "")),
        )
            text = getattr(resp, "content", str(resp))
            if text:
                yield text


def _expand_env(value: Optional[str]) -> Optional[str]:
    """把 ${VAR} 展开为环境变量（取不到则 None），其它值原样返回。"""
    if not isinstance(value, str):
        return value
    m = _ENV_PATTERN.match(value.strip())
    if m:
        return os.getenv(m.group(1)) or None
    return value


# ── 请求头：绕开 httpx2 + 老 brotli 的解压崩溃 ─────────────────────────────
#
# 症状：所有 LLM 调用报 ``APIConnectionError: Connection error``，但网络是通的
# （同一个 URL 用 httpx 直连能拿到 401）。真实报错在更下面：
#
#   httpx2/_decoders.py → self._decompress(data, output_buffer_limit=...)
#   TypeError: process() takes no keyword arguments
#
# 原因：httpx2 2.12 的 BrotliDecoder 走官方 brotli 的 ``Decompressor.process``，
# 并给它传 ``output_buffer_limit`` 关键字——而官方 brotli（1.0.9）的
# ``process()`` 只接受一个参数。httpx2 的默认 Accept-Encoding 里带 br
# （装了 brotli 就会带），于是**读响应体的那一步**就炸，所有请求全军覆没。
#
# 绕过：请求头里只要 gzip / deflate，服务端就不会回 br，httpx2 改走 gzip 解码器。
# 代价是失去 br 的压缩率——LLM 响应本来就不大，可以忽略。
#
# 为什么要探测而不是无条件加：brotli / httpx2 修好之后这段就该自动失效，
# 不然白丢压缩率。探测方式是「空数据试调一次」，安全且确定。

_HTTP_HEADERS_CACHE: Dict[str, bool] = {}


def _brotli_incompatible() -> bool:
    """当前环境的 brotli 是否与 httpx2 2.12 的解压调用不兼容。

    判据只有一个：``Decompressor.process`` 收不收 ``output_buffer_limit``。
    收不了就是会崩（httpx2 就是这么调的）。
    """
    cached = _HTTP_HEADERS_CACHE.get("brotli")
    if cached is not None:
        return cached
    result = False
    try:
        import brotli  # type: ignore[import-not-found]

        brotli.Decompressor().process(b"", output_buffer_limit=1024)
    except ImportError:
        result = False  # 没装 brotli，httpx2 根本不会用 br
    except TypeError:
        result = True
    except Exception:  # noqa: BLE001 其它异常按「兼容」处理，别挡路
        result = False
    _HTTP_HEADERS_CACHE["brotli"] = result
    return result


def _http_default_headers() -> Dict[str, str]:
    """LLM 客户端要带的额外请求头（目前只有绕 brotli 崩溃那一项）。

    环境正常时返回空 dict——不往请求里塞任何多余的东西。
    """
    if not _brotli_incompatible():
        return {}
    return {"Accept-Encoding": "gzip, deflate"}


def build_model(cfg: LLMConfig) -> Any:
    """根据 LLMConfig 现场构建一个 ChatModel 实例（用完即弃，不缓存）。"""
    from langchain.chat_models import init_chat_model

    provider = PROVIDER_MAP.get(cfg.provider, cfg.provider)
    kwargs: Dict[str, Any] = {
        "model": cfg.model_name,
        "model_provider": provider,
        "temperature": cfg.temperature,
        "max_retries": cfg.max_retries,
    }
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    if cfg.timeout:
        kwargs["timeout"] = cfg.timeout

    # 让**流式**也能拿到 token 用量。OpenAI 兼容端点（openai / deepseek / siliconflow）
    # 必须在模型上显式开 stream_usage，否则 astream 产出的 chunk 不带 usage_metadata，
    # 流式调用的 token 用量就统计不到（只能记调用次数）——这正是「后台几十万、
    # keeper 接近 0」的根因。
    # 坑：init_chat_model(..., stream_usage=True) 经常把这个参数静默丢弃
    # （构造函数不支持时走 except 分支被 pop 掉），所以 openai 系**直接构造
    # ChatOpenAI** 并显式 stream_usage=True，确保一定生效。
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=cfg.model_name,
            temperature=cfg.temperature,
            max_retries=cfg.max_retries,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            stream_usage=True,
            # 绕开 httpx2 + 老 brotli 的解压崩溃（见 _http_default_headers）
            default_headers=_http_default_headers(),
        )

    # 其余 provider 仍走 init_chat_model，并尽量开启 stream_usage（不支持会回退）。
    kwargs["stream_usage"] = True
    # default_headers 同理：支持的 provider 会带上，不支持的下面会去掉。
    headers = _http_default_headers()
    if headers:
        kwargs["default_headers"] = headers
    try:
        return init_chat_model(**kwargs)
    except TypeError:
        kwargs.pop("stream_usage", None)
        try:
            return init_chat_model(**kwargs)
        except TypeError:
            # 构造器不认 default_headers（部分 provider）→ 去掉再试。
            # 少了这个头顶多是又碰上 brotli 那个崩溃，不会更糟。
            kwargs.pop("default_headers", None)
            return init_chat_model(**kwargs)


def get_llm(cfg: Optional[Dict[str, Any]] = None) -> Optional[LLM]:
    """按配置构建 LLM；无 provider 或构建失败则返回 None（纯索引模式）。

    cfg 是**已经解析好的**一份 LLM 配置 dict，正常来自数据库：绑定预设
    （agent_llm_binding → llm_profiles）或该 agent 的内联 llm，见
    ``agent/config.py::_resolve_llm``。全局 config.yaml 的 llm 段不参与这里。

    支持字段：
        provider / model_name（兼容旧写法 model）/ api_key / base_url /
        temperature / timeout / max_retries
    """
    cfg = cfg or {}
    provider = cfg.get("provider")
    if not provider:
        return None
    try:
        model_name = (
            cfg.get("model_name")
            or cfg.get("model")
            or DEFAULT_MODELS.get(provider, "gpt-4o-mini")
        )
        llm_cfg = LLMConfig(
            model_name=model_name,
            provider=provider,
            api_key=_expand_env(cfg.get("api_key")),
            base_url=_expand_env(cfg.get("base_url")),
            temperature=cfg.get("temperature", 0.2),
            timeout=cfg.get("timeout", 120),
            max_retries=cfg.get("max_retries", 3),
        )
        return LangChainLLM(
            build_model(llm_cfg),
            provider=provider,
            model_name=model_name,
            # agent 走模型预设时由调用方塞进来；内联 llm 配置时为 None，
            # 成本换算会退到按 model_name（+provider）匹配。
            profile_id=cfg.get("profile_id"),
            cfg=llm_cfg,
        )
    except Exception as e:
        logger.warning("LLM 构建失败，降级为无 LLM: %s", e)
        return None

