"""知识库读取工具：把本地 PDF 的页面**以图片的形式**交给 agent。

两个工具
--------
``kb.search_pdf(keyword, block=1)``
    关键词召回本地资料，**直接返回 3 页 PDF 的截图**（不是文字）。
    命中页 + 上下各 1 页；撞到文档边界时往另一侧补足。

``kb.read_pdf(path, start_page, end_page)``
    按路径 + 页范围读 PDF，**一次最多 5 页**，同样返回截图。

为什么给的是图而不是文字
------------------------
PDF 的文字层是「排版的副产品」：两栏排版会左右交替、表格被拉平成一堆数字和
竖线、图里的文字根本不存在。模型读这样的文本**读不懂表格**，也无法判断一个
数字属于哪一行、哪一列。而整页渲染出来的图保留全部版面信息。

所以分工是：**检索用文字（定位到哪一页），阅读用图（把那一页给模型看）**。
文字层只用来回答「第几页」，不进 agent 的上下文。

为什么要走厂商 Files API 而不是 base64 内联
-------------------------------------------
图片最终都要上到厂商那里（云端模型），两条路都能做到。选 Files API 是因为
它**是项目里已经跑通的通道**：``llm/files.py`` 有完整的上传器实现（multipart、
file_id、引用格式），而 base64 内联要新增块类型、改发送层、还要改上下文压缩
（它按字符串处理 content）。复用现成通道的改动面小得多。

代价：每页一次上传往返（几百毫秒），且图片存在厂商侧。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..pdfpage import render as pdfrender
from ..tools.base import ProcessorTool, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

#: 召回时的候选块数（内部固定，不让 agent 调）。
#:
#: 为什么固定：召回只是「定位」，最终只取其中一块。给 agent 一个 top_k 参数
#: 只会诱使它去调参，而它无法判断该给多少——候选越多不代表越准。
_CANDIDATES = 20

#: 严格门槛（0~1）。低于它的结果不作为「命中页」返回。
_MIN_SCORE_STRICT = 0.8
#: 宽松门槛：严格门槛一条都没有时的兜底。这时会在说明里标注「低置信度」。
_MIN_SCORE_LOOSE = 0.4

#: 一次最多给几页（read_pdf 的硬上限）。
MAX_PAGES = 5

_SEARCH_DESC = (
    "（知识库·PDF）按关键词检索本地资料，**直接返回 PDF 页面的截图**（不是文字）。"
    "输入检索词，返回命中最相关的那一块所在的 3 页（第 N-1、N、N+1 页；"
    "撞到文档首尾时自动往另一侧补足），并附一行文字说明（文件名、页码范围、"
    "命中的章节）。\n"
    "如果你需要的是别的页、或某次召回没命中，用 kb.read_pdf 按路径和页码直接读。"
)
_READ_DESC = (
    "（知识库·PDF）按文件路径 + 页码范围读 PDF，**返回这些页的截图**（不是文字）。"
    "path 用「我的空间」里的相对路径（或绝对路径），页码从 1 开始，"
    f"一次最多 {MAX_PAGES} 页。返回截图 + 一行文字说明（文件名、页码范围）。\n"
    "不知道文件叫什么，先用 bash 工具 `ls -1`（或 `ls -R`）列一下目录，"
    "再把准确的文件名传进来——文件名里有中文和空格时容易抄错，"
    "抄错了本工具会列出最接近的几个候选。"
)


# ---------------------------------------------------------------- 路径与页码


def _space_root() -> Path:
    """「我的空间」根目录。"""
    from ..setting import get_user_space_path

    raw = (get_user_space_path() or "").strip()
    if not raw:
        raise ValueError("尚未配置「我的空间」")
    return Path(raw).expanduser().resolve()


def _iter_pdfs(root: Path, limit: int = 500):
    """遍历「我的空间」下的 PDF（跳过隐藏目录），最多 limit 个。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith(".") or not name.lower().endswith(".pdf"):
                continue
            yield Path(dirpath) / name
            count += 1
            if count >= limit:
                return


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _not_found(root: Path, raw: str) -> str:
    """文件没找到时的报错：说清「大概是什么错」并给出可操作的下一步。

    为什么这么啰嗦：中文文件名里常带空格与【】，模型从 ``ls`` 的输出里手工
    复制路径时很容易漏空格、漏一个字。与其让它反复试，不如把「最像的几个」
    直接摆出来——一次就中。
    """
    import difflib

    names = [p.name for p in _iter_pdfs(root)]
    close = difflib.get_close_matches(raw, names, n=4, cutoff=0.4)
    hint = ""
    if close:
        hint = "\n名字最接近的：\n" + "\n".join(f"  - {c}" for c in close)
    return (
        f"文件不存在：{raw}\n"
        "「我的空间」里没有这个路径。可能是路径抄错了（中文文件名里常有空格）。\n"
        "先列一下目录再传准确路径，例如 bash: ls -1 "
        "（或 ls -R 看子目录）。" + hint
    )


