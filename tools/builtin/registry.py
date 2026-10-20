"""内置工具清单——**keeper 自带**，与平台无关。

平台**不管理**内置工具：代码就在本仓库，不需要下载，也不需要平台下发
binding。是否装配给某个 agent 完全由**本地开关**决定（见
``agent_capability_overrides`` 表）。

工厂签名统一为 ``make_xxx(root, read_only) -> ProcessorTool``，``root`` 由装配器
传入（agent 的工作区根目录）。

``mutating`` 的工具在只读工作区下不会被装配——与 external 工具同一条安全规则。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 每项：name / impl / description / parameters / mutating / requires_workspace
# name 与工厂里 ProcessorTool 的 name 保持一致（否则装配出的工具名会漂移）。
BUILTIN_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "fs.deliver",
        "impl": "keeper.tools.builtin:make_deliver",
        "description": "文件 / 多模态交付的唯一正式入口：把本次要交付的「块（block）」列表（blocks）一次性声明，界面按块的 kind 渲染成可交互内容（图片预览、HTML 沙箱、代码高亮、图表、表格、流程图…）；兼容旧写法 paths",
        "parameters": {"blocks": "要交付的块列表；每个块是带 kind 的字典（kind 必填），或兼容地传 paths/路径字符串列表"},
        "mutating": False,
        "requires_workspace": True,
    },
    {
        # 任务模式的完成判定（D5：显式动作，不靠 LLM 自述「完成了」）
        "name": "task.mark_item_done",
        "impl": "keeper.tools.builtin:make_mark_item_done",
        "description": "任务模式下把当前正在执行的计划点标记为已完成，并写下结论与过程产物",
        "parameters": {
            "conclusion": "这一步的结论：简述做了什么、得到了什么结果",
            "artifacts": "可选：本步产出的文件相对路径数组",
        },
        # 只改库、不写工作空间，因此只读工作区下也要装配（否则任务永远推进不了）
        "mutating": False,
        "requires_workspace": False,
    },
]

# 内置工具的开关种类（override.kind）
KIND = "builtin"


def builtin_names() -> List[str]:
    return [t["name"] for t in BUILTIN_TOOLS]


def find_builtin(name: str) -> Optional[Dict[str, Any]]:
    return next((t for t in BUILTIN_TOOLS if t["name"] == name), None)
