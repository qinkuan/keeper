"""PDF 页渲染的 HTTP 端点（给「我的空间」内联预览用）。

    GET /documents/pdf/info?path=<绝对路径>
        → {"pages": 69, "width": 595.3, "height": 807.9}   （尺寸单位 pt）

    GET /documents/pdf/page?path=<绝对路径>&page=<1-based>&width=<像素宽>
        → image/png（整页渲染图）

为什么在**服务端**渲染，而不是浏览器里的 pdf.js
-------------------------------------------------
pdf.js（浏览器端）碰到**未嵌入字体**的 PDF 时，只能画出 ASCII，中文整片消失
（实测：网易云音乐半年报.pdf 的繁体中文、标题、表格标签全没了，只剩数字）。
它没有 CJK 回退字体可用——那是浏览器沙箱的硬限制。

PDFium（内核里 ``pdfpage/render.py`` 用的那套，Chrome 看 PDF 就是它）自带 CJK
回退，同一份文件渲染出来是完整的。顺带还有两个好处：

- 还原度与系统里看 PDF 一致（用户看到的和 Word/预览一致）；
- 页面图可以走 HTTP 缓存，滚回去不用重渲染。

代价是渲染发生在内核里（每页几十毫秒）——对"看得见"这件事来说，这笔买卖
很划算。

路径必须落在「我的空间」内——复用 zonefilesystem 的 ``resolve_target``
（越界 403），不给这条端点开第二个后门。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

router = APIRouter(prefix="/documents", tags=["documents"])

# 单个 PDF 的大小上限（渲染时要整份读进内存）。
_MAX_PDF_BYTES = 100 * 1024 * 1024

# 渲染倍率（相对 72dpi）的上下限：太小看不清，太大没意义还慢。
_MIN_SCALE = 0.4
_MAX_SCALE = 4.0

# 单页 PNG 的体积上限（pdfpage.render 内部会自动降倍率重试）。
_MAX_PNG_BYTES = 8 * 1024 * 1024


def _resolve(path: str):
    """把「我的空间」内的路径解析成真实文件（越界 403）。"""
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
    if size > _MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件太大（{size // 1024 // 1024}MB），暂不支持预览",
        )
    return target


@router.get("/pdf/info")
async def pdf_info(
    path: str = Query(..., description="PDF 文件的绝对路径（必须在「我的空间」内）"),
) -> Dict[str, Any]:
    """PDF 的页数与首页尺寸（pt）。前端用它算占位高度与缩放。"""
    target = _resolve(path)
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(target))
    except Exception as e:  # noqa: BLE001 损坏 / 加密 / 不是 PDF
        raise HTTPException(status_code=422, detail=f"PDF 打开失败：{e}") from e
    try:
        pages = len(doc)
        if pages <= 0:
            raise HTTPException(status_code=422, detail="这个 PDF 没有页面")
        w, h = doc[0].get_size()
    finally:
        doc.close()
    return {"pages": pages, "width": float(w), "height": float(h)}


@router.get("/pdf/page")
async def pdf_page(
    path: str = Query(..., description="PDF 文件的绝对路径（必须在「我的空间」内）"),
    page: int = Query(..., ge=1, description="页码（1-based）"),
    width: int = Query(
        900, ge=64, le=4096, description="渲染出的像素宽度（按页宽等比缩放）"
    ),
) -> Response:
    """把第 ``page`` 页整页渲染成 PNG。"""
    target = _resolve(path)
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(target))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"PDF 打开失败：{e}") from e
    try:
        total = len(doc)
        if page > total:
            raise HTTPException(status_code=404, detail=f"页码 {page} 超出范围（共 {total} 页）")
        page_w, _ = doc[page - 1].get_size()
    finally:
        doc.close()
    if not page_w:
        raise HTTPException(status_code=422, detail="页面尺寸异常")

    from ..pdfpage.render import RenderError, render_page

    scale = max(_MIN_SCALE, min(_MAX_SCALE, width / float(page_w)))
    try:
        png = render_page(target, page, scale=scale)
    except RenderError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"页面渲染失败：{e}") from e

    if len(png) > _MAX_PNG_BYTES:
        raise HTTPException(status_code=413, detail="这一页渲染出来太大")
    return Response(
        content=png,
        media_type="image/png",
        headers={
            # 同一份文件同一页同一宽度 → 浏览器直接命中缓存，滚回去不重渲染
            "Cache-Control": "private, max-age=3600",
        },
    )