def _norm_name(s: str) -> str:
    """文件名归一化：去掉所有空白（含全角空格）。

    为什么：中文文件名里空格常是「视觉分隔」（"2026新大纲 基金私募"），
    模型抄写时漏掉它是头号错误来源。归一化后再比一次，就把它吸收掉了。
    """
    return re.sub(r"[\s　]+", "", s or "")


def _fuzzy_find(root: Path, raw: str) -> Optional[Path]:
    """路径没精确命中时，按文件名模糊找**唯一**匹配的 PDF。

    为什么要模糊：模型把 ``ls`` 的输出抄进参数时，漏空格、少一个字、
    多带一对引号都是常事。与其让它一次次试错，不如在这里直接吸收掉。

    命中多个 → 返回 None（由调用方列出候选让模型自己选，不替它猜）。
    """
    stem = Path(raw).name.strip().strip("'\"")
    if not stem:
        return None
    cands = list(_iter_pdfs(root))
    if not cands:
        return None
    for p in cands:
        if p.name == stem or p.name.lower() == stem.lower():
            return p
    # 忽略空白后再比（漏空格）
    n_stem = _norm_name(stem)
    for p in cands:
        if _norm_name(p.name).lower() == n_stem.lower():
            return p
    sub = [p for p in cands if stem in p.name]
    if len(sub) == 1:
        return sub[0]
    if len(sub) > 1:
        names = "\n".join(f"  - {_rel(root, p)}" for p in sub[:8])
        raise ValueError(f"「{stem}」匹配到多个文件，请写得更精确：\n{names}")
    # 忽略空白的包含关系
    nsub = [p for p in cands if n_stem and n_stem in _norm_name(p.name)]
    if len(nsub) == 1:
        return nsub[0]
    sup = [p for p in cands if p.name in raw]
    if len(sup) == 1:
        return sup[0]
    return None


def resolve_in_space(path: str) -> Path:
    """把 path 解析成「我的空间」内的绝对路径；越界 / 不存在 / 是目录都报错。

    为什么要自己写一份而不复用 :func:`zonefilesystem.space_ops.resolve_target`：
    那个函数抛的是 ``fastapi.HTTPException``——它是给 Web 层用的。工具跑在
    ReAct 循环里，报错会被 ``ProcessorTool.execute`` 兜住并转成一句人话给
    模型看，抛 HTTP 异常会让日志里出现一堆无意义的栈。
    """
    root = _space_root()
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("path 不能为空")
    # 模型常把 shell 里的引号一起抄进来
    raw = raw.strip("'\"").strip()
    p = Path(raw).expanduser()
    target = (p if p.is_absolute() else root / p).resolve()
    if target != root and root not in target.parents:
        raise ValueError("只能读取「我的空间」内的文件")
    if target.exists():
        if target.is_dir():
            raise ValueError("这是一个目录，不是 PDF")
        return target
    # 精确路径没命中 → 模糊找一次（模型抄错路径是常态，不是异常）
    hit = _fuzzy_find(root, raw)
    if hit is not None:
        return hit
    raise ValueError(_not_found(root, raw))


