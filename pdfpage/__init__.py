"""PDF 页级渲染：把某一页整页变成 PNG。

见 :mod:`pdfpage.render` 的说明——为什么是整页光栅图、为什么不用 PyMuPDF、
为什么不引 Pillow。
"""
from .render import (  # noqa: F401
    DEFAULT_SCALE,
    MAX_PNG_BYTES,
    RenderError,
    encode_png,
    page_count,
    render_page,
    render_pages,
)

__all__ = [
    "DEFAULT_SCALE",
    "MAX_PNG_BYTES",
    "RenderError",
    "encode_png",
    "page_count",
    "render_page",
    "render_pages",
]
