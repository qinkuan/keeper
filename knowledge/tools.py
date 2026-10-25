"""知识库工具：检索本地资料，并把内容交给 agent。

三个工具
--------
``kb.search(keyword)``
    检索本地资料（PDF + Markdown 一起搜），**按命中内容的类型分别给**：

    - 命中的是 **Markdown** → 给命中位置**附近的若干行**（纯文本）；
    - 命中的是 **PDF** → 给命中处那 **3 页的截图**（第 N-1、N、N+1 页）。

    两种形态会出现在同一次结果里（一次检索可能同时命中 PDF 和 Markdown）。

``kb.read_pdf(path, start_page, end_page)``
    按路径 + 页范围读 PDF，**返回这些页的截图**（不是文字）。一次最多 5 页。

``kb.read_markdown(path, start_line, lines)``
    按路径 + 行号读 Markdown 的**文本**。默认 200 行，上限 600 行。

为什么按类型分岔
----------------
PDF 与 Markdown 的内容形态根本不同：PDF 要给**图**（版面信息在图里，文字层
是排版的副产品），Markdown 给**文本**（它本来就是结构化文本，渲染成图只会
让模型更难读）。硬凑成一个形态只能两边都别扭，所以按类型各给各的。

「召回只定位，阅读才给内容」这条分工在两种载体上分别落地：
- PDF：文字层只用来回答「第几页」，**不进上下文**；给模型的是那一页的图。
- Markdown：给的是命中处附近的行，agent 想看更多再调 ``kb.read_markdown``。

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
#: 为什么固定：给 agent 一个 top_k 参数只会诱使它去调参，而它无法判断该给多少
#: ——候选越多不代表越准。真正要翻哪一条，让它自己调读取工具更准。
_CANDIDATES = 20

#: 一次检索最多把几条命中做「实体化」（给内容）。其余命中只给定位信息。
_MAX_HITS = 5

#: 一次检索最多给几组 PDF 页面图。
#:
#: 为什么限死：PDF 每页是一张真图（约 800~900 token）+ 一次上传往返。
#: 5 个命中全是 PDF 就是 15 张图 —— 上下文被图片吃光，模型读不完也读不准。
#: 所以图只给最靠前的几组，其余命中给定位信息（agent 想读再调 kb.read_pdf）。
_MAX_PDF_PICTURES = 2

#: Markdown 命中时给的行数（以命中块为中心，上下补足到这个总量）。
#:
#: 注意这是 **kb.search 里**的窗口，不是 ``kb.read_markdown`` 的（那边默认 200 行）。
#: 为什么这里给得少：命中块本身就是**完整段落**（Markdown 切片按段落/代码块/
#: 表格切，不会切半句话），块外的行只是章节的邻接内容。给 200 行常常是
#: 「一大段无关内容 + 命中那 3 行」，而且 5 条命中一叠加就是两万字符。
#: agent 想细读，自己调 kb.read_markdown 拿 200 行。
_MD_SEARCH_LINES = 60

#: kb.read_markdown 默认给的行数。
_MD_WINDOW_LINES = 200

#: 低置信度命中只给这么多个字符的线索（不给整段）。
_WEAK_SNIPPET_CHARS = 200

#: 一次 kb.search 的**文本总预算**（字符）。
#:
#: 为什么要有：不加预算的话，5 条 Markdown 命中 × 200 行 ≈ 2.4 万字符。
#: 而工具结果超过 ``observation_limit``（3500）就会被外部化（落盘 + block_id），
#: 模型只拿得到一段预览，还得再 read 一次才看得全——一次检索变成两三次往返。
#: 所以宁可在这里主动裁剪，也不要让它被外部化。
_MAX_TEXT_CHARS = 5000

#: 单条 Markdown 正文最多给多少字符（别让一条吃掉全部预算）。
_MAX_ONE_CHARS = 1500

#: 一次最多给几页（read_pdf 的硬上限）。
MAX_PAGES = 5

#: 一次最多给多少行 Markdown（read_markdown 的硬上限）。
MAX_MD_LINES = 600

#: 严格门槛（0~1）。低于它的结果不作为命中返回。
#:
#: 为什么不是 0.8：BM25 的分数经归一化后，**同一档的块会撞在同一个值**
#: （实测 5 个不同的块都是 0.3423）。门槛定在档位之间就会把一整类命中
#: （比如所有只提到某个泛词的 Markdown 块）整体滤掉。0.65 落在两个档位之间。
_MIN_SCORE_STRICT = 0.65
#: 宽松门槛：严格门槛一条都没有时的兜底。这时会在说明里标注「低置信度」。
#:
#: 0.3 而不是 0：实测 Markdown 块的典型分数是 0.3423，0.4 门槛会把它们
#: 整批滤掉——那正好是最容易被漏掉的一类命中（文档正文里顺带提了一句）。
_MIN_SCORE_LOOSE = 0.3

_SEARCH_DESC = (
    "（知识库）检索「我的空间」里的本地资料，**按内容类型分别给**：\n"
    "- 命中 **Markdown** → 给命中位置附近的若干行**文本**；\n"
    "- 命中 **PDF** → 给命中处那 3 页的**截图**（第 N-1、N、N+1 页，撞到文档"
    "首尾时自动往另一侧补足）。\n"
    "两种形态会出现在同一次结果里。每条结果都标了文件名、类型、位置，"
    "以及「下一步该调哪个读取工具」。\n"
    "结果不够时用 kb.read_pdf（按路径+页码）或 kb.read_markdown（按路径+行号）继续读。"
)
_READ_DESC = (
    "（知识库·PDF）按文件路径 + 页码范围读 PDF，**返回这些页的截图**（不是文字）。"
    "path 用「我的空间」里的相对路径（或绝对路径），页码从 1 开始，"
    f"一次最多 {MAX_PAGES} 页。返回截图 + 一行文字说明（文件名、页码范围）。\n"
    "不知道文件叫什么，先用 bash 工具 `ls -1`（或 `ls -R`）列一下目录，"
    "再把准确的文件名传进来——文件名里有中文和空格时容易抄错，"
    "抄错了本工具会列出最接近的几个候选。"
)
_READ_MD_DESC = (
    "（知识库·Markdown）按文件路径 + 起始行号读 Markdown 的**文本**。"
    "path 用「我的空间」里的相对路径（或绝对路径），start_line 从 1 开始，"
    f"默认给 {MAX_MD_LINES} 行以内的一段（一次最多 {MAX_MD_LINES} 行）。\n"
    "不知道文件叫什么，先用 bash 工具 `ls -1` 列目录；文件名里有中文和空格时"
    "容易抄错，抄错了本工具会列出最接近的几个候选。"
)


# ---------------------------------------------------------------- 路径与页码


def _space_root() -> Path:
    """「我的空间」根目录。"""
    from ..setting import get_user_space_path

    raw = (get_user_space_path() or "").strip()
    if not raw:
        raise ValueError("尚未配置「我的空间」")
    return Path(raw).expanduser().resolve()


def _iter_files(root: Path, suffixes: tuple = (".pdf",), limit: int = 500):
    """遍历「我的空间」下指定后缀的文件（跳过隐藏目录），最多 limit 个。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if suffixes and not name.lower().endswith(suffixes):
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


