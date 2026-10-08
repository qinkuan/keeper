"""任务模式的内置工具：让 agent **显式**标记「当前计划点已完成」。

设计见 ``keeper/doc/task-design.md`` 决策 D5：完成判定走显式动作，而不是靠
LLM 在回复里说一句「完成了」——后者容易误判 / 漏判，也无法挂结论与产物。

「当前是哪个计划点」不靠参数传、也不靠闭包捕获，而是从 ContextVar 取
（执行器发一轮对话前 set 一次），避免多会话并发时串台。
"""
from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from ..base import ProcessorTool
from .sandbox import PathEscape, safe_path


def _effective_root(fallback_root: Path) -> Path:
    """取当前请求的工作空间根；无上下文则回落兜底值（与 fs 工具同一套规则）。"""
    from ...chat.context import current_workspace

    ws = current_workspace()
    return ws.root if ws is not None else Path(fallback_root)


def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def make_mark_item_done(root: Path, read_only: bool) -> ProcessorTool:
    """标记当前正在执行的任务计划点为已完成，并写入结论与过程产物。

    参数：
    - ``conclusion``（必填）：这一步的结论，简述做了什么、结果是什么；
    - ``artifacts``（可选）：本步产出的文件相对路径数组，登记为该点的过程产物。

    注意：本工具**不写工作空间**（只改库），所以在只读工作区下也可用。
    """

    async def run(args: dict) -> str:
        from ...chat.context import current_task_item_id
        from ...store import TaskItem, get_session_factory, new_id

        item_id = current_task_item_id()
        if not item_id:
            return "[拒绝] 当前不在任务执行上下文中（没有进行中的计划点），无法标记完成"
        conclusion = (args.get("conclusion") or "").strip()
        if not conclusion:
            return "[缺少参数 conclusion] 请简述这一步的结论（做了什么、结果是什么）"

        eff_root = _effective_root(root)
        raw = args.get("artifacts") or []
        if isinstance(raw, str):
            raw = [raw]
        arts = []
        for rel in raw:
            rel = (rel or "").strip()
            if not rel:
                continue
            try:
                p = safe_path(eff_root, rel)
            except PathEscape as e:
                return f"[拒绝] {e}"
            if not p.is_file():
                return f"[文件不存在] {rel}"
            arts.append(
                {
                    "id": new_id(),
                    "name": p.name,
                    "path": "file://" + str(p),
                    "mime": _guess_mime(p.name),
                    "size": p.stat().st_size,
                }
            )

        factory = get_session_factory()
        async with factory() as db:
            it = await db.get(TaskItem, item_id)
            if it is None:
                return "[拒绝] 计划点不存在（可能已被重新规划作废）"
            if it.status == "done":
                return f"该计划点已是完成态：{it.content_md}"
            if it.status == "skipped":
                return "[拒绝] 该计划点已被作废（重新规划过），不能再标记完成"
            it.status = "done"
            it.conclusion = conclusion
            it.finished_at = datetime.now(timezone.utc)
            if arts:
                old = json.loads(it.artifacts) if it.artifacts else []
                it.artifacts = json.dumps(old + arts, ensure_ascii=False)
            await db.commit()
            seq, content = it.seq, it.content_md

        suffix = f"，登记过程产物 {len(arts)} 个" if arts else ""
        return f"已标记完成（第 {seq} 步：{content}）{suffix}"

    return ProcessorTool(
        name="task.mark_item_done",
        description=(
            "在任务模式下，当你确认**当前正在执行的那一个计划点**已经做完时，"
            "调用本工具把它标记为完成，并写下结论（做了什么、结果是什么）。"
            "必须用这一步来宣告完成——只在回复里写「完成了」不算数。"
            "若本步产出了**要交付给用户的文件**，用 artifacts 登记为交付产物"
            "（相对工作空间的路径数组）。只标记当前这一步，不要越级标记后面的步骤。"
        ),
        parameters={
            "conclusion": "这一步的结论：简述做了什么、得到了什么结果",
            "artifacts": (
                "可选：本步**要交付给用户的最终文件**相对路径数组，如 ['out.html']。"
                "**只登记真正的交付物**：你自己写来验证的临时测试脚本、调试脚本、"
                "补丁脚本（如 check.py、test_*.js、patch_*.py、extract_*.py）"
                "**不要登记**——那些是过程产物，登记进来只会挤占「最终产物」列表。"
            ),
        },
        run=run,
    )
