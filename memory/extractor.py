"""异步记忆块摘要：系统侧、一轮结束后跑，不干扰 ReAct 主循环。

设计依据见 doc/memory.md 5.4（系统侧异步抽取）与 7.5（Turn 块级索引）。
``llm`` 接受 duck-typed 的 ``LLMChat``（需有 ``async def chat(messages, system)``），
从而与具体 LLM 实现解耦（调用方用 llm/client.py 的 build_model 结果注入即可）。
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class LLMChat(Protocol):
    async def chat(self, messages: List[dict], system: str = "") -> str: ...


# 单轮记忆块摘要：用于 7.5 的 Turn 块级索引（recall 粗筛）。只需轮廓，无需逐字细节。
_TURN_SUMMARY_PROMPT = """用一句话（不超过 150 字）总结下面「单轮对话」的要点：
用户问了什么、你调用了哪些工具及关键结果、最终结论是什么。
用于记忆块索引，便于后续按需检索召回。不要复述全部细节，只留可检索的轮廓。

单轮对话：
{transcript}
"""


async def summarize_turn(
    turn_text: str,
    llm: LLMChat,
    system: str = "你是记忆块摘要器，只输出一句话摘要。",
) -> str:
    """生成单个 Turn 块的摘要（用于 7.5 块级索引 / recall 粗筛）。

    ``turn_text`` 通常是 ``sm.render_turn_text(rec)``（``sm`` 为 SessionMemory 实例）的输出。
    """
    # 摘要是在 ReAct 循环**之后**跑的，此时 contextvar 里还残留着
    # kind=react_step 与 step_seq=N。若不覆盖，这次调用会被记账成「某一步」，
    # dump 文件名也会被写成 stepN——和真实的第 N 步混在一起。摘要不属于任何
    # 步，显式标为 turn_summary。
    try:
        from ..chat.context import set_llm_call_kind, set_step_seq

        set_llm_call_kind("turn_summary")
        set_step_seq(None)
    except Exception:  # noqa: BLE001
        pass

    messages = [
        {"role": "user", "content": _TURN_SUMMARY_PROMPT.format(transcript=turn_text)}
    ]
    return (await llm.chat(messages, system=system)).strip()