def _not_found(root: Path, raw: str, suffixes: tuple = (".pdf",)) -> str:
    """文件没找到时的报错：说清「大概是什么错」并给出可操作的下一步。

    为什么这么啰嗦：中文文件名里常带空格与【】，模型从 ``ls`` 的输出里手工
    复制路径时很容易漏空格、漏一个字。与其让它反复试，不如把「最像的几个」
    直接摆出来——一次就中。
    """
    import difflib

    names = [p.name for p in _iter_files(root, suffixes)]
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


def _fuzzy_find(root: Path, raw: str, suffixes: tuple = (".pdf",)) -> Optional[Path]:
    """路径没精确命中时，按文件名模糊找**唯一**匹配的文件。

    为什么要模糊：模型把 ``ls`` 的输出抄进参数时，漏空格、少一个字、
    多带一对引号都是常事。与其让它一次次试错，不如在这里直接吸收掉。

    命中多个 → 返回 None（由调用方列出候选让模型自己选，不替它猜）。
    """
    stem = Path(raw).name.strip().strip("'\"")
    if not stem:
        return None
    cands = list(_iter_files(root, suffixes))
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


def resolve_in_space(path: str, suffixes: tuple = (".pdf",)) -> Path:
    """把 path 解析成「我的空间」内的绝对路径；越界 / 不存在 / 是目录都报错。

    ``suffixes`` 只影响**模糊匹配**时找哪些类型的文件（精确路径不受限）。

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
    hit = _fuzzy_find(root, raw, suffixes)
    if hit is not None:
        return hit
    raise ValueError(_not_found(root, raw, suffixes))


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


# ---------------------------------------------------------------- Markdown 行


def _count_lines(path: Path) -> int:
    """文件的总行数；读不出来按 0 算。"""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _read_lines(path: Path, lo: int, hi: int) -> List[str]:
    """读文件第 lo..hi 行（1-based 闭区间）；读不出来返回空列表。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    lo = max(1, int(lo))
    hi = min(len(lines), int(hi))
    if hi < lo:
        return []
    return lines[lo - 1:hi]


