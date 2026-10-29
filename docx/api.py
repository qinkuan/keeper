"""docx 只读渲染的 HTTP 端点。

    GET /documents/docx?path=<绝对路径>

返回 IR 的 JSON。路径必须落在「我的空间」内——复用 zonefilesystem 的
``resolve_target``（越界 403），不给这条端点开第二个后门。

为什么不做"解析任意路径"：keeper 是本机单用户应用（见 doc/security.md），
但这条端点会把**任意 docx 变成结构化数据**返回，等于一个无认证的解析器——
限制在空间内是最低成本的收敛。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/documents", tags=["documents"])

# 单个 docx 的大小上限。docx 是 zip，10MB 的文档解出来可能几十 MB，
# 再加上图片 base64，IR 会更大——超限直接拒绝并说清楚原因。
_MAX_DOCX_BYTES = 30 * 1024 * 1024


@router.get("/docx")
async def get_docx_ir(
    path: str = Query(..., description="docx 文件的绝对路径（必须在「我的空间」内）"),
) -> Dict[str, Any]:
    """把 docx 解析成可渲染的 IR（段落/表格/图片/样式）。

    解析失败（不是 zip、缺 document.xml、损坏）都回 422，并把原因带上——
    前端据此提示"这个文件读不出来，请用系统应用打开"。
    """
    from ..zonefilesystem.space_ops import resolve_target

    try:
        target = resolve_target(path)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"无法定位文件：{e}") from e

    if target.is_dir():
        raise HTTPException(status_code=400, detail="这是一个目录")
    size = target.stat().st_size
    if size > _MAX_DOCX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件太大（{size // 1024 // 1024}MB），暂不支持预览",
        )

    try:
        data = target.read_bytes()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败：{e}") from e

    from .extract import extract_docx_ir
    from .ir import to_json

    try:
        doc = extract_docx_ir(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"这不是一个可解析的 Word 文档：{e}"
        ) from e

    return to_json(doc)
