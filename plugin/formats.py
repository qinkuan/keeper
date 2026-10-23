"""插件包格式：探测 + 分派。

**一个目录是不是插件、按哪种格式读，全看特征标志。**
------------------------------------------------
keeper 包的标志是包根的 ``keeper-plugin.json``；Claude 包的标志是
``.claude-plugin/plugin.json``。两种布局的技能目录约定（``skills/<name>/SKILL.md``）
恰好一致，所以真正因格式而异的只有四件事：

- 特征标志（markers）；
- 清单读取（位置与字段）；
- 能力类型判定（kinds）；
- 包级校验（有目录内容但本格式不装配时告警）。

其余全部共享——目录遍历、多套一层目录的解析、每-agent 链接管理、技能目录发现、
装配骨架（见 ``agent/config._build_plugin_components``）。**不写两份**：双份实现
必然漂移，改了 keeper 忘了 claude 是最典型的死法。

加新格式 = 加一个 :class:`PluginFormat` 子类并登记进 :data:`FORMATS`，扫描 / 装载 /
UI 自动跟上，主循环一行不用动。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# keeper 清单文件名：包根目录的这一份是**真源**（库里的快照不算）
PLUGIN_MANIFEST = "keeper-plugin.json"


def _read_json_manifest(path: Path) -> Dict[str, Any]:
    """读一份 JSON 清单；坏清单抛 ``ValueError``——装不上也要让人看见原因。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"清单解析失败（{path}）：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"清单必须是 JSON 对象（{path}）")
    return data


@dataclass
class PluginFormat:
    """一种插件包格式。子类只覆盖需要差异化的部分。"""

    name: str
    #: 特征标志（相对包根），任一存在即认定为该格式
    markers: Tuple[str, ...] = ()
    #: 该格式下「有内容但不装配」的目录名，用于包级告警
    unsupported_dirs: Tuple[str, ...] = ()

    def read_manifest(self, pkg: Path) -> Dict[str, Any]:
        """读包内清单并返回（字段名与 keeper 装配关心的语义对齐）。"""
        raise NotImplementedError

    def kinds(self, manifest: Dict[str, Any]) -> List[str]:
        """能力类型：bin（可执行）/ mcp（MCP server）/ skill（纯知识包）。

        有 bin / mcpServers 字段算对应类型；都没有但有个名字 → 纯知识包
        （Claude 的 plugin.json 只有 name/version/description，正落在这里）。
        """
        out: List[str] = []
        if manifest.get("bin"):
            out.append("bin")
        if manifest.get("mcpServers"):
            out.append("mcp")
        if not out and (manifest.get("skill") or manifest.get("skills") or manifest.get("name")):
            out.append("skill")
        return out

    def package_warnings(self, pkg: Path, manifest: Dict[str, Any]) -> List[str]:
        """包级提醒：存在本格式不装配的目录内容。不阻塞装配。"""
        out: List[str] = []
        for d in self.unsupported_dirs:
            p = pkg / d
            try:
                has_content = p.is_dir() and any(p.iterdir())
            except OSError:  # noqa: BLE001
                has_content = False
            if has_content:
                out.append(
                    f"{d}/ 目录有内容，但 {self.name} 格式一期不装配该组件（已忽略）"
                )
        return out


class KeeperFormat(PluginFormat):
    """keeper 原生格式：包根一份 ``keeper-plugin.json``，字段全量（bin/mcp/skill）。"""

    def __init__(self) -> None:
        super().__init__(name="keeper", markers=(PLUGIN_MANIFEST,))

    def read_manifest(self, pkg: Path) -> Dict[str, Any]:
        return _read_json_manifest(pkg / PLUGIN_MANIFEST)


class ClaudeFormat(PluginFormat):
    """Claude 插件布局：``.claude-plugin/plugin.json`` + ``skills/<name>/SKILL.md``。

    - 清单字段（name / version / description）与 keeper 语义一致，读进来直接可用；
    - ``skills/`` 目录约定相同，技能发现零改动复用；
    - ``commands/``、``agents/``、``hooks/`` 是 Claude 的另一套机制，一期不装配
      （只告警）；``mcp/``、``bin/`` 也不是 Claude 的概念，同样告警后忽略。
    """

    MARKER = ".claude-plugin/plugin.json"

    def __init__(self) -> None:
        super().__init__(
            name="claude",
            markers=(self.MARKER,),
            unsupported_dirs=("commands", "agents", "hooks", "mcp", "bin"),
        )

    def read_manifest(self, pkg: Path) -> Dict[str, Any]:
        return _read_json_manifest(pkg / self.MARKER)


#: 已登记的格式。**顺序即优先级**：一个包同时带多种标志时，排前面的赢
#: （keeper 语义最明确，放第一）。
FORMATS: List[PluginFormat] = [KeeperFormat(), ClaudeFormat()]


def detect_format(pkg: Path) -> Optional[PluginFormat]:
    """按特征标志探测包格式；没有已知标志 → ``None``（调用方按「不是插件」处理）。

    唯一的 dispatch 点：扫描（``plugin.lib.scan_library``）与装载
    （``agent.config._load_plugins``）都走它，保证「库里看得见」与「装配读得到」
    永远是同一套判断。
    """
    pkg = Path(pkg)
    for fmt in FORMATS:
        for marker in fmt.markers:
            if (pkg / marker).is_file():
                return fmt
    return None


def any_marker_present(pkg: Path) -> bool:
    """包里是否存在**任意**已知格式的标志（用于「多套一层目录」的探测）。"""
    pkg = Path(pkg)
    return any((pkg / m).is_file() for fmt in FORMATS for m in fmt.markers)