def _expand_lines(start: int, end: int, total: int, target: int = _MD_WINDOW_LINES) -> tuple:
    """以命中块为中心，把窗口补足到 target 行。返回 ``(lo, hi)``。

    为什么要补足：命中块往往只有几十行，孤零零给它，模型看不到上下文
    （比如命中的是一句结论，上一句的前提不在块里）。上下补足到 200 行，
    模型才有完整的语义环境。

    边界：撞到文件首尾时，从另一侧补；补完仍不足 target 就是文件本身那么短。
    """
    start = max(1, int(start or 1))
    end = max(start, int(end or start))
    total = max(end, int(total or end))
    span = end - start + 1
    if span >= target:
        return start, min(end, start + target - 1)
    need = target - span
    up = need // 2
    lo = start - up
    hi = end + (need - up)
    if lo < 1:
        hi += 1 - lo
        lo = 1
    if hi > total:
        lo -= hi - total
        hi = total
    return max(1, lo), max(1, hi)


def _overlaps(windows: List[tuple], lo: int, hi: int, ratio: float = 0.6) -> bool:
    """这个行区间是否和已给过的某个区间高度重叠。

    为什么要去重：一份文档里常有多个块落在同一章节，展开成窗口后内容几乎
    一模一样。给 5 遍同一段文字，模型看到的有效信息只有 1 遍。
    """
    span = max(1, hi - lo + 1)
    for a, b in windows:
        ov = min(hi, b) - max(lo, a) + 1
        if ov > 0 and ov / span >= ratio:
            return True
    return False


def _display_path(path: Path) -> str:
    """给 agent 看用的路径：**优先相对「我的空间」**（短、不含机器路径）。

    相对路径也能直接喂给 kb.read_pdf / kb.read_markdown（它们都按空间解析）。
    文件不在空间里（索引里的历史路径）就退回绝对路径。
    """
    try:
        return str(path.relative_to(_space_root()))
    except Exception:  # noqa: BLE001 不在空间内 / 取不到空间
        return str(path)


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


