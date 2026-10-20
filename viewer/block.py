"""多模态交付块（Block）的模型、校验与能力清单。

agent 用 ``fs.deliver([...])`` 交付的不是「文件路径」，而是「带类型的块」：
文件只是块的一种（``kind="file"``），图表 / 表格 / 地图 / 流程图是另外的 kind。
后端只认 kind + 文件引用（或内联内容），前端按 kind 选渲染器
（见 web/src/components/blocks.ts，本包不依赖前端）。

设计要点（呼应 F 方案「声明与内容分离」）：
- 重内容（图表 spec、数据）由 agent 用 bash 写进文件，块只描述「怎么渲染这个文件」，
  几行 JSON 即可，不把巨型 spec 塞进工具参数（避免超输出上限、且易错）。
- 新增一种模态 = 在这里加一项 + 前端加一个 viewer，提示词由 ``capability_bullets`` 自动生成。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# 所有支持的块类型。新增模态时改这里 + 前端 viewer。
BLOCK_KINDS = ("file", "chart", "mermaid", "table", "map", "audio")

# 这些 kind 必须引用工作空间里的一个文件。
_FILE_REQUIRED_KINDS = ("chart", "table", "map", "audio")

RenderHint = str  # "inline" | "preview" | "run" | "download" | "folder"


def normalize_block(raw: Any) -> Optional[Dict[str, Any]]:
    """把一个 deliver 元素规整成标准块字典；无法识别返回 None。"""
    if isinstance(raw, str):
        return {"kind": "file", "path": raw.strip()}
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    # 语义化引用名：agent 在 fs.deliver 里给块起 ref，之后在 FINAL 文字里用
    # [[art:ref]] 把这块产物插到指定位置（详见 chat/service._capture_artifacts）。
    ref = raw.get("ref")
    ref = ref.strip() if isinstance(ref, str) and ref.strip() else None
    if kind == "file":
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        block: Dict[str, Any] = {"kind": "file", "path": path.strip()}
        rh = raw.get("render")
        if rh in ("inline", "preview", "run", "download", "folder"):
            block["render"] = rh
        if ref:
            block["ref"] = ref
        return block
    if kind == "mermaid":
        if not raw.get("file") and not raw.get("src"):
            return None
        block = {"kind": "mermaid"}
        if raw.get("file"):
            block["file"] = raw["file"].strip()
        if raw.get("src"):
            block["src"] = raw["src"]
        if ref:
            block["ref"] = ref
        return block
    if kind in _FILE_REQUIRED_KINDS:
        f = raw.get("file")
        if not isinstance(f, str) or not f.strip():
            return None
        block = {"kind": kind, "file": f.strip()}
        if kind == "chart" and raw.get("engine") in ("vega-lite", "echarts"):
            block["engine"] = raw["engine"]
        if ref:
            block["ref"] = ref
        return block
    return None


def parse_blocks(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 fs.deliver 参数里解析出块列表，兼容旧的 paths / path 写法。"""
    raw = args.get("blocks")
    if raw is None:
        raw = args.get("paths")
    if raw is None:
        raw = args.get("path")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        b = normalize_block(item)
        if b is not None:
            out.append(b)
    return out


def block_file_ref(block: Dict[str, Any]) -> Optional[str]:
    """块指向的工作空间相对路径（内联 mermaid 等无文件则返回 None）。"""
    return block.get("path") or block.get("file")


def default_render_hint(name: str) -> RenderHint:
    """没有显式 render 时，按文件名给默认展示方式，贴合策略：

    浏览器能直接开的（图片/视频/PDF）→ preview；HTML → run（沙箱运行）；
    代码/文档 → inline（编辑器/内联）；其余打不开的 → folder（只给文件夹按钮）。
    """
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in (
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "svg",
        "mp4", "webm", "ogg", "ogv", "mov", "m4v", "avi", "mkv", "pdf",
    ):
        return "preview"
    if ext in ("html", "htm"):
        return "run"
    if ext in (
        "py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "h",
        "md", "markdown", "mdx", "json", "yaml", "yml", "toml", "txt", "csv",
        "log", "sh", "bash", "css", "scss", "sql",
    ):
        return "inline"
    return "folder"


