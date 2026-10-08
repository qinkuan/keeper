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
        "name": "fs.list_dir",
        "impl": "keeper.tools.builtin:make_list_dir",
        "description": "列出工作空间内的目录内容（[D] 目录，[F] 文件）",
        "parameters": {"path": "相对工作空间的目录路径，默认 '.'"},
        # read_only 与 mutating 是两回事：前者决定能不能并发跑（工具并行化 P1-1），
        # 后者决定只读工作区下装不装。
        "read_only": True,
        "mutating": False,
        "requires_workspace": True,
    },
    {
        "name": "fs.read_file",
        "impl": "keeper.tools.builtin:make_read_file",
        "description": "读取工作空间内的文本文件",
        "parameters": {"path": "相对工作空间的文件路径"},
        "read_only": True,
        "mutating": False,
        "requires_workspace": True,
    },
    {
        "name": "fs.write_file",
        "impl": "keeper.tools.builtin:make_write_file",
        "description": "把内容写入工作空间内的文件（父目录自动创建）",
        "parameters": {
            "path": "相对工作空间的文件路径",
            "content": "要写入的文本",
        },
        "mutating": True,
        "requires_workspace": True,
    },
    {
        "name": "fs.edit_file",
        "impl": "keeper.tools.builtin:make_edit_file",
        "description": "编辑工作空间内**已有**的文本文件：精确替换其中一段（改几行只传那几行）",
        "parameters": {
            "path": "相对工作空间的文件路径（也可用 @tmp/ 前缀）",
            "old_string": "要被替换的原文（原样含缩进换行，最好足够长以保证唯一）",
            "new_string": "替换后的文本；删除则传空字符串",
            "replace_all": "可选，默认 false；确认替换全部同名片段时置 true",
        },
        # 读-改-写之间有中间态，不能与别的工具并发跑
        "read_only": False,
        "mutating": True,
        "requires_workspace": True,
    },
    {
        "name": "fs.find",
        "impl": "keeper.tools.builtin:make_find",
        "description": "按文件名 glob 在工作空间内（含子目录）搜索已存在的文件，返回匹配的相对路径列表",
        "parameters": {"pattern": "文件名 glob 模式，如 '*录取*'、'*.pdf'"},
        "read_only": True,
        "mutating": False,
        "requires_workspace": True,
    },
    {
        "name": "fs.publish",
        "impl": "keeper.tools.builtin:make_publish",
        "description": "把工作空间内已存在的文件登记为对话产物（可预览/打开），用于向用户展示已有文件",
        "parameters": {"path": "相对工作空间的文件路径"},
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
