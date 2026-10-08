"""framework 运行时管理：检测本机已安装的 python / node 环境。

设计调整（不再下载）：
- 之前的设计是「缺哪个版本就从网络下载可移植运行时到 ``~/.keeper/framework``」；
  现改为**直接检测用户电脑上已经装好的解释器**——绝大多数开发机本就有
  python / node，无需任何下载即可拿来跑 agent 的依赖（工具包 / 脚本 / 本地
  MCP 进程）。
- 检测顺序（按优先级）：
  1. ``config.yaml`` 的 ``framework.python`` / ``framework.node`` 显式指定；
  2. ``framework.extra_paths`` 给出的候选目录 / 可执行文件；
  3. 系统 ``PATH`` 上的常见命令（``python3`` / ``python``、``node``）。
- 版本匹配：``resolve_runtime(kind, "3.11")`` 优先找 major.minor 恰好 3.11 的；
  找不到则退而用本机最高版本（provision 阶段会提示版本不一致）。
- 需要网络下载的那套（RUNTIME_CATALOG / 镜像源）已移除；agent 的**业务依赖**
  （包 / 脚本 / MCP 归档）仍由 provision 引擎从平台拉取，见 ``_download_and_extract``。
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
KEEPER_HOME = Path.home() / ".keeper"
FRAMEWORK_ROOT = KEEPER_HOME / "framework"

# 各 kind 在 PATH / extra_paths 上探测的命令名
_CANDIDATE_NAMES: Dict[str, Tuple[str, ...]] = {
    "python": ("python3", "python"),
    "node": ("node",),
}


# ---------------------------------------------------------------------------
# 配置（framework 段：显式路径 + 额外搜索路径）
# ---------------------------------------------------------------------------
def _config_framework() -> Dict[str, Any]:
    """懒读取 config.yaml 的 framework 段（不引入硬依赖，避免循环导入）。

    返回 ``{"python": str, "node": str, "extra_paths": list}``，缺省为空。
    """
    try:
        from keeper.config import load_config

        fw = load_config().framework
        cfg = {
            "python": fw.python,
            "node": fw.node,
            "extra_paths": list(fw.extra_paths or []),
        }
    except Exception:  # noqa: BLE001
        cfg = {"python": "", "node": "", "extra_paths": []}
    # 环境变量优先级最高（临时覆盖显式路径）
    import os

    if os.environ.get("KEEPER_PYTHON"):
        cfg["python"] = os.environ["KEEPER_PYTHON"]
    if os.environ.get("KEEPER_NODE"):
        cfg["node"] = os.environ["KEEPER_NODE"]
    return cfg


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def framework_root() -> Path:
    return FRAMEWORK_ROOT


def runtime_dir(kind: str, version: str) -> Path:
    """保留兼容：某运行时的「登记目录」（检测模式下基本不用，仅作占位）。"""
    return FRAMEWORK_ROOT / kind / version


def _current_platform() -> str:
    os_name = platform.system().lower()  # darwin / linux / windows
    arch = platform.machine().lower()  # arm64 / x86_64
    if os_name == "darwin":
        os_key = "macos"
    elif os_name == "windows":
        os_key = "windows"
    else:
        os_key = os_name
    return f"{os_key}-{arch}"


def current_platform() -> str:
    """当前宿主机的 ``<os>-<arch>`` 键（如 ``macos-arm64`` / ``linux-x86_64``）。"""
    return _current_platform()


def _parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """从 ``--version`` 输出里抠出 ``(major, minor, patch?)``。

    兼容 ``Python 3.11.9`` 与 ``v20.18.0`` 两种风格；取不到返回 None。
    """
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _split_req(version: str) -> Tuple[Optional[int], Optional[int]]:
    """把 ``"3.11"`` / ``"3"`` 拆成 (major, minor)，缺失的为 None。"""
    m = re.match(r"(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return (None, None)
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else None
    return (major, minor)


def _candidate_paths(kind: str) -> List[str]:
    """按优先级收集某 kind 的候选可执行文件路径（去重保序）。"""
    cfg = _config_framework()
    out: List[str] = []

    explicit = cfg.get("python") if kind == "python" else cfg.get("node")
    if explicit:
        out.append(explicit)

    for p in cfg.get("extra_paths") or []:
        pp = Path(p)
        if pp.is_dir():
            for n in _CANDIDATE_NAMES.get(kind, ()):
                out.append(str(pp / n))
        else:
            out.append(str(pp))

    for n in _CANDIDATE_NAMES.get(kind, ()):
        found = shutil.which(n)
        if found:
            out.append(found)

    seen: set = set()
    res: List[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------
def detect_runtimes(kind: str) -> List[Dict[str, Any]]:
    """探测本机某 kind 的所有可用解释器。

    返回列表，每项 ``{"path", "version": (major,minor,...), "version_string"}``；
    同一可执行文件只计一次（按 realpath 去重）。
    """
    results: List[Dict[str, Any]] = []
    seen_paths: set = set()
    for path in _candidate_paths(kind):
        try:
            rp = str(Path(path).resolve())
            if rp in seen_paths:
                continue
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            continue
        ver = _parse_version(proc.stdout + proc.stderr)
        if ver is None:
            continue
        seen_paths.add(rp)
        line = (proc.stdout + proc.stderr).strip().splitlines()[0]
        results.append(
            {"path": rp, "version": ver, "version_string": line or f"{kind} {ver[0]}.{ver[1]}"}
        )
    # 同版本多路径时按版本降序，便于取「最高版本」
    results.sort(key=lambda d: d["version"], reverse=True)
    return results


def resolve_runtime(kind: str, version: Optional[str] = None) -> Optional[Path]:
    """找到满足 version 要求的本机解释器路径；找不到返回 None。

    - 未指定 version：返回本机最高版本。
    - 指定如 ``"3.11"``：优先 major.minor 恰好匹配；无则退而用最高版本。
    """
    det = detect_runtimes(kind)
    if not det:
        return None
    if version:
        major, minor = _split_req(version)
        if minor is not None:
            exact = [d for d in det if d["version"][0] == major and d["version"][1] == minor]
            if exact:
                return Path(exact[0]["path"])
        else:
            # 只要求 major，如 "3"
            exact = [d for d in det if d["version"][0] == major]
            if exact:
                return Path(exact[0]["path"])
        # 未精确匹配：退回到最高版本（provision 阶段会提示不一致）
        return Path(det[0]["path"])
    return Path(det[0]["path"])


def ensure_runtime(
    kind: str,
    version: Optional[str] = None,
    progress: Optional[Callable[[str, float], None]] = None,
) -> Path:
    """确保某版本运行时可用（检测模式下即「本机有即可」）。

    不再下载：若本机检测不到满足条件的解释器，直接抛错提示用户去安装或在
    config 里指定路径。返回可执行文件绝对路径。
    """
    exe = resolve_runtime(kind, version)
    if exe is None:
        req = f" 需要版本 {version}" if version else ""
        raise RuntimeError(
            f"framework 未检测到可用的 {kind}{req}。"
            f"请在本机安装对应解释器，或在 config.yaml 的 framework 段显式指定路径"
            f"（framework.{kind}: /abs/path/to/{kind}）。"
        )
    if progress:
        progress(f"{kind} {version or ''} 已检测到：{exe}", 1.0)
    return exe


def agent_runtime_paths(agent_id: str) -> Dict[str, Optional[str]]:
    """该 agent 实际应使用的 python / node 路径。

    解析顺序：``agent_overrides[agent_id]`` → framework 全局默认 →
    本机检测到的第一个解释器（应用本身会带环境，默认就有得用）。
    装载时据此拿到「这个 agent 用哪个解释器」，供 provision / 执行层使用。
    """
    from ..config import load_config

    cfg = load_config()
    ov = cfg.agent_overrides.get(agent_id) or {}
    python = ov.get("python") or cfg.framework.python or None
    node = ov.get("node") or cfg.framework.node or None
    if python is None:
        ds = detect_runtimes("python")
        python = str(ds[0]["path"]) if ds else None
    if node is None:
        ds = detect_runtimes("node")
        node = str(ds[0]["path"]) if ds else None
    return {"python": python, "node": node}


# ---------------------------------------------------------------------------
# 下载 / 解包（供 provision 拉取 agent 业务依赖用，非运行时）
# ---------------------------------------------------------------------------
def _download_and_extract(
    url: str,
    dest: Path,
    progress: Optional[Callable[[str, float], None]] = None,
    prefix: str = "下载",
) -> None:
    """下载一个 tar.gz / tar.xz / zip 到临时文件，解包到 dest，再清理临时文件。

    便携版发行包顶层往往带一个版本目录（如 ``python/`` 或 ``node-vX.Y.Z/``），
    这里做「拍平一层」处理：若解包后 dest 内只有一个子目录且非空，就把其内容
    上移到 dest 根，保证 ``dest/bin/python3`` 这类路径稳定。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="keeper-dl-"))
    try:
        archive = tmp / ("pkg" + _suffix(url))
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0) or 0)
            done = 0
            with archive.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(f"{prefix} ...", min(0.95, 0.05 + 0.9 * done / total))
        _extract(archive, tmp / "unpacked")
        # 把解包内容归整到 dest
        _flatten_into(tmp / "unpacked", dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _suffix(url: str) -> str:
    low = url.lower().split("?")[0]
    if low.endswith(".zip"):
        return ".zip"
    if low.endswith((".tar.gz", ".tgz")):
        return ".tar.gz"
    if low.endswith(".tar.xz"):
        return ".tar.xz"
    return ".bin"


def _extract(archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip" or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(out)
    elif archive.name.endswith((".tar.gz", ".tgz", ".tar.xz")):
        mode = "r:gz" if archive.name.endswith((".tar.gz", ".tgz")) else "r:xz"
        with tarfile.open(archive, mode) as t:
            t.extractall(out)
    else:
        raise RuntimeError(f"不支持的归档格式：{archive.name}")


def _flatten_into(src: Path, dest: Path) -> None:
    """把 src 内容搬进 dest，若 src 只有单个非空顶层目录则拍平一层。"""
    items = [p for p in src.iterdir() if p.name != "__MACOSX"]
    if len(items) == 1 and items[0].is_dir():
        src = items[0]
    dest.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        target = dest / p.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(p), str(target))
