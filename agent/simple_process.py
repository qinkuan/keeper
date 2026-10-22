"""快捷模式（simple）：一次性任务的可执行体——翻译 / OCR / 总结等。

从 ``process.py`` 拆出、独立成文件：三个运行模式各占一个文件、彼此**不继承**——
- :class:`ReactProcess`（普通）在 ``process.py``；
- :class:`SimpleProcess`（快捷，本文件）；
- ``PtcProcess``（程序化）待加，届时同样独立成文件。

三者只共用 ``process.py`` 里的模块级 :class:`ProcessResult` 与记账函数
（``check_budget_scope`` / ``log_usage``）——那是跨模式的公共契约（用量 / 预算），
提成函数共用，而非继承基类。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .process import ProcessResult, check_budget_scope, log_usage


class SimpleProcess:
    """快捷模式：一次性任务（翻译 / OCR / 总结等），无循环、无记忆、无 planner。

    与 ReactProcess / PtcProcess **相互独立、不继承**，只共用模块级的 ProcessResult
    与记账函数（check_budget_scope / log_usage）。

    为什么单开一档：这类任务给一句话就要一个结果，多步 ReAct、最近历史、长期记忆
    召回全是负担——不仅慢，还会把"就翻译一句"跑成"先 recall 再调三个工具"。所以这里
    就是**一次** LLM 调用：极简 system + 用户问题（可带图片附件做 OCR / 看图）→ 流式返回。

    history / memory_hint：签名与其它模式一致（调用方统一传），但本模式**忽略**——
    "要不要记忆"由调用方按 mode 决定（见 chat/service.py），快捷模式根本不取，传进来也是空的。
    """

    def __init__(
        self,
        llm,
        tools=None,
        *,
        agent_name: str = "",
        system_extra: str = "",
    ) -> None:
        self.llm = llm
        # 本模式不调工具；保留 registry 只为签名与其它模式一致，便于统一装配。
        self.registry = tools
        self.agent_name = agent_name
        self.system_extra = system_extra
        self._budget_scope: Tuple[Optional[str], Optional[str]] = (None, None)

    @classmethod
    def from_agent(cls, agent: "KeeperAgent", tools) -> "SimpleProcess":
        return cls(
            agent.llm,
            tools,
            agent_name=agent.name,
            # 只带 always 常驻技能的行为正文；任务型技能不加载（快捷模式不用）。
            system_extra=cls._skill_system(agent.skills),
        )

    @staticmethod
    def _skill_system(skills) -> str:
        return "\n".join(
            s.system_prompt
            for s in (skills or [])
            if s.system_prompt and bool(getattr(s, "always", False))
        )

    def _system(self) -> str:
        extra = (self.system_extra or "").strip()
        base = "你是一个专注的助手。请直接、简洁地完成下面这个一次性任务，不要展开成多步骤流程。"
        return base + (("\n\n" + extra) if extra else "")

    def _build_messages(self, question: str, file_parts) -> List[dict]:
        # 有图片附件（OCR / 看图总结）时按 OpenAI content block 组装；否则纯文本。
        if file_parts:
            content = list(file_parts) + [{"type": "text", "text": question}]
            return [{"role": "user", "content": content}]
        return [{"role": "user", "content": question}]

    async def run(
        self,
        question: str,
        *,
        on_step=None,
        on_delta=None,
        history=None,
        memory_hint=None,
        should_stop=None,
        session_id=None,
        task_id=None,
        file_parts: Optional[List[dict]] = None,
    ) -> ProcessResult:
        """一次性调用：极简 system + 问题（+ 图片附件）→ 流式答案。无循环 / 无工具 / 无挂起。"""
        u = getattr(self.llm, "usage", None)
        before = u.copy() if u is not None else None
        self._budget_scope = (session_id, task_id)
        try:
            # 快捷模式也过预算守卫：别让一次性任务把额度烧穿
            budget = await check_budget_scope(self._budget_scope)
            if budget and budget.get("over"):
                scope = "任务" if budget.get("scope") == "task" else "会话"
                return ProcessResult(
                    answer=(
                        f"（已超出{scope} token 预算：已用 {budget.get('used')} / "
                        f"上限 {budget.get('limit')}，本轮未执行。请新开会话，或调高 "
                        f"KEEPER_SESSION_TOKEN_LIMIT。）"
                    ),
                    budget_stopped=True,
                    budget=budget,
                )
            # 单趟推理：把这次 LLM 调用标成「第 1 步」，否则可观测里既没 react_step
            # 行、dump 标签也退化成 other——「查看详情 / dump」就只剩汇总、看不到这一步。
            from ..chat.context import set_llm_call_kind, set_step_seq

            set_step_seq(1)
            set_llm_call_kind("react_step")
            parts: List[str] = []
            async for piece in self.llm.astream_chat(
                self._build_messages(question, file_parts), system=self._system()
            ):
                if on_delta is not None:
                    await on_delta(piece)
                parts.append(piece)
            return ProcessResult(answer="".join(parts))
        finally:
            log_usage(self.llm, before)
