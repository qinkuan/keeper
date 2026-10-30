"""PDF 页渲染端点（/documents/pdf/info、/documents/pdf/page）的自测。

跑法：keeper/.venv/bin/python keeper/tests/test_pdf_page_api.py

覆盖：元信息（页数/尺寸）、整页 PNG 渲染、中文能不能画出来（这份 PDF 的
中文是**未嵌入字体**——浏览器里的 pdf.js 会整片丢掉，服务端 PDFium 才画得出）、
路径边界（空间外 403 / 不存在 404 / 非 PDF 422）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}{('  → ' + extra) if extra else ''}")


def _make_pdf(path: Path, pages: int = 3) -> None:
    """造一个最小 PDF（用 pypdf，纯 Python）。"""
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=100)
    with open(path, "wb") as f:
        w.write(f)


def main() -> None:
    from fastapi import HTTPException

    from keeper.pdfpage.api import pdf_info, pdf_page

    tmp = Path(tempfile.mkdtemp())
    space = tmp / "space"
    space.mkdir()
    # 把「我的空间」指到临时目录：KEEPER_SETTINGS 指向临时 settings.yaml，
    # 这样 resolve_target 的空间校验对着临时目录走，不碰用户真实设置。
    os.environ["KEEPER_SETTINGS"] = str(tmp / "settings.yaml")
    from keeper.setting import user_settings

    user_settings.set_user_space_path(str(space))

    pdf = space / "测试.pdf"
    _make_pdf(pdf, pages=3)

    print("\n=== 元信息 ===")
    try:
        info = asyncio.run(pdf_info(path=str(pdf)))
        check("页数 = 3", info["pages"] == 3, str(info))
        check("页面尺寸（pt）正确", abs(info["width"] - 200) < 1 and abs(info["height"] - 100) < 1,
              str(info))
    except Exception as e:  # noqa: BLE001
        check("元信息能取到", False, f"{type(e).__name__}: {e}")

    print("\n=== 整页渲染 ===")
    try:
        resp = asyncio.run(pdf_page(path=str(pdf), page=2, width=400))
        body = resp.body
        check("返回 image/png", resp.media_type == "image/png", resp.media_type)
        check("是合法 PNG", body[:8] == b"\x89PNG\r\n\x1a\n", repr(body[:8]))
        check("体积合理（<1MB）", len(body) < 1024 * 1024, f"{len(body) // 1024}KB")
        check("带缓存头", "max-age" in (resp.headers.get("cache-control") or ""),
              str(resp.headers.get("cache-control")))
        # 宽度 400 / 页宽 200pt → 缩放 2 倍 → 像素宽 400
        w = int.from_bytes(body[16:20], "big")
        h = int.from_bytes(body[20:24], "big")
        check("渲染宽度按请求的像素宽", abs(w - 400) <= 2, str(w))
        check("保持页面比例", abs((w / h) - 2.0) < 0.05, f"{w}x{h}")
    except Exception as e:  # noqa: BLE001
        check("能渲染出 PNG", False, f"{type(e).__name__}: {e}")

    print("\n=== 越界 / 坏文件 ===")
    outside = tmp / "空间外.pdf"
    _make_pdf(outside, pages=1)
    try:
        asyncio.run(pdf_info(path=str(outside)))
        check("空间外的文件被拒", False, "居然没报错")
    except HTTPException as e:
        check("空间外的文件被拒（403）", e.status_code == 403, str(e.status_code))
    except Exception as e:  # noqa: BLE001
        check("空间外的文件被拒", False, f"{type(e).__name__}: {e}")

    try:
        asyncio.run(pdf_info(path=str(space / "不存在.pdf")))
        check("不存在的文件被拒", False, "居然没报错")
    except HTTPException as e:
        check("不存在的文件被拒（404）", e.status_code == 404, str(e.status_code))
    except Exception as e:  # noqa: BLE001
        check("不存在的文件被拒", False, f"{type(e).__name__}: {e}")

    bad = space / "坏的.pdf"
    bad.write_text("not a pdf at all", encoding="utf-8")
    try:
        asyncio.run(pdf_info(path=str(bad)))
        check("坏文件被拒", False, "居然没报错")
    except HTTPException as e:
        check("坏文件被拒（422）", e.status_code == 422, str(e.status_code))
    except Exception as e:  # noqa: BLE001
        check("坏文件被拒", False, f"{type(e).__name__}: {e}")

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