def expand_pages(hit: int, total: int, span: int = 1) -> List[int]:
    """把命中页展开成「命中页 ±span」，撞到边界时往另一侧补足。

    ============== ==========================================
    情况           返回
    ============== ==========================================
    常规           ``hit-1, hit, hit+1``
    命中第 1 页    ``1, 2, 3``（往下补）
    命中最后一页   ``total-2, total-1, total``（往上补）
    文档不足 3 页  全部页
    ============== ==========================================

    **页数恒定**是刻意的：工具的契约是「返回 3 页」，agent 每次拿到的数量
    一致，不用自己判断「这次是不是少了几页」。
    """
    span = max(0, int(span))
    total = max(1, int(total))
    hit = max(1, min(int(hit), total))
    width = span * 2 + 1
    if total <= width:
        return list(range(1, total + 1))
    lo = hit - span
    hi = hit + span
    if lo < 1:
        lo, hi = 1, 1 + span * 2
    elif hi > total:
        lo, hi = total - span * 2, total
    return list(range(lo, hi + 1))


# ---------------------------------------------------------------- 模型配置


async def _current_llm_config():
    """取当前 agent 绑定的模型配置；取不到返回 None。

    为什么要模型配置：上传图片走厂商 Files API，需要 provider / model /
    base_url / api_key。走的是 :mod:`llm.files` 里那张**按 provider 注册**的
    能力表——只有实现了上传器的 provider 才能给图。
    """
    from ..chat.context import current_agent_id

    agent_id = current_agent_id()
    if not agent_id:
        return None
    try:
        from ..agent.keeper import _resolve_llm_by_binding
        from ..llm.config import LLMConfig

        raw = await _resolve_llm_by_binding(agent_id)
        if not raw:
            return None
        return LLMConfig(**raw)
    except Exception as e:  # noqa: BLE001 配置读不到不该让工具崩掉
        logger.warning("解析 agent %s 的模型配置失败: %s", agent_id, e)
        return None


# ---------------------------------------------------------------- 上传


async def _upload_pages(cfg, path: Path, pages: List[int]) -> List[Dict[str, Any]]:
    """渲染这些页 → 上传厂商 → 返回 content block 列表。

    渲染是 CPU 密集的（一页 50~200ms，复杂矢量页能到 1s+），放在线程池里跑，
    别把事件循环堵住——同一时刻可能有别的会话正在流式输出。

    上传并发做：3 页顺序上传是 3 个往返，并发是 1 个。
    """
    from ..llm.files import uploader_for

    uploader = uploader_for(getattr(cfg, "provider", "") or "")
    if uploader is None:
        raise ValueError(
            f"当前模型（{getattr(cfg, 'provider', '') or '未知'}）不支持文件上传，"
            "无法把 PDF 页面截图交给模型。"
        )
    ok, why = uploader.supports(cfg)
    if not ok:
        raise ValueError(why or "当前模型不支持文件上传")

    pngs = await asyncio.to_thread(pdfrender.render_pages, str(path), pages)
    if not pngs:
        raise ValueError(f"这些页渲染不出来（页码可能越界）：{pages}")

    async def _one(data: bytes, page: int) -> Dict[str, Any]:
        up = await uploader.upload(cfg, data, f"{path.stem}-p{page}.png")
        return uploader.ref_part(up.file_id)

    blocks = await asyncio.gather(
        *(_one(data, page) for data, page in zip(pngs, pages))
    )
    return list(blocks)


def _page_label(pages: List[int]) -> str:
    """页范围的文字描述：「第 12 页」/「第 11-13 页」。"""
    if not pages:
        return ""
    if len(pages) == 1:
        return f"第 {pages[0]} 页"
    return f"第 {pages[0]}-{pages[-1]} 页"