def validate_chart_spec(content: str, engine: str = "vega-lite") -> Optional[str]:
    """交付前校验图表 spec，把「写成功却白图」变成 agent 拿得到的错误。

    - JSON 必须能解析；
    - 按 engine 做结构校验（vega-lite 需有 mark/layer/…；echarts 需有 series）；
    - vega-lite 若运行环境装了 ``vl_convert`` 会**真正编译**一次，嵌套错一层 /
      字段名拼错 / 类型不对都能抓到；没装则退化到结构校验（不阻塞交付）。

    返回 ``None`` 表示通过，返回字符串表示失败原因（交给 fs.deliver 让 agent 重试）。
    """
    try:
        spec = json.loads(content)
    except Exception as e:  # noqa: BLE001
        return f"图表 spec 不是合法 JSON：{e}"
    engine = (engine or "vega-lite").lower()
    if engine == "echarts":
        return _validate_echarts(spec)
    err = _validate_vega_lite(spec)
    if err is None:
        err = _compile_vega_lite(spec)  # 结构没问题再尝试真编译（可选依赖）
    return err


def _validate_vega_lite(spec: Any) -> Optional[str]:
    """Vega-Lite 结构校验：只抓明显问题，不替代真编译。"""
    if not isinstance(spec, dict):
        return "Vega-Lite spec 必须是 JSON 对象"
    # 顶层至少要有一个能产出图形的入口（注意是 mark 不是 marks）
    has_root = any(
        k in spec
        for k in ("mark", "layer", "concat", "hconcat", "vconcat", "repeat", "facet", "spec")
    )
    if not has_root:
        return "Vega-Lite spec 缺少 mark / layer / concat / facet 等图形入口"
    if isinstance(spec.get("data"), dict):
        if not any(
            k in spec["data"]
            for k in ("values", "url", "name", "source", "sequence")
        ):
            return "data 字段缺少 values / url / name / source / sequence 之一"
    if "mark" in spec:
        m = spec["mark"]
        if isinstance(m, dict) and not isinstance(m.get("type"), str):
            return "mark 为对象时必须带 type 字段"
    if "layer" in spec and (not isinstance(spec["layer"], list) or not spec["layer"]):
        return "layer 必须是非空数组"
    if "encoding" in spec:
        enc = spec["encoding"]
        if not isinstance(enc, dict):
            return "encoding 必须是对象"
        for ch, v in enc.items():
            if not isinstance(v, dict):
                return f"encoding.{ch} 必须是对象"
            if not any(k in v for k in ("field", "type", "datum", "value", "band")):
                return f"encoding.{ch} 缺少 field / type / value 之一"
    return None


def _validate_echarts(spec: Any) -> Optional[str]:
    """ECharts option 结构校验（前端的 vega-embed 当前只渲染 vega-lite，
    但交付侧先别用 mark 规则误杀 echarts 的 option）。"""
    if not isinstance(spec, dict):
        return "ECharts option 必须是 JSON 对象"
    has_series = isinstance(spec.get("series"), list) and len(spec["series"]) > 0
    has_axes = "xAxis" in spec and "yAxis" in spec
    if not (has_series or has_axes):
        return "ECharts option 需要 series 数组（或同时有 xAxis / yAxis）"
    if has_series:
        for i, s in enumerate(spec["series"]):
            if not isinstance(s, dict) or not isinstance(s.get("type"), str):
                return f"series[{i}] 缺少 type 字段"
    return None


def _compile_vega_lite(spec: Any) -> Optional[str]:
    """真正编译一次（可选依赖 vl_convert）。装了就抓深层错误，没装返回 None 交给上层。"""
    try:
        import vl_convert as vlc  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        vlc.vl2vg(spec)
        return None
    except Exception as e:  # noqa: BLE001
        return f"Vega-Lite 编译失败：{e}"


def capability_bullets() -> str:
    """生成「可用块类型」说明，注入到 system prompt（单一事实来源，避免与代码漂移）。"""
    return (
        "   - file：普通文件（图片/视频/代码/HTML…）。"
        "``{\"kind\":\"file\",\"path\":\"x.png\",\"render\":\"preview|run|inline|folder\"}``\n"
        "   - chart：图表。把 Vega-Lite / ECharts 的 spec 写进 ``*.vg.json`` / ``*.echarts.json``，"
        "再 ``{\"kind\":\"chart\",\"file\":\"x.vg.json\",\"engine\":\"vega-lite\"}``\n"
        "   - mermaid：流程图 / 架构图。短则内联 ``{\"kind\":\"mermaid\",\"src\":\"graph TD;A-->B\"}``，"
        "长则写进 ``*.mmd`` 再 ``{\"kind\":\"mermaid\",\"file\":\"x.mmd\"}``\n"
        "   - table：可排序表格。数据写进 ``*.csv`` / ``*.json`` 后 ``{\"kind\":\"table\",\"file\":\"d.csv\"}``\n"
        "   - map：地图。GeoJSON 写进 ``*.geojson`` 后 ``{\"kind\":\"map\",\"file\":\"m.geojson\"}``\n"
        "   - audio：音频文件。``{\"kind\":\"audio\",\"file\":\"a.mp3\"}``\n"
    )
