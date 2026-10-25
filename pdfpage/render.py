"""把 PDF 的某一页**整页渲染**成 PNG（页级光栅图）。

为什么是「整页光栅图」而不是「抽文字」
--------------------------------------
给 agent 看 PDF 有两条路：抽文字层，或者把页面渲染成图。

抽文字会丢东西，而且是**丢最关键的东西**：版面。图表里的数据、表格的行列
对应关系、示意图、代码块的缩进结构——文字层里这些全都不存在（pypdf 抽出来
的是一堆按绘制顺序排列的字符串，两栏排版还会左右交替）。模型看到的是
「一段没有布局的文本」，它可以读懂字面意思，却**读不懂表格**。

整页渲染则相反：页面上有什么就是什么，包括版式。代价是模型要自己"看图识字"，
且厂商侧会按最大边缩放（可能把正文小字缩到看不清）——但那是分辨率问题，
可以用更大的 scale 换，而**信息本身不会丢**。

所以本模块只做一件事：**把第 N 页变成一张 PNG**。检索（定位到第几页）
是 :mod:`search` 的事，这里不关心。

为什么不用 PyMuPDF
-----------------
PyMuPDF 是 **AGPL** 协议，会传染到整个产品，没法用在闭源桌面应用里。
这里用 ``pypdfium2``——PDFium（Google）的 Python 绑定，**Apache-2.0 / BSD-3
双许可任选**，且官方提供 macOS / Windows / Linux 全平台预编译 wheel
（自带 PDFium 二进制，不需要本地编译工具链，Windows 上也不需要额外装
VC++ 运行库）。

为什么自己编码 PNG
------------------
``pypdfium2`` 的 ``render()`` 默认返回 PIL Image，那就得拖进 Pillow
（另一个二进制依赖）。但它同时提供 ``PdfBitmap.to_numpy()``——直接给 numpy
数组。numpy keeper 已经有了，于是 PNG 编码自己写（zlib + 几十行），
**整条链上不引入 Pillow**。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

#: 默认渲染倍率。PDF 的坐标基准是 72 DPI，scale=2 即 **144 DPI**。
#:
#: 为什么不是 1.0：1.0 只有 72 DPI，正文 10pt 的字高约 10px——模型几乎读不出
#: 字。2.0 是「能读」与「文件大小」之间的平衡点（A4 → 1191×1684，PNG 约
#: 300~800 KB）。再往上（3.0 = 216 DPI）字更清楚，但体积与上传时间线性上涨。
DEFAULT_SCALE = 2.0

#: 单页 PNG 的体积上限（字节）。超过就说明这个 PDF 页面异常复杂（比如
#: 巨幅矢量图），传上去既慢又可能被厂商拒；此时降级到更小的 scale。
MAX_PNG_BYTES = 8 * 1024 * 1024


class RenderError(RuntimeError):
    """渲染失败（PDF 打不开 / 加密 / 页码越界 / 未装 pypdfium2）。"""


def _pdfium():
    """取 pypdfium2 模块；没装要说清楚怎么装（而不是抛 ImportError 让人一头雾水）。"""
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # pragma: no cover - 环境没装
        raise RenderError(
            "PDF 页面渲染需要 pypdfium2（pip install 'pypdfium2>=5.14,<6'）。"
            "没装时无法给 agent 提供 PDF 页面截图。"
        ) from e
    return pdfium


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """PNG 的一个数据块：长度 + 类型 + 数据 + CRC32。"""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(rgb) -> bytes:
    """把 numpy 的 (H, W, 3) uint8 数组编码成 PNG 字节。

    自己写而不引 Pillow：Pillow 是二进制依赖（要编译 / 要匹配平台 wheel），
    而 PNG 编码本身只有几十行——不值得为它多一个依赖。

    只支持 8-bit truecolor（无 alpha）：页面渲染出来的图不需要透明通道。
    """
    import numpy as np

    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise RenderError(f"期望 (H, W, 3) 的 RGB 数组，拿到 {arr.shape}")
    h, w = arr.shape[0], arr.shape[1]

    # PNG 的每行前面要带一个 filter 字节；0 = None（不预测，直接存原值）。
    raw = np.zeros((h, w * 3 + 1), dtype=np.uint8)
    raw[:, 1:] = arr.reshape(h, w * 3)
    compressed = zlib.compress(raw.tobytes(), 6)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def page_count(path) -> int:
    """PDF 的总页数。"""
    pdfium = _pdfium()
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:  # noqa: BLE001 损坏 / 加密 / 不是 PDF
        raise RenderError(f"PDF 打开失败：{e}") from e
    try:
        return len(doc)
    finally:
        doc.close()


def render_pages(
    path,
    pages: Sequence[int],
    scale: float = DEFAULT_SCALE,
    max_bytes: int = MAX_PNG_BYTES,
) -> List[bytes]:
    """把指定页渲染成 PNG 字节列表（**按传入顺序**）。

    ``pages`` 用 **1-based 页码**（和 PDF 阅读器、和 ``locator`` 里写的
    「第 12 页」一致）。越界的页码会跳过（不报错）——调用方拿到的长度
    可能小于传入的长度，所以要按返回值判断实际渲染了几页。

    体积超限时**自动降一档倍率**再试（最多降到 1.0）：巨幅矢量页在
    scale=2 下可能生成十几 MB 的 PNG，既慢又超厂商上限。
    """
    pdfium = _pdfium()
    path = Path(path)
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:  # noqa: BLE001
        raise RenderError(f"PDF 打开失败：{e}") from e

    out: List[bytes] = []
    try:
        total = len(doc)
        for p in pages:
            idx = int(p) - 1
            if idx < 0 or idx >= total:
                continue
            out.append(_render_one(doc[idx], scale, max_bytes))
    finally:
        doc.close()
    return out


def _render_one(page, scale: float, max_bytes: int) -> bytes:
    """渲染单页；体积超限就降一档倍率重试。"""
    cur = float(scale)
    while True:
        bitmap = page.render(scale=cur)
        try:
            png = encode_png(bitmap.to_numpy())
        finally:
            # PdfBitmap 持有 PDFium 的原生内存；不显式关闭会一直占着
            # （一页 1191×1684×4 ≈ 8 MB，渲几十页就上百 MB）。
            try:
                bitmap.close()
            except Exception:  # noqa: BLE001 关不掉也不该让整次渲染失败
                pass
        if len(png) <= max_bytes or cur <= 1.0:
            return png
        cur = max(1.0, cur - 0.5)


def render_page(path, page: int, scale: float = DEFAULT_SCALE) -> bytes:
    """渲染单页（1-based 页码），返回 PNG 字节。"""
    out = render_pages(path, [page], scale=scale)
    if not out:
        raise RenderError(f"页码 {page} 超出文档范围")
    return out[0]
