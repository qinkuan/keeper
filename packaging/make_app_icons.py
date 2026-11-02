#!/usr/bin/env python3
"""生成桌面应用的图标（macOS 用的 icon.png + Windows 用的 icon.ico）。

为什么需要它
------------
electron-builder 默认用 **Electron 自带的图标**（mac: Electron.icns /
win: electron.ico）—— 装出来的应用在访达 / 任务栏 / 开始菜单里一眼就是
「一个 Electron 应用」，完全看不出是 keeper。

keeper 的样式图标是一份矢量源：``desktop/icon.svg``
（白色圆角方块 + 蓝色 K）。两个平台要的图还不一样：

- **macOS**：要一张 1024×1024、**不带圆角、铺满**的方图（icon.png）。
  圆角是**系统加的**（macOS 11+ 会给 App 图标套一层 squircle 遮罩）——自己带
  圆角就会变成「圆角套圆角」，四个角被啃掉一圈。.icns 不自己拼，交给
  **electron-builder**（app-builder）转：它那条管线产出的 icns 是 Electron 自
  己也在用的（已被 macOS 验证过）。曾试过 iconutil，它写出的 icns 条目长度
  字段是坏的，macOS 读出来是「半张图上下拼贴」，不能用。
- **Windows**：.ico，圆角要**自己带**（Windows 不加遮罩）。ICO 容器自己拼
  （条目全用 PNG，保住透明圆角；用 BMP 条目的话四角会变黑角）。

SVG → PNG 用 **Playwright 的 Chromium**（本机已装）：它能正确处理 SVG 里的
渐变、圆角与字体，比自己手写光栅化靠谱得多。

产物**提交进仓库**（desktop/build/，也就是 electron-builder 的 buildResources
目录）：

- CI 的 Windows runner 上没有 Playwright，不该在打包时临时生成图标
  （渲染器/字体不同会得到不一致的图标）；
- 改了 favicon.svg 之后手动跑一次本脚本即可重新生成。

用法::

    .venv/bin/python packaging/make_app_icons.py
    .venv/bin/python packaging/make_app_icons.py --svg path/to/icon.svg
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List

# 仓库根 = 本文件的上一级（packaging/ → 仓库根）
ROOT = Path(__file__).resolve().parents[1]
# 应用图标的矢量源（白底蓝 K）。**不复用** official-web/favicon.svg：
# 那是网页 favicon（蓝底白 K），与桌面应用图标（白底蓝 K）是两种观感。
DEFAULT_SVG = ROOT / "desktop" / "icon.svg"
# electron-builder 的 buildResources 目录（desktop/electron-builder.yml 里
# `directories.buildResources: build`）
OUT_DIR = ROOT / "desktop" / "build"

# macOS 的应用图标：1024×1024 的方图（圆角由系统加）
MAC_PNG_SIZE = 1024
MAC_PNG_NAME = "icon.png"

# Windows .ico 里放哪些尺寸（像素）
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICO_NAME = "icon.ico"


def _log(msg: str) -> None:
    print(f"[icons] {msg}", flush=True)


# ---------------------------------------------------------------------------
# SVG 变体
# ---------------------------------------------------------------------------
def square_variant(svg_text: str) -> str:
    """把 SVG 里的圆角矩形改成直角矩形（备用）。

    macOS 的应用图标**可以**自带圆角（ChatGPT / Spotify 等系统应用都是
    「白底圆角方块 + 透明外圈」这样），所以默认不做这个变换；留着这个函数
    是为了哪天想改成方角底时方便用。
    """
    out = re.sub(r'\brx\s*=\s*"[^"]*"', 'rx="0"', svg_text)
    out = re.sub(r"\brx\s*=\s*'[^']*'", 'rx="0"', out)
    return out


#: macOS 图标本体占 1024 画布的比例（Apple 规范：图标本体 824×824，四周留白，
#: 拼在 Dock 里才和系统应用一样大）。
#: 824 / 1024 = 0.8047
MACOS_ICON_SCALE = 0.805


def macos_variant(svg_text: str) -> str:
    """macOS 用的图标：**整体缩到 80.5% 并居中**（四周留透明边距）。

    为什么：macOS 的应用图标不是铺满 1024 画布的。Apple 的规范是
    「图标本体 824×824 放在 1024 画布中央」——四周那圈透明边距才是标准
    应用图标的视觉尺寸。铺满 1024 的图标在 Dock 里会比系统应用明显大一圈
    （macOS 并不会去裁切第三方图标，尺寸得自己守规范）。

    圆角半径会随整体一起缩放：源 SVG 里白块是满幅 32 单位、rx=7.4
    （23%），缩到 25.8 单位后 rx≈5.96 → 1024 画布里约 191px，与 Apple 规范
    的 ~185px 基本一致。
    """
    m = re.search(r"(<svg[^>]*>)(.*)(</svg>)", svg_text, re.S)
    if not m:
        return svg_text
    head, inner, tail = m.groups()
    scale = MACOS_ICON_SCALE
    off = 16 * (1 - scale)   # 居中平移（viewBox 是 0 0 32 32）
    return (
        f'{head}<g transform="translate({off:.4f} {off:.4f}) scale({scale})">'
        f"{inner}</g>{tail}"
    )


# ---------------------------------------------------------------------------
# SVG → PNG
# ---------------------------------------------------------------------------
def _launch_browser(pw):
    """启动一个能渲染 SVG 的 Chromium。

    优先用 Playwright 自带的 Chromium；**没装浏览器**（升级 playwright 后
    缓存对不上）就退回系统装的 Chrome / Edge —— 省掉一次几百 MB 的下载。
    """
    tried = []
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            browser = pw.chromium.launch(**kwargs)
            _log(f"用浏览器渲染：{kwargs or 'playwright 自带 chromium'}")
            return browser
        except Exception as e:  # noqa: BLE001
            first = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            tried.append(f"{kwargs.get('channel') or 'bundled chromium'} → {first}")
    raise SystemExit(
        "没有可用的 Chromium。任选一种：\n"
        "  1) 装 Playwright 自带的浏览器：.venv/bin/playwright install chromium\n"
        "  2) 装 Google Chrome 或 Edge（脚本会自动用它们）\n"
        "尝试记录：\n  " + "\n  ".join(tried)
    )


def render_pngs(svg_text: str, sizes: List[int]) -> Dict[int, bytes]:
    """用 Chromium 把 SVG 渲染成若干尺寸的 PNG。

    返回 ``{尺寸: png 字节}``。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - 环境没装
        raise SystemExit(
            "需要 Playwright 才能把 SVG 渲染成 PNG：\n"
            "  .venv/bin/pip install playwright && .venv/bin/playwright install chromium\n"
            f"（原始错误：{e}）"
        ) from e

    out: Dict[int, bytes] = {}
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        try:
            page = browser.new_page()
            for size in sorted(set(sizes)):
                # 页面本身零边距，SVG 撑满视口 —— 截图才是正好 size×size
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<style>html,body{margin:0;padding:0;background:transparent}"
                    "svg{display:block;width:%dpx;height:%dpx}</style></head>"
                    "<body>%s</body></html>" % (size, size, svg_text)
                )
                page.set_viewport_size({"width": size, "height": size})
                page.set_content(html, wait_until="load")
                out[size] = page.screenshot(omit_background=True)
                _log(f"渲染 {size}×{size} → {len(out[size])} 字节")
        finally:
            browser.close()
    return out