def _balance_by_kind(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """截断到 ``_MAX_HITS`` 条，并保证结果里**每种类型都至少留一条**。

    为什么需要保底：召回是全局按分数排的，一条热门的 PDF 能把它的 5 个块全塞进
    top_k，把 Markdown 命中整个挤掉 —— 于是「资料里有段 Markdown 正好在讲这
    个词」这件事，模型根本看不到。混合检索的意义正在于**跨载体**，
    纯按分数截断等于把「跨载体」这层信息丢了。

    做法：若截断后某种类型一条不剩，就用它里分数最高的那条替换掉**当前最低
    分**的那条。替换而不是追加，是为了让条数保持稳定（上下文预算可控）。
    """
    if len(hits) <= _MAX_HITS:
        return hits
    out = list(hits[:_MAX_HITS])
    kinds = {(h.get("source_kind") or "").lower() for h in hits}
    missing = kinds - {(h.get("source_kind") or "").lower() for h in out}
    for kind in sorted(missing):
        cand = next(
            (h for h in hits[_MAX_HITS:] if (h.get("source_kind") or "").lower() == kind),
            None,
        )
        if cand is None:
            continue
        # 替换掉当前最低分的那条（out 是按分数降序的，末尾即最低）
        out[-1] = cand
    return out


async def _search(args: dict) -> ToolResult:
    """检索本地资料：命中 Markdown 给附近的行，命中 PDF 给那 3 页的图。"""
    keyword = str((args or {}).get("keyword") or "").strip()
    if not keyword:
        return ToolResult(text="[kb.search: 至少需要一个非空 keyword]")

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
        logger.warning("kb.search 检索失败: %s", e)
        return ToolResult(text=f"[kb.search: 检索失败：{e}]")
    finally:
        svc.close()

    if not hits:
        return ToolResult(
            text=(
                f"[kb.search: 没有召回到与「{keyword}」相关的内容。]\n"
                "可能的原因：① 资料还没有建索引（在「知识库」里触发一次索引）；"
                "② 这份 PDF 没有文字层（扫描件无法被检索）；③ 关键词太具体。\n"
                "知道文件与位置的话，可以直接用 kb.read_pdf 或 kb.read_markdown。"
            )
        )

    cfg = await _current_llm_config()
    shown = _balance_by_kind(hits)
    parts: List[str] = []
    blocks: List[Dict[str, Any]] = []
    pictures = 0
    budget = _MAX_TEXT_CHARS
    seen_windows: List[tuple] = []

    for i, hit in enumerate(shown, 1):
        kind = (hit.get("source_kind") or "").lower()
        path = Path(str(hit.get("path") or ""))
        rel = _display_path(path)
        tp = _title_path(hit)
        score = hit.get("score")
        strong = score is None or float(score or 0) >= _MIN_SCORE_STRICT
        weak = "" if strong else "（低置信度匹配，内容可能与关键词无关）"

        if kind == "markdown":
            if not path.exists():
                parts.append(f"[{i}] Markdown｜{rel}｜文件已不存在，请重新索引")
                continue
            total = _count_lines(path)
            start_line = int(hit.get("start_line") or 1)
            end_line = int(hit.get("end_line") or start_line)
            # 有把握 → 命中块 + 少量上下文；没把握 → 只给命中块本身。
            #
            # 为什么不用「分数高就多给」：向量分**分不出相关性**——实测
            # 「缓存穿透」对「上下文治理」的向量分是 0.833（看着像强命中，
            # 其实完全无关），而真正相关的「上下文压缩」关键词分只有 0.213。
            # 所以「给多少」的判断权交给 agent（它看得懂标题路径），我只负责
            # 标出「这条大概不相关」，让它别浪费一次 read。
            target = _MD_SEARCH_LINES if strong else max(1, end_line - start_line + 1)
            lo, hi = _expand_lines(start_line, end_line, total, target)
            if _overlaps(seen_windows, lo, hi):
                # 窗口和前面某条几乎一样 → 正文重复，不再给一遍
                parts.append(
                    f"[{i}] Markdown｜{rel}｜第 {lo}-{hi} 行"
                    + (f"｜{tp}" if tp else "")
                    + f"{weak}（与上面某条内容重叠，正文见上）"
                )
                continue
            seen_windows.append((lo, hi))
            body = "\n".join(_read_lines(path, lo, hi))
            if len(body) > _MAX_ONE_CHARS:
                body = body[:_MAX_ONE_CHARS] + "\n…（本条已截断）"
            if len(body) > budget:
                body = body[:budget] + "\n…（本次检索的文本预算已用完）"
            budget -= len(body)
            parts.append(
                f"[{i}] Markdown｜{rel}｜第 {lo}-{hi} 行"
                + (f"｜{tp}" if tp else "")
                + f"{weak}\n"
                + body
                + f"\n（想读更多：kb.read_markdown(path=\"{rel}\", start_line={lo}））"
            )
            continue

        # ---- PDF ----
        if not path.exists():
            parts.append(f"[{i}] PDF｜{rel}｜文件已不存在，请重新索引")
            continue
        npages = _page_total(path)
        p = _hit_page(hit)
        pages = expand_pages(p, npages, span=1)
        head = f"[{i}] PDF｜{rel}｜命中第 {p} 页" + (f"｜{tp}" if tp else "")
        if strong and pictures < _MAX_PDF_PICTURES and cfg is not None:
            try:
                got = await _upload_pages(cfg, path, pages)
            except Exception as e:  # noqa: BLE001
                logger.warning("kb.search 上传页面失败: %s", e)
                got = []
            if got:
                blocks.extend(got)
                pictures += 1
                parts.append(
                    f"{head}｜已附上第 {pages[0]}-{pages[-1]} 页的截图\n"
                    f"（想读别的页：kb.read_pdf(path=\"{rel}\", start_page={p}））"
                )
                continue
        if not strong:
            parts.append(
                f"{head}{weak}\n"
                f"（想看这一页：kb.read_pdf(path=\"{rel}\", start_page={p}））"
            )
        elif pictures >= _MAX_PDF_PICTURES:
            parts.append(
                f"{head}\n"
                f"（本次已经附了 {_MAX_PDF_PICTURES} 组截图，剩下的就不附了——"
                f"想看这一页：kb.read_pdf(path=\"{rel}\", start_page={p}））"
            )
        else:
            parts.append(
                f"{head}\n"
                f"（这一页的截图没给出来，再试一次："
                f"kb.read_pdf(path=\"{rel}\", start_page={p}））"
            )

    head = f"检索「{keyword}」找到 {len(shown)} 条相关内容："
    return ToolResult(text=head + "\n\n" + "\n\n".join(parts), blocks=blocks)


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


async def _read_markdown(args: dict) -> ToolResult:
    """按路径 + 起始行号读 Markdown 的文本。"""
    raw_path = str((args or {}).get("path") or "").strip()
    if not raw_path:
        return ToolResult(text="[kb.read_markdown: 需要一个 path]")
    try:
        start = int((args or {}).get("start_line") or 0)
    except (TypeError, ValueError):
        start = 0
    if start < 1:
        return ToolResult(text="[kb.read_markdown: start_line 必须是从 1 开始的行号]")
    try:
        want = int((args or {}).get("lines") or _MD_WINDOW_LINES)
    except (TypeError, ValueError):
        want = _MD_WINDOW_LINES
    want = max(1, min(MAX_MD_LINES, want))

    try:
        path = resolve_in_space(raw_path, suffixes=(".md", ".markdown", ".txt"))
    except ValueError as e:
        return ToolResult(text=f"[kb.read_markdown: {e}]")

    total = _count_lines(path)
    if total == 0:
        return ToolResult(text=f"[kb.read_markdown: {path.name} 读不出内容（空文件？）]")
    if start > total:
        return ToolResult(
            text=f"[kb.read_markdown: 起始行 {start} 超出文件范围（共 {total} 行）]"
        )
    lo = start
    hi = min(total, start + want - 1)
    seg = _read_lines(path, lo, hi)
    if not seg:
        return ToolResult(text=f"[kb.read_markdown: {path.name} 第 {lo}-{hi} 行读不出来]")
    head = (
        f"{_display_path(path)} 第 {lo}-{hi} 行（共 {len(seg)} 行，文件共 {total} 行）\n"
        "----\n"
    )
    return ToolResult(text=head + "\n".join(seg))


# ---------------------------------------------------------------- 注册


def build_knowledge_tools() -> ToolRegistry:
    """构造知识库工具注册表（kb.search / kb.read_pdf / kb.read_markdown）。"""
    reg = ToolRegistry()
    reg.register(
        ProcessorTool(
            name="kb.search",
            description=_SEARCH_DESC,
            parameters={
                "keyword": "检索词（字符串）。用文档里真实出现的词，别用泛词。",
            },
            read_only=True,   # 纯读取：可并发
            run=_search,
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
    reg.register(
        ProcessorTool(
            name="kb.read_markdown",
            description=_READ_MD_DESC,
            parameters={
                "path": "Markdown 路径（「我的空间」里的相对路径或绝对路径）",
                "start_line": "起始行号（整数，从 1 开始）",
                "lines": f"读多少行（整数，默认 {MAX_MD_LINES}；一次最多 {MAX_MD_LINES} 行）",
            },
            read_only=True,   # 纯读取：可并发
            run=_read_markdown,
        )
    )
    return reg
