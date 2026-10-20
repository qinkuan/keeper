"""「我的空间」文件操作逻辑（与 Web 框架无关，只处理真实文件系统）。

所有函数都接收一个位于「我的空间」内的路径字符串，并校验其不越界；
越界 / 不存在 / 类型不符时抛出 fastapi.HTTPException。Web 层（chat/api.py）
只负责解析请求、再把活儿交给这里。
"""
import mimetypes
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ..setting import get_user_space_path

# 读文件接口对超过此大小（2MB）的文件只回元数据、不回内容
_MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def space_root() -> Path:
    """解析「我的空间」根目录（来自 settings.yaml）；未配置则报错。"""
    raw = get_user_space_path()
    if not raw:
        raise HTTPException(status_code=400, detail="尚未配置「我的空间」")
    return Path(raw).expanduser().resolve()


def resolve_target(path: str, *, allow_dir: bool = False) -> Path:
    """校验 path 位于「我的空间」内并返回绝对路径；越界 / 不存在 / 类型不符则报错。

    所有写操作（上传 / 下载 / 重命名 / 删除 / 打开）都先过这一关，保证前端即便被
    篡改也碰不到空间之外的服务器文件。
    """
    root = space_root()
    target = Path(path).expanduser().resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=403, detail="只能操作「我的空间」内的文件")
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not allow_dir and target.is_dir():
        raise HTTPException(status_code=400, detail="这是一个目录")
    return target


def read_space_file(path: str) -> Dict[str, Any]:
    """读取「我的空间」内某文件的内容（受限）。

    二进制 / 超过 2MB 的文件只回元数据、不回内容（前端据此展示「无法预览」）。
    """
    target = resolve_target(path)
    st = target.stat()
    size = st.st_size
    mime, _ = mimetypes.guess_type(str(target))
    too_large = size > _MAX_PREVIEW_BYTES
    is_text = False
    text = ""
    if not too_large:
        try:
            data = target.read_bytes()
            data.decode("utf-8")
            is_text = True
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            is_text = False
    return {
        "path": str(target),
        "name": target.name,
        "mime": mime,
        "size": size,
        "mtime": st.st_mtime,
        "is_dir": False,
        "is_text": is_text,
        "too_large": too_large,
        "text": text,
    }


def open_in_explorer(path: str, allow_dir: bool) -> Dict[str, Any]:
    """在系统文件管理器里定位到本空间 / 某文件（等同 PyCharm 的 Reveal）。"""
    target = resolve_target(path, allow_dir=allow_dir) if path else space_root()
    if target.is_dir():
        loc = target
    elif sys.platform == "darwin":
        loc = target  # open -R 会选中文件
    else:
        loc = target.parent
    if sys.platform == "darwin":
        args = ["open", "-R", str(target)] if not target.is_dir() else ["open", str(loc)]
    elif sys.platform == "win32":
        args = ["explorer", (f'/select,"{target}"' if not target.is_dir() else str(target))]
    else:
        args = ["xdg-open", str(loc)]
    try:
        subprocess.Popen(args)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"无法打开文件管理器：{e}")
    return {"ok": True}


def upload_to_space(filename: str, data: bytes, dir_rel: str = "") -> Dict[str, Any]:
    """把文件写入「我的空间」（拷入）。dir_rel 为相对空间根的子目录，缺省为根。"""
    root = space_root()
    dest = (root / dir_rel).resolve() if dir_rel else root
    if dest != root and root not in dest.parents:
        raise HTTPException(status_code=403, detail="目标目录越界")
    dest.mkdir(parents=True, exist_ok=True)
    # 防文件名穿越：只取最后一段、丢弃任何路径分隔
    raw_name = Path(filename or "upload.bin").name
    if not raw_name or raw_name in (".", ".."):
        raw_name = "upload.bin"
    target = (dest / raw_name).resolve()
    if target != dest and root not in target.parents:
        raise HTTPException(status_code=400, detail="非法文件名")
    target.write_bytes(data)
    return {"ok": True, "path": str(target), "name": target.name}


def download_from_space(path: str) -> FileResponse:
    """从「我的空间」下载文件（拷出）。"""
    target = resolve_target(path)
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        str(target),
        media_type=mime or "application/octet-stream",
        filename=target.name,
        content_disposition_type="attachment",
    )


def delete_in_space(path: str) -> Dict[str, Any]:
    """删除「我的空间」内的文件或目录。"""
    target = resolve_target(path, allow_dir=True)
    if target == space_root():
        raise HTTPException(status_code=400, detail="不能删除「我的空间」根目录")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}


def rename_in_space(path: str, name: str) -> Dict[str, Any]:
    """重命名「我的空间」内的文件或目录（不可重命名根）。"""
    target = resolve_target(path, allow_dir=True)
    if target == space_root():
        raise HTTPException(status_code=400, detail="不能重命名根目录")
    new_name = Path(name or "").name
    if not new_name or new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="非法名称")
    new_path = (target.parent / new_name).resolve()
    if new_path != target and (
        new_path == space_root() or space_root() not in new_path.parents
    ):
        raise HTTPException(status_code=400, detail="非法名称（越界）")
    target.rename(new_path)
    return {"ok": True, "path": str(new_path), "name": new_path.name}


def mkdir_in_space(path: str, name: str) -> Dict[str, Any]:
    """在指定父目录下新建子文件夹。"""
    parent = resolve_target(path, allow_dir=True)
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="父目录不存在")
    name = Path(name or "").name
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="非法名称")
    new_dir = (parent / name).resolve()
    if new_dir != parent and (
        new_dir == space_root() or space_root() not in new_dir.parents
    ):
        raise HTTPException(status_code=400, detail="非法名称（越界）")
    new_dir.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(new_dir), "name": name}