# ---------------------------------------------------------------------------
# .ico（Windows）
# ---------------------------------------------------------------------------
def build_ico(pngs: Dict[int, bytes], out_path: Path) -> None:
    """拼一个 Windows .ico。

    **全部用 PNG 条目**（Vista+ 原生支持 PNG-in-ICO）：ICO 里的 BMP/DIB 条目
    没有可靠的 alpha 通道，而 keeper 图标是**圆角方块**（四角透明）—— 用 BMP
    存的话四角会变成黑角。PNG 条目既保留透明圆角，体积还更小。

    electron-builder 会把这个 .ico 嵌进 exe 的 PE 资源。
    """
    entries = [(size, pngs[size]) for size in ICO_SIZES]

    count = len(entries)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    directory = bytearray()
    body = bytearray()
    for size, data in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 256 在目录项里记作 0
            0 if size >= 256 else size,
            0,        # 调色板数
            0,        # 保留
            1,        # planes
            32,       # bpp
            len(data),
            offset,
        )
        body += data
        offset += len(data)
    out_path.write_bytes(bytes(header) + bytes(directory) + bytes(body))
    _log(f"生成 {out_path}（{out_path.stat().st_size} 字节，{count} 个尺寸）")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="生成 keeper 桌面应用的图标")
    ap.add_argument("--svg", default=str(DEFAULT_SVG), help="图标源（SVG），默认用官网 favicon")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录，默认 desktop/build")
    args = ap.parse_args()

    svg_path = Path(args.svg)
    if not svg_path.is_file():
        raise SystemExit(f"找不到图标源：{svg_path}")
    svg_text = svg_path.read_text(encoding="utf-8")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    _log(f"图标源：{svg_path}")

    # macOS：整体缩到 80.5% 居中（Apple 规范：图标本体 824×824，四周留白）
    mac_pngs = render_pngs(macos_variant(svg_text), [MAC_PNG_SIZE])
    mac_png = out_dir / MAC_PNG_NAME
    mac_png.write_bytes(mac_pngs[MAC_PNG_SIZE])
    _log(f"生成 {mac_png}（{mac_png.stat().st_size} 字节，{MAC_PNG_SIZE}×{MAC_PNG_SIZE}）")

    # Windows：铺满画布的圆角白底（Windows 没有留白规范，图标就是满幅的）
    win_pngs = render_pngs(svg_text, ICO_SIZES)
    build_ico(win_pngs, out_dir / ICO_NAME)

    _log("完成。electron-builder 会把 icon.png 转成 .icns、把 icon.ico 嵌进 exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
