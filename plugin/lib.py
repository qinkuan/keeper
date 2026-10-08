"""插件库：目录扫描、清单解析、每-agent 的链接管理。

**插件库是一个目录，不是一种来源。**
------------------------------------
手写一个插件包丢进去、用 plugin-authoring 写完放进去、将来远程下载安装——都只是
"往这个目录里放东西"的不同方式。装载链路只认"库里现在有什么"，不关心它从哪来，
所以将来加远程安装时这里是**纯增量**：多一个往目录里写文件的实现即可，装配、
校验、UI 全都不用动。

目录结构
--------
::

    <root>/<name>/keeper-plugin.json      ← 一个子目录 = 一个插件
    <root>/<name>/...                     ← 包内其余文件（bin/、mcp/、SKILL.md…）

每 agent 的链接
--------------
插件**不复制**进 agent 目录，而是在 ``~/.keeper/agents/<agent_id>/plugins/<name>``
建一个**相对符号链接**指回库里的真实目录：

- 零成本：codebase-memory 这种 283MB 的包不会因为有 5 个 agent 就变成 1.4GB；
- 跟得上：库里改了插件，所有 agent 立刻看到新版本，不需要"同步副本"这种会
  冲掉改动的操作；
- 删 agent 安全：``shutil.rmtree`` 遇到 symlink 只 unlink 链接、不动目标。

用**相对**链接而不是绝对路径：整个 ``~/.keeper`` 换位置、换机器都不会全断。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 插件清单文件名：包根目录的这一份是**真源**（库里的快照不算）
PLUGIN_MANIFEST = "keeper-plugin.json"

# 打包时多套了一层目录时要跳过的噪声目录（与历史行为一致）
_PACKAGE_NOISE_DIRS = ("__MACOSX",)


def plugins_root() -> Path:
    """插件库根目录（来自 ``config.yaml`` 的 ``plugins.root``）。

    相对路径按**进程工作目录**解析——正常从仓库根启动，所以 ``./keeper/plugins``
    就是仓库里那个目录。用 ``resolve()`` 固化绝对路径：后面要拿它算相对链接，
    中途 cwd 变了会算出错的链接。
    """
    from ..config import load_config

    try:
        raw = load_config().plugins.root
    except Exception:  # noqa: BLE001 配置读不到就用默认，别让插件库整个不可用
        raw = "./keeper/plugins"
    return Path(raw).expanduser().resolve()


def agent_plugins_dir(agent_id: str) -> Path:
    """某 agent 的插件链接目录：``~/.keeper/agents/<agent_id>/plugins``。

    与 :func:`keeper.agent.config.default_agent_resource_root` 同根，只是把 kind
    定成复数 ``plugins``——它装的是"若干个插件"，不是"一个插件"。
    这里不 import agent.config 是为了避开循环依赖（它要 import 本模块）。
    """
    return Path.home() / ".keeper" / "agents" / str(agent_id) / "plugins"


# ──────────────────────────────────────────────────────────────────────────
# 清单
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class EnvDependency:
    """插件声明的运行环境要求。

    插件包自己说清"我需要 node >= 18"，创建 agent 时就能在**装之前**校验本机满不
    满足，而不是等 agent 跑起来才炸——后者难查得多（表现为子进程起不来或者
    命令找不到，跟插件本身没关系）。
    """

    kind: str  # python / node
    min_version: str = ""
    max_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "minVersion": self.min_version,
            "maxVersion": self.max_version,
        }


def _as_str(v: Any) -> str:
    return str(v).strip() if v not in (None, "") else ""


def parse_env_dependencies(manifest: Dict[str, Any]) -> List[EnvDependency]:
    """从清单里读 ``envDependencies``。

    字段名与平台侧 ``EnvDependency`` 对齐（``kind`` / ``minVersion`` /
    ``maxVersion``），也接受蛇形写法——手写 JSON 时两种都常见，拒掉反而碍事。
    解析不了就当没声明：**清单是给人写的**，不该因为多一个没见过的字段就装不上。
    """
    raw = manifest.get("envDependencies") or manifest.get("env_dependencies") or []
    if not isinstance(raw, list):
        return []
    out: List[EnvDependency] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = _as_str(item.get("kind")).lower()
        if kind not in ("python", "node"):
            continue
        out.append(
            EnvDependency(
                kind=kind,
                min_version=_as_str(item.get("minVersion") or item.get("min_version")),
                max_version=_as_str(item.get("maxVersion") or item.get("max_version")),
            )
        )
    return out


def read_manifest(plugin_dir: Path) -> Dict[str, Any]:
    """读包内清单。清单**可选**（纯 SKILL.md 包可以不写），解析失败抛 ValueError。

    抛异常而不是吞掉：清单坏了却静默当成空清单，等于插件"装上了但什么都不会"，
    排查时完全看不出是清单有问题。
    """
    path = Path(plugin_dir) / PLUGIN_MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"插件清单解析失败（{path}）：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"插件清单必须是 JSON 对象（{path}）")
    return data


def resolve_plugin_root(dest: Path) -> Path:
    """兼容"打包时多套了一层目录"：清单不在包根就往下探唯一的那层。

    探到多层（或者零层）就原样返回，让调用方按"没有清单"处理。
    """
    try:
        cands = [
            p
            for p in sorted(Path(dest).iterdir())
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name not in _PACKAGE_NOISE_DIRS
            and (p / PLUGIN_MANIFEST).is_file()
        ]
    except OSError:  # noqa: BLE001
        return Path(dest)
    if len(cands) == 1:
        return cands[0]
    return Path(dest)


# ──────────────────────────────────────────────────────────────────────────
# 扫描
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class LibraryPlugin:
    """插件库里读出来的一个插件。

    ``dirname`` 是它**在插件库里的目录名**，``name`` 是清单里声明的名字。
    两者不一定相同（目录叫 ``codebase-memory-mcp``、清单写 ``codebase-memory``
    是常见的），而链接名必须用 ``dirname``——链接建在
    ``<agent>/plugins/<dirname>``，对得上用户在文件夹里看到的东西。
    """

    name: str
    path: Path
    dirname: str = ""
    version: str = ""
    description: str = ""
    # 能力类型：bin（可执行）/ mcp（MCP server）/ skill（纯知识包）
    kinds: List[str] = field(default_factory=list)
    env_dependencies: List[EnvDependency] = field(default_factory=list)
    size_bytes: int = 0
    # 清单有问题时记在这里，仍然把插件列出来（能让人看见并去修）
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dirname": self.dirname or self.name,
            "path": str(self.path),
            "version": self.version,
            "description": self.description,
            "kinds": self.kinds,
            "envDependencies": [d.to_dict() for d in self.env_dependencies],
            "sizeBytes": self.size_bytes,
            "error": self.error,
        }


def _dir_size(p: Path, *, limit: int = 4000) -> int:
    """目录体积（累加，文件数封顶）。

    封顶是因为这个值只用于**展示**——283MB 的包要遍历几万个文件，而插件菜单
    不值得为了一列数字卡一下。超了就返回当时的累计值，前端显示"≥"。
    """
    total = 0
    count = 0
    try:
        for f in p.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            try:
                total += f.stat().st_size
            except OSError:  # noqa: BLE001
                continue
            count += 1
            if count >= limit:
                break
    except OSError:  # noqa: BLE001
        pass
    return total


def _manifest_kinds(manifest: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if manifest.get("bin"):
        out.append("bin")
    if manifest.get("mcpServers"):
        out.append("mcp")
    # 有清单但既没 bin 也没 mcpServers → 当作纯知识包
    if not out and (manifest.get("skill") or manifest.get("name")):
        out.append("skill")
    return out


def scan_library(root: Optional[Path] = None) -> List[LibraryPlugin]:
    """扫插件库，返回全部插件（按名字排序）。

    一个子目录含 ``keeper-plugin.json`` 就算一个插件；清单坏了也**列出来**
    （带 error），因为"库里有这个目录但装不上"是最需要被看见的状态。
    """
    base = Path(root) if root is not None else plugins_root()
    if not base.is_dir():
        return []
    out: List[LibraryPlugin] = []
    try:
        entries = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:  # noqa: BLE001
        return []
    for d in entries:
        if d.name.startswith(".") or d.name in _PACKAGE_NOISE_DIRS:
            continue
        if not (d / PLUGIN_MANIFEST).is_file():
            continue
        # 清单不在包根就往下探一层（兼容多套了一层的包）
        pkg = resolve_plugin_root(d)
        err = ""
        manifest: Dict[str, Any] = {}
        try:
            manifest = read_manifest(pkg)
        except ValueError as e:
            err = str(e)
        out.append(
            LibraryPlugin(
                name=_as_str(manifest.get("name")) or d.name,
                path=pkg,
                dirname=d.name,
                version=_as_str(manifest.get("version")),
                description=_as_str(manifest.get("description")),
                kinds=_manifest_kinds(manifest),
                env_dependencies=parse_env_dependencies(manifest),
                size_bytes=_dir_size(pkg),
                error=err,
            )
        )
    out.sort(key=lambda p: p.name)
    return out


def find_plugin(name: str, root: Optional[Path] = None) -> Optional[LibraryPlugin]:
    """按名字找插件。

    先按**目录名**找，再按清单里的 ``name`` 找：用户往库里丢的目录未必和清单
    名字一致（``codebase-memory-mcp`` vs ``codebase-memory``），两个都认更符合
    "我在文件夹里看到什么就点什么"的直觉。
    """
    if not name:
        return None
    for p in scan_library(root):
        if name in (p.dirname, p.name):
            return p
    return None


# ──────────────────────────────────────────────────────────────────────────
# 环境要求校验
# ──────────────────────────────────────────────────────────────────────────


def _ver_tuple(text: str) -> tuple:
    """"3.10" / "18" / "v20.18.0" → 可比较的元组。

    短的后补 0：否则 ``(3, 10) < (3, 10, 0)``，会让"要求 >= 3.10"把
    3.10.0 判成不满足——这种错很隐蔽，只在某个特定 patch 版本上复现。
    """
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _pad(a: tuple, b: tuple) -> tuple:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def detect_runtime_versions() -> Dict[str, List[tuple]]:
    """本机各 kind 的可用版本列表（降序）。探测失败就当没有。

    复用 ``framework.runtime.detect_runtimes``——那是唯一一处真正跑
    ``--version`` 的地方，这里不重复实现探测逻辑（不同实现会给出不同答案，
    而"创建时通过、装载时失败"是最难查的一类问题）。
    """
    from ..framework.runtime import detect_runtimes

    out: Dict[str, List[tuple]] = {}
    for kind in ("python", "node"):
        try:
            out[kind] = sorted(
                (d["version"] for d in detect_runtimes(kind)), reverse=True
            )
        except Exception:  # noqa: BLE001 探测失败 ≠ 环境不满足，但要能报出"没探测到"
            out[kind] = []
    return out


def check_env_requirements(
    deps: List[EnvDependency],
) -> tuple:
    """校验本机是否满足一批环境要求。

    返回 ``(是否通过, 逐条结果)``。**结果里带上本机实际版本**：只说"不满足"
    而不说"你本机是 3.9"，用户没法判断是装错了还是要求写错了。

    多个插件声明同一个 kind 时**逐条判定、取最严**：插件 A 要 node>=18、
    插件 B 要 node>=20，装了 A 没装 B 能过，两个都装就该要求 20。只看"有没有
    任意一个满足"会让后者的要求形同虚设。
    """
    installed = detect_runtime_versions()
    results: List[Dict[str, Any]] = []
    for d in deps:
        avail = installed.get(d.kind) or []
        lo = _ver_tuple(d.min_version)
        hi = _ver_tuple(d.max_version)
        picked = None
        for v in avail:
            a, b = _pad(v, lo) if lo else (v, v)
            if lo and a < b:
                continue
            if hi:
                c, d2 = _pad(v, hi)
                if c > d2:
                    continue
            picked = v
            break
        results.append(
            {
                "kind": d.kind,
                "minVersion": d.min_version,
                "maxVersion": d.max_version,
                "installed": [".".join(str(x) for x in v) for v in avail[:3]],
                "satisfied": picked is not None,
            }
        )
    return all(r["satisfied"] for r in results), results


# ──────────────────────────────────────────────────────────────────────────
# 每 agent 的链接
# ──────────────────────────────────────────────────────────────────────────


def link_plugin(agent_id: str, plugin: LibraryPlugin) -> Path:
    """在 ``<agent>/plugins/`` 下建一个**相对**符号链接指向插件库里的目录。

    相对链接（而不是绝对）：``~/.keeper`` 整体搬家、换机器都不会断。

    已存在的链接会先删掉再建——重复勾选同一个插件不该报错，而且这样"改了库里的
    真实路径"也能自动跟上。
    """
    link_dir = agent_plugins_dir(agent_id)
    link_dir.mkdir(parents=True, exist_ok=True)
    # 相对链接必须在**同一套解析后的路径**里算。
    # os.path.relpath 是纯词法运算，而 macOS 上 /var 是 /private/var 的软链：
    # 一边带软链、一边不带时，`../../..` 会落到错误位置，链接当场悬空
    # （症状是 is_symlink() 为 True 但 is_dir() 为 False，插件静默不加载）。
    real_dir = link_dir.resolve()
    target = plugin.path.resolve()
    rel = os.path.relpath(target, real_dir)
    # 链接名用**库里的目录名**（dirname），不是清单里的 name：链接是"指向某个目录"，
    # 名字必须和用户在文件夹里看到的一致。
    link = real_dir / (plugin.dirname or plugin.name)
    if link.is_symlink() or link.exists():
        if link.is_dir() and not link.is_symlink():
            raise ValueError(
                f"{link} 已存在且不是符号链接，请先手动移除"
            )
        link.unlink()
    link.symlink_to(rel, target_is_directory=True)
    logger.info(
        "绑定插件：agent=%s 插件=%s 链接=%s -> %s（库目录 %s）",
        agent_id, link.name, link, rel, target,
    )
    return link


def unlink_plugin(agent_id: str, name: str) -> bool:
    """删掉某个 agent 的插件链接（只删链接，不动库里的目标）。返回是否真删了。

    判断用 ``is_symlink()`` 而不是 ``exists()``：链接悬空时 ``exists()`` 是 False，
    但那个链接仍然该删——它就是上次指向已删除目录留下的垃圾。
    """
    link = agent_plugins_dir(agent_id) / name
    if link.is_symlink():
        logger.info("解绑插件：agent=%s 插件=%s 链接=%s", agent_id, name, link)
        link.unlink()
        return True
    logger.info(
        "解绑插件：agent=%s 插件=%s 跳过（%s 不是符号链接，可能已是下载的固定副本，"
        "需手动删除目录才会真正生效）",
        agent_id, name, link,
    )
    return False


def linked_plugins(agent_id: str) -> List[str]:
    """某 agent 当前**可用**的插件链接名（库内目录名）。

    悬空的链接不算：它对应"库里那个目录已经被删了"，算进去只会让 UI 显示一个
    装不上的插件。``p.is_dir()`` 会跟随链接，所以断链自动被过滤掉了。
    """
    d = agent_plugins_dir(agent_id)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())