def _title_path(hit: Dict[str, Any]) -> str:
    """章节路径 → 「第 3 章 > 3.2 缓存层」这样的可读形式。"""
    parts = [p for p in (hit.get("title_path") or []) if p]
    if not parts:
        return ""
    return " > ".join(parts)


def _page_total(path: Path) -> int:
    """PDF 的总页数；读不到就按 1 页算（后续页码校验会拦住）。"""
    try:
        return pdfrender.page_count(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("读 PDF 页数失败 %s: %s", path, e)
        return 1


def _hit_page(hit: Dict[str, Any]) -> int:
    """命中内容所在的页码（1-based）。

    优先用 ``page_start``（精确页，索引里新加的列）；老库没有这列时退回
    ``page``（从 locator 解析出来的起始页）。
    """
    for key in ("page_start", "page"):
        v = hit.get(key)
        if v:
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                continue
    return 1


# ---------------------------------------------------------------- 工具实现

def _search_pdf_hits(svc, keyword: str, mode: str) -> List[Dict[str, Any]]:
    """先按严格门槛召回；一条都没有再用宽松门槛兜底。"""
    hits = svc.search(
        keyword, top_k=_CANDIDATES, mode=mode, min_score=_MIN_SCORE_STRICT
    )
    if hits:
        return hits
    return svc.search(
        keyword, top_k=_CANDIDATES, mode=mode, min_score=_MIN_SCORE_LOOSE
    )


async def _search_pdf(args: dict) -> ToolResult:
    """召回本地 PDF 资料，返回命中处的 3 页截图。"""
    keyword = str((args or {}).get("keyword") or "").strip()
    if not keyword:
        return ToolResult(text="[kb.search_pdf: 至少需要一个非空 keyword]")
    try:
        block = int((args or {}).get("block") or 1)
    except (TypeError, ValueError):
        block = 1
    if block < 1:
        block = 1

    from ..search import config as scfg
    from ..search.ingest import embed as embedmod
    from ..search.service import SearchService

    emb = embedmod.get_embedder()
    svc = SearchService(embedder=emb)
    try:
        # 没装 embedding 模型时只能走纯关键词通道（不因此失败——「搜字面词」
        # 不该被「装不装得动几百 MB 的模型」卡住）。
        mode = scfg.SEARCH_MODE if emb is not None else "keyword"
        hits = _search_pdf_hits(svc, keyword, mode)
    except Exception as e:  # noqa: BLE001 检索失败要说清楚，不该让 agent 瞎猜
        logger.warning("kb.search_pdf 检索失败: %s", e)
        return ToolResult(text=f"[kb.search_pdf: 检索失败：{e}]")
    finally:
        svc.close()

    # 只处理 PDF（Markdown 的读取是另一条路）
    pdf_hits = [h for h in hits if (h.get("source_kind") or "") == "pdf"]
    if not pdf_hits:
        return ToolResult(
            text=(
                f"[kb.search_pdf: 没有召回到与「{keyword}」相关的 PDF。]\n"
                "可能的原因：① 这份 PDF 没有文字层（扫描件无法被检索）；"
                "② 关键词太具体。\n"
                "如果知道文件与页码，直接用 kb.read_pdf 读那一段。"
            )
        )
    if block > len(pdf_hits):
        return ToolResult(
            text=(
                f"[kb.search_pdf: 第 {block} 块不存在，本次只召回到 "
                f"{len(pdf_hits)} 块 PDF 命中。]"
            )
        )

    hit = pdf_hits[block - 1]
    path = Path(str(hit.get("path") or ""))
    if not path.exists():
        return ToolResult(
            text=f"[kb.search_pdf: 命中的文件已不存在（{path.name}），请重新索引。]"
        )

    total = _page_total(path)
    hit_page = _hit_page(hit)
    pages = expand_pages(hit_page, total, span=1)

    cfg = await _current_llm_config()
    if cfg is None:
        return ToolResult(
            text="[kb.search_pdf: 当前 agent 没有绑定模型，无法上传页面截图。]"
        )
    try:
        blocks = await _upload_pages(cfg, path, pages)
    except Exception as e:  # noqa: BLE001
        logger.warning("kb.search_pdf 上传页面失败: %s", e)
        return ToolResult(text=f"[kb.search_pdf: 页面截图上传失败：{e}]")

    tp = _title_path(hit)
    note = (
        f"{path.name} {_page_label(pages)}"
        f"（命中第 {hit_page} 页"
        + (f" · {tp}" if tp else "")
        + f" · 第 {block}/{len(pdf_hits)} 块）"
    )
    score = hit.get("score")
    if score is not None and float(score or 0) < _MIN_SCORE_STRICT:
        note += "（低置信度匹配，页面内容可能与关键词无关）"
    return ToolResult(text=note, blocks=blocks)


async def _read_pdf(args: dict) -> ToolResult:
    """按路径 + 页范围读 PDF，返回截图。"""
    raw_path = str((args or {}).get("path") or "").strip()
    if not raw_path:
        return ToolResult(text="[kb.read_pdf: 需要一个 path]")
    try:
        start = int((args or {}).get("start_page") or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        end = int((args or {}).get("end_page") or start or 0)
    except (TypeError, ValueError):
        end = start
    if start < 1:
        return ToolResult(text="[kb.read_pdf: start_page 必须是从 1 开始的页码]")

    try:
        path = resolve_in_space(raw_path)
    except ValueError as e:
        return ToolResult(text=f"[kb.read_pdf: {e}]")

    if path.suffix.lower() != ".pdf":
        return ToolResult(text=f"[kb.read_pdf: {path.name} 不是 PDF 文件]")

    total = _page_total(path)
    if start > total:
        return ToolResult(
            text=f"[kb.read_pdf: 起始页 {start} 超出文档范围（共 {total} 页）]"
        )
    if end < start:
        end = start
    if end > total:
        end = total
    if end - start + 1 > MAX_PAGES:
        return ToolResult(
            text=(
                f"[kb.read_pdf: 一次最多读 {MAX_PAGES} 页，"
                f"你请求了 {end - start + 1} 页（第 {start}-{end} 页）。"
                "请缩小范围分次读取。"
            )
        )

    pages = list(range(start, end + 1))
    cfg = await _current_llm_config()
    if cfg is None:
        return ToolResult(
            text="[kb.read_pdf: 当前 agent 没有绑定模型，无法上传页面截图。]"
        )
    try:
        blocks = await _upload_pages(cfg, path, pages)
    except Exception as e:  # noqa: BLE001
        logger.warning("kb.read_pdf 上传页面失败: %s", e)
        return ToolResult(text=f"[kb.read_pdf: 页面截图上传失败：{e}]")

    note = f"{path.name} {_page_label(pages)}（共 {len(pages)} 页）"
    return ToolResult(text=note, blocks=blocks)


# ---------------------------------------------------------------- 注册


def build_knowledge_tools() -> ToolRegistry:
    """构造知识库工具注册表（kb.search_pdf / kb.read_pdf）。"""
    reg = ToolRegistry()
    reg.register(
        ProcessorTool(
            name="kb.search_pdf",
            description=_SEARCH_DESC,
            parameters={
                "keyword": "检索词（字符串）。用文档里真实出现的词，别用泛词。",
                "block": (
                    "取第几块命中（整数，默认 1）。第一次调用用默认即可；"
                    "想看第 2、3 块命中时再传 2、3。"
                ),
            },
            read_only=True,   # 纯读取：可并发
            run=_search_pdf,
        )
    )
    reg.register(
        ProcessorTool(
            name="kb.read_pdf",
            description=_READ_DESC,
            parameters={
                "path": "PDF 路径（「我的空间」里的相对路径或绝对路径）",
                "start_page": "起始页码（整数，从 1 开始）",
                "end_page": f"结束页码（整数，默认与起始页相同；一次最多 {MAX_PAGES} 页）",
            },
            read_only=True,   # 纯读取：可并发
            run=_read_pdf,
        )
    )
    return reg
