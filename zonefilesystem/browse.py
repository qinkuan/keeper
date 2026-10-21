"""服务器本机目录浏览（供前端选择工作目录 / 「我的空间」根目录）。

只读列目录，不落库、不写盘。与 space_ops 的「空间内受边界限制的文件操作」区分：
这里不受任何空间边界约束，用于自由挑选任意本机文件夹。
"""
import os
from pathlib import Path
from typing import Any, Dict


def browse_directory(path: str = "") -> Dict[str, Any]:
    """浏览服务器本机目录，供前端在选择工作目录时挑选文件夹。

    只读列目录，返回目录与文件两类条目（目录可进入/选择，文件仅展示），
    每条带 size（字节，目录为 null）与 mtime（修改时间，epoch 秒）。
    """
    # Linux/macOS 从用户 home 目录开始浏览；Windows 回退到系统盘根。
    _default_root = Path.home() if os.name != "nt" else Path("/")
    root = Path(path).expanduser() if path else _default_root
    if not root.is_absolute():
        root = _default_root
    # 不存在或不是目录：回退到父目录（再不行就用 home）
    if not root.is_dir():
        root = root.parent if root.parent != root else Path.home()

    def _info(e: Path) -> Dict[str, Any]:
        info: Dict[str, Any] = {"name": e.name, "path": str(e)}
        try:
            st = e.stat()
            info["size"] = st.st_size if e.is_file() else None
            info["mtime"] = int(st.st_mtime)
        except OSError:
            info["size"] = None
            info["mtime"] = None
        return info

    try:
        entries = sorted(root.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        return {
            "path": str(root),
            "parent": None,
            "dirs": [],
            "files": [],
            "error": "无权限访问该目录",
        }
    except OSError as e:  # noqa: BLE001
        return {
            "path": str(root),
            "parent": None,
            "dirs": [],
            "files": [],
            "error": f"无法读取目录: {e}",
        }
    dirs = [_info(e) for e in entries if e.is_dir()]
    files = [_info(e) for e in entries if e.is_file()]
    parent = str(root.parent) if root != root.parent else None
    return {"path": str(root), "parent": parent, "dirs": dirs, "files": files, "error": None}
