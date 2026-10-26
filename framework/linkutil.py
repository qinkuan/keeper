"""跨平台的「建链接」：POSIX 用符号链接，Windows 用 junction / 硬链接。

背景
----
keeper 用链接表达三件事：

- **插件绑定**：``~/.keeper/agents/<id>/plugins/<name>`` → 插件库里的目录
  （plugin/lib.py）
- **工作区软链**：会话的工作区指向真实目录（chat/api.py）
- **会话目录软链**（zonefilesystem/session_link.py）

Windows 上建符号链接要**开发者模式或管理员权限**（SeCreateSymbolicLinkPrivilege），
普通用户直接 PermissionError —— 插件绑定就绑不上，agent 连 bash 插件都没有。

所以 Windows 上换用不需要特权的机制：

- **目录** → junction（``mklink /J``）：同卷即可，**不需要任何特权**
- **文件** → 硬链接（``mklink /H``）：同卷即可，也不需要特权

POSIX 上维持符号链接（相对链接，换机器/换 home 也不失效）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def _is_windows() -> bool:
    return os.name == "nt"


def make_dir_link(target: Path, link: Path, relative: bool = True) -> Path:
    """让 ``link`` 指向目录 ``target``（link 不存在时创建）。

    POSIX：符号链接（默认相对链接）。
    Windows：junction（目录）/ 退回符号链接（跨卷时 junction 用不了）。
    """
    target = Path(target)
    link = Path(link)
    if _is_windows():
        return _windows_link(target, link, is_dir=True)
    rel = os.path.relpath(target, link.parent) if relative else str(target)
    link.symlink_to(rel, target_is_directory=True)
    return link


def make_file_link(target: Path, link: Path, relative: bool = True) -> Path:
    """让 ``link`` 指向文件 ``target``。"""
    target = Path(target)
    link = Path(link)
    if _is_windows():
        return _windows_link(target, link, is_dir=False)
    rel = os.path.relpath(target, link.parent) if relative else str(target)
    link.symlink_to(rel)
    return link


def _windows_link(target: Path, link: Path, is_dir: bool) -> Path:
    """Windows 上建 junction（目录）/ 硬链接（文件）。

    junction 只能指向**同卷**的绝对路径；跨卷时退回符号链接（需要特权，
    失败就把真实错误抛给调用方）。
    """
    target = target.resolve()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        return link
    if is_dir and _same_volume(target, link.parent):
        cmd = ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)]
    elif not is_dir and _same_volume(target, link.parent):
        cmd = ["cmd.exe", "/c", "mklink", "/H", str(link), str(target)]
    else:
        cmd = ["cmd.exe", "/c", "mklink", str(link), str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return link
    # 兜底：mklink 失败（跨卷 / 权限）→ 用 Python 的符号链接再试一次
    try:
        if is_dir:
            link.symlink_to(target, target_is_directory=True)
        else:
            link.symlink_to(target)
    except OSError:
        raise OSError(
            f"无法创建链接 {link} -> {target}："
            f"mklink 失败（{proc.stderr.strip() or proc.stdout.strip()}）"
        )
    return link


def _same_volume(a: Path, b: Path) -> bool:
    """两个路径在同一个盘符上吗（Windows 上 junction/硬链接要求同卷）。"""
    try:
        return os.path.splitdrive(str(a.resolve()))[0].lower() == os.path.splitdrive(str(b))[0].lower()
    except Exception:  # noqa: BLE001
        return False


def is_link(path) -> bool:
    """是链接吗（POSIX 符号链接 / Windows junction 都算）。

    注意：Windows 的 junction **不是** symlink，``Path.is_symlink()`` 判不出来
    （Python 3.12+ 才有 ``is_junction()``）——凡是「这是不是一个链接」的判断
    都要走这个函数，否则 Windows 上会把 junction 当成真实目录。
    """
    p = Path(path)
    if p.is_symlink():
        return True
    checker = getattr(p, "is_junction", None)
    try:
        return bool(checker and checker())
    except OSError:
        return False


def remove_link(link: Path) -> bool:
    """删掉一个链接（是链接就删；是真实目录/文件则不碰）。"""
    link = Path(link)
    is_junction = hasattr(link, "is_junction") and link.is_junction()
    if not (link.is_symlink() or is_junction):
        return False
    try:
        if is_junction or (link.is_dir() and not link.is_symlink()):
            os.rmdir(link)  # junction 在 Windows 上是目录，得用 rmdir 删
        else:
            link.unlink()
        return True
    except OSError:
        return False
