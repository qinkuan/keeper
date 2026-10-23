"""code 子 agent：接收自然语言任务，自己写代码 → 跑 → 看输出 → 迭代，返回最终结果。

这是「子 react」的具体实例：主循环把"需要写代码才能完成的子任务"交给它，它内部
跑一个迷你 ReAct 循环（想 → 写代码 → 运行 → 观测 → 改），直到得出结果或达到迭代上限。
非交互：危险代码直接告知改、不弹确认；信息不足时把"缺少什么"作为文本冒泡回主循环。
"""
from __future__ import annotations

import re
from typing import Optional

from .delegatee import Ctx, Delegatee, DelegateResult, Requirement
from .ptc_sandbox import run_code, scan_code

_SYSTEM = """你是编程子 agent。主 agent 把一个需要写代码才能完成的子任务交给你。
规则：
1. 用 ```python 围栏输出你要运行的代码。代码里已自动 `import keeper_tools as kt`。
2. 需要读写文件、查数据等，用 `kt.call("工具名", 参数=值)` 调主系统的工具。
3. 最终答案用 print 输出到 stdout；若任务产出文件，print 出文件路径即可。
4. 若代码报错，你会收到运行输出（含报错），请修改后重试。
5. 不要做危险操作（rm -rf、sudo、删系统目录等）。确需新库时优先用已安装库；
   若必须安装，在代码顶部用注释说明，沙箱会自动 pip install（仅影响本沙箱）。
只输出简短思考 + 一个代码块；不要输出 <FINAL> 这类主循环标签。"""


class CodeDelegatee(Delegatee):
    name = "code"
    description = (
        "用 Python 写代码解决子任务（数据处理 / 格式转换 / 批量文件操作 / 借助库计算等）。"
        "适合主循环没有现成工具、需要灵活编程的活。"
    )
    #: 它跑的是**大模型自己刚写的代码**，不是第三方插件的代码——威胁模型不同，
    #: 不套工作区的只读约束（见 :class:`keeper.agent.delegatee.Delegatee` 的说明）。
    #: 路径越界仍然拦：只放开「能不能写工作区」，不放开「能不能写到外面去」。
    writable = True

    def __init__(self, llm, tools=None, max_iter: int = 5) -> None:
        self.llm = llm
        # 专属工具子集（ToolRegistry）；None 时主循环回退用宿主完整工具表。
        # 这是「子 agent 作为独立子 agent」的关键：它只看到被授权的工具。
        self.tools = tools
        self.max_iter = max_iter

    async def run(self, req: Requirement, ctx: Ctx) -> DelegateResult:
        from ..chat.context import sub_step

        prompt = req.text or ""
        if req.context:
            prompt += "\n\n已知信息：\n" + req.context
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ]
        last_out = ""
        for it in range(self.max_iter):
            # 把「这是父步下的第几次内部迭代」注入上下文，落盘标签才能显示
            # step{父}.{it+1}，否则子 agent 的多次迭代会全压平成一个 step。
            async with sub_step(it + 1):
                text = await self.llm.chat(messages)
            code = _extract_code(text)
            if not code:
                # 没给代码：可能直接给了答案
                return DelegateResult(answer=text.strip())
            risks = scan_code(code)
            if risks:
                # 非交互：命中危险直接要求改，继续迭代（不会弹确认）
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {"role": "user", "content":
                     f"[拒绝] 代码含潜在危险操作：{'；'.join(risks)}。请改用安全方式重写。"}
                )
                continue
            out, artifacts = await run_code(code, ctx.call_tool)
            messages.append({"role": "assistant", "content": text})
            if _is_success(out):
                return DelegateResult(answer=out.strip(), artifacts=artifacts)
            last_out = out.strip() or "[无输出]"
            messages.append(
                {"role": "user", "content":
                 "运行输出（含报错）：\n" + last_out + "\n请修改代码重试。"}
            )
        return DelegateResult(
            answer=f"[code 子 agent 未在第 {self.max_iter} 次迭代内得出结果]\n最后输出：\n{last_out}"
        )


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return ""


def _is_success(out: str) -> bool:
    if not out.strip():
        return False
    # 没有典型失败标记即视为成功
    return not any(k in out for k in ("Traceback", "Error", "拒绝", "超时", "运行失败"))
