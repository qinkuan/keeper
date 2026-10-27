"""内置交付工具：``fs.deliver``（文件 / 多模态产物的**唯一正式交付入口**）。

读 / 写 / 列目录 / 编辑 / 搜索文件一律走 ``bash``（cat / ls / find / 编辑器），
不再提供对应的 fs.* 工具，避免与 bash 能力重复、也少一层封装。

``fs.deliver`` 只负责把 agent 已用 bash 生成在工作区里的文件「声明」为本次对话的
产物（见 ``chat.service._capture_artifacts``），由前端按块的 ``kind`` 选渲染器
（图片预览、HTML 沙箱、代码高亮、图表、表格、流程图…）。

工厂签名统一为 ``make_xxx(root, read_only) -> ProcessorTool``：
``root`` / ``read_only`` 是**兜底值**（装配期传入的 agent 默认工作目录），
运行时优先取当前请求的工作空间（``chat.context.current_workspace``）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Optional

from ..base import ProcessorTool
from .sandbox import (
    PathEscape,
    resolve_path,
    is_scratch,
)
from ...viewer import parse_blocks, block_file_ref, validate_chart_spec


def _integrity_hint(rel: str, content: str) -> str:
    """写完立刻粗检一遍内容完整性；没问题返回空串。

    为什么必须做：这层存在的意义是**把静默失败变成信号**。工具此前只回
    「已写入 N 字符」——那是**操作结果**，不是**达成情况**。模型据此理所当然
    认为文件写好了，然后在 FINAL 里向用户断言「点开就能跑」，而文件可能
    是个缺尾标签的残片。这个差异对纯文本无所谓，对「产物卡片」是致命的。

    判定全部是启发式，只在**明显可疑**时出声，宁可漏报也不误报——误报会让
    模型反复去「修」一个本来正确的文件。

    覆盖不到的情况也说明白：内容在 JSON 里被截断时，闭合标签/括号/引号
    往往仍然配对齐全（模型会在中途收尾），这种「完整但不完整」检不出来，
    只能靠上面的长度上限拦住。
    """
    name = rel.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    tail = content.rstrip()
    if not tail:
        return "⚠️ 内容是空的。"

    if ext in ("html", "htm"):
        if not tail.lower().endswith("</html>"):
            return "⚠️ HTML 没有 </html> 结束标签，文件很可能被截断了。"
        if tail.lower().count("<script") != tail.lower().count("</script>"):
            return "⚠️ <script> 与 </script> 数量不匹配，文件很可能被截断了。"
    elif ext == "json":
        try:
            json.loads(content)
        except Exception as e:  # noqa: BLE001
            return f"⚠️ 内容不是合法 JSON：{e}"
    elif ext == "py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            return f"⚠️ 内容不是合法 Python：{e}"
    elif ext in ("js", "jsx", "ts", "tsx", "css", "less", "scss"):
        for open_c, close_c in (("{", "}"), ("(", ")"), ("[", "]")):
            if content.count(open_c) != content.count(close_c):
                return (
                    f"⚠️ {open_c}{close_c} 数量不配对（{content.count(open_c)} vs "
                    f"{content.count(close_c)}），文件很可能被截断了。"
                )
    elif ext in ("xml", "svg", "html.j2"):
        if not tail.endswith(">"):
            return "⚠️ XML/SVG 没有以 > 结束，文件很可能被截断了。"
    return ""


class NoWorkspace(Exception):
    """当前没有可用工作区（用户还没在「我的空间」里选目录）。"""


def _effective(fallback_root: Optional[Path], fallback_ro: bool):
    """取当前请求的 (root, read_only, tmp_root)；无工作空间上下文则回落兜底值。

    ``tmp_root`` 是本 (会话, agent) 的私有目录，模型用 ``@tmp/`` 前缀指代它
    （见 :func:`sandbox.resolve_path`）。它是工作区**之外**的一个额外合法根，
    用来放测试脚本之类的临时文件——不放出去的话，模型写临时文件时只能往用户
    工作区根下堆。

    ``fallback_root`` 为 None = 装配期就没有工作区（用户没配「我的空间」）。
    这种情况下**明确报错**，而不是悄悄落到某个默认目录——落到默认目录的后果是
    模型在一个用户根本不知道的地方读写文件，而界面显示的却是「我的空间」。
    对话过程中通常不会走到这里：会话工作区是运行期注入的（见
    ``chat/service.py`` 的 ``resolve_workspace``），跟「我的空间」配没配无关。
    """
    from ...chat.context import current_workspace

    ws = current_workspace()
    if ws is None:
        if fallback_root is None:
            raise NoWorkspace(
                "当前没有工作区：请先到「我的空间」里选一个目录，"
                "或换个不依赖文件操作的用法。"
            )
        return Path(fallback_root), bool(fallback_ro), None
    return ws.root, ws.read_only, ws.agent_space


def make_deliver(root: Optional[Path], read_only: bool) -> ProcessorTool:
    """把工作空间内**已存在**的文件（或结构化内容），作为本次对话的交付清单推给用户
    （不复制 / 不重写）。

    它不写内容（文件用 ``bash`` 生成），只是「声明本次交付了哪些块」。捕获逻辑
    （``service._capture_artifacts``）会扫 ``react_steps`` 里的 ``fs.deliver`` 调用，
    把每个块挂成 artifact，前端按块的 ``kind`` 选渲染器（图片预览、HTML 沙箱、代码高亮、
    图表、表格、流程图…）。

    这是文件 / 多模态交付的**唯一正式入口**：agent 用 ``bash`` 把内容写进工作空间后，
    末尾用一次 ``fs.deliver([...])`` 列出要交付的**块**即可，不必为单个文件反复调用。
    """
    async def run(args: dict) -> str:
        eff_root, _, tmp_root = _effective(root, read_only)
        blocks = parse_blocks(args)
        if not blocks:
            return (
                "[缺少有效交付] 请传入块列表 blocks，如 "
                "[{\"kind\":\"file\",\"path\":\"report.html\"}]；"
                "兼容旧写法 paths=[\"report.html\"]"
            )
        ok, skip = [], []
        for b in blocks:
            rel = block_file_ref(b)
            if rel is None:
                # 内联 mermaid 等无文件块：src 已齐，直接通过
                if b.get("src"):
                    ok.append(b)
                else:
                    skip.append(f"{b.get('kind')}（既无 file 也无 src）")
                continue
            rel = rel.strip().strip("/")
            if not rel or is_scratch(rel):
                skip.append(f"{rel}（临时目录，不作为交付）")
                continue
            try:
                p = resolve_path(eff_root, tmp_root, rel)
            except PathEscape as e:
                skip.append(f"{rel}（越界：{e}）")
                continue
            if not p.is_file():
                skip.append(f"{rel}（文件不存在）")
                continue
            # 体检：file 块校验内容完整性；chart 块校验 spec 可解析
            if b["kind"] == "file":
                try:
                    body = p.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    skip.append(f"{rel}（读取失败：{e}）")
                    continue
                hint = _integrity_hint(rel, body)
                if hint:
                    skip.append(f"{rel}（{hint}）")
                    continue
            elif b["kind"] == "chart":
                try:
                    body = p.read_text(encoding="utf-8")
                except OSError as e:
                    skip.append(f"{rel}（读取失败：{e}）")
                    continue
                err = validate_chart_spec(body, b.get("engine") or "vega-lite")
                if err:
                    skip.append(f"{rel}（{err}）")
                    continue
            ok.append(b)
        if not ok:
            detail = "；".join(skip) if skip else "清单为空"
            return f"[无有效交付] {detail}"
        tail = f"；跳过 {len(skip)} 个：{'，'.join(skip)}" if skip else ""
        summary = ", ".join(f"{b['kind']}:{block_file_ref(b) or 'inline'}" for b in ok)
        return f"已交付 {len(ok)} 个块：{summary}{tail}"

    return ProcessorTool(
        name="fs.deliver",
        description=(
            "**唯一正式交付入口**——把要交付给用户的「块」用一次 `fs.deliver(blocks=[...])` 列出，前端按 kind 渲染成可交互内容：\n"
            "- 文件 `{\"kind\":\"file\",\"path\":\"x\",\"render\":\"preview|run|inline|folder\"}`（render 不写按扩展名默认：图/视频/PDF→打开，HTML→沙箱，代码/文档→内联）；\n"
            "- 图表 `{\"kind\":\"chart\",\"file\":\"x.vg.json\",\"engine\":\"vega-lite\"}`；流程图 `{\"kind\":\"mermaid\",\"src\":\"...\"}` 或 `{\"kind\":\"mermaid\",\"file\":\"x.mmd\"}`；表格 `{\"kind\":\"table\",\"file\":\"d.csv\"}`；地图 `{\"kind\":\"map\",\"file\":\"m.geojson\"}`；音频 `{\"kind\":\"audio\",\"file\":\"a.mp3\"}`。兼容旧写法 `paths=[\"x\"]`。\n"
            "- 先 `bash` 把内容写好（cat > / npm run build 等）；重内容（spec、数据）写进文件，块只写「怎么渲染」几行 JSON，别把巨型内容塞进参数。\n"
            "- **交付给用户看**才用本工具；自己分析文件用 `bash`（cat / less）即可。\n"
            "- bash 生成的文件不会自动上卡片：写完必须显式交付，否则用户只听到「文件已生成」却点不开。\n"
            "- 只交最终成品；草稿 / 临时 / 中间产物（测试脚本、分片、构建缓存）不要交——会被跳过。要交付 bash 写出的成品，`path` 用 `@session/` 前缀（如 `@session/report.md`）。\n"
        ),
        parameters={
            "blocks": "要交付的块列表；每个块是带 kind 的字典（见上方写法），或兼容地传 paths/路径字符串列表",
        },
        run=run,
    )
