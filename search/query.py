"""检索：向量召回 + 关键词召回 -> 融合成 0~1 的最终分 -> 按分数下限过滤。

mode（检索通道）:
  "vector"  —— 只用向量召回
  "keyword" —— 只用关键词召回
  "hybrid"  —— 两路各自召回后融合（默认）

fusion（融合方式，决定"最终分"怎么算）:
  "noisy_or"（默认）—— 证据充分度：final = 1 - (1 - 向量分) * (1 - 关键词分)
                       两路都命中 → 更接近 1；只有一路命中 → 就是那一路的强度
                       （不会被另一路的权重压扁，所以"只搜到文件名"也能拿高分）
  "weighted" —— alpha*向量分 + (1-alpha)*关键词分（旧的加权方式）

最终分是 0~1 的"证据充分度"，用来做两件事：
  1. 排序；
  2. 卡门槛（min_score）——文件目录搜索用 MIN_SCORE_UI（0.5），
     agent 用 MIN_SCORE_AGENT（0.8），因为 agent 会把它当答案用，宁缺毋滥。

两条通道各自先换算成 0~1 的"证据充分度"，再用 noisy-OR 合并：
  向量路：余弦落在 [SCORE_VEC_LO, SCORE_VEC_HI] 之间 → 线性映射到 0~1
  关键词路：这个词有多稀有 × 命中在多显眼的位置（见 rank.py 的 evidence）
"""
import logging

from . import config
from .store import db as dbmod

logger = logging.getLogger(__name__)


def _kw_unit(kw_list):
    """关键词路的分数 → 0~1 的「证据充分度」。

    用的是 rank.py 算好的 ``evidence``（这个词有多稀有 × 命中在多显眼的位置），
    **不用** BM25F 原始分——原始分的绝对值会随语料大小漂移，跟固定门槛没法比。

    举例（7 篇语料）：
        搜"北京大学"（只在 1 篇里出现）→ 命中在文件名 → 0.74
        搜"的"（7 篇里都有）          → 命中在正文   → 0.06
    后者就是"到处都是、说明不了什么"的典型，门槛会把它挡掉。
    """
    if not kw_list:
        return {}
    out = {}
    for r in kw_list:
        ev = r.get("evidence")
        if ev is None:
            ev = 0.0
        ev = float(ev)
        out[r["chunk_id"]] = 0.0 if ev < 0 else (1.0 if ev > 1 else ev)
    return out


def _vec_unit(vec_list):
    """向量路的余弦 → 0~1 的证据充分度（按标定区间线性映射）。

    余弦低到 LO（基本不相关）记 0 分，高到 HI（很相关）记满分。
    """
    lo, hi = config.SCORE_VEC_LO, config.SCORE_VEC_HI
    span = (hi - lo) if hi > lo else 1.0
    out = {}
    for r in vec_list:
        v = (r["score"] - lo) / span
        out[r["chunk_id"]] = 0.0 if v < 0 else (1.0 if v > 1 else v)
    return out


def _merge_meta(merged, kw_list):
    """把关键词侧的"实际命中词"补到融合结果上（前端拿它做关键词高亮）。

    关键词通道知道**切出来的哪些词真的命中了**——用户输入「缓存穿透」，
    正文里往往只出现「缓存」，前端拿整串去高亮会一个都标不出来。
    """
    kwmap = {r["chunk_id"]: r for r in kw_list}
    for res in merged:
        hit = kwmap.get(res.get("chunk_id"))
        res["matched_terms"] = (hit.get("matched_terms") or []) if hit else []
    return merged


def _fuse_noisy_or(vec_list, kw_list):
    """证据充分度融合：final = 1 - (1 - 向量分) * (1 - 关键词分)。

    为什么不是加权平均：加权平均会被 alpha 压住上限——只靠语义命中的一条
    最多只能拿 0.65 分，在 agent 的 0.8 门槛下会被全部丢掉。
    noisy-OR 没有这个天花板：一条证据够硬，分就够高；两路都命中则更高
    （"两路都指向它"本身就是更可信的信号）。
    """
    vmap = _vec_unit(vec_list)
    kmap = _kw_unit(kw_list)
    merged = {}
    for r in vec_list:
        merged.setdefault(r["chunk_id"], {"base": r})
    for r in kw_list:
        merged.setdefault(r["chunk_id"], {"base": r})
    out = []
    for cid, ent in merged.items():
        v = vmap.get(cid, 0.0)
        k = kmap.get(cid, 0.0)
        res = dict(ent["base"])
        res["score"] = 1.0 - (1.0 - v) * (1.0 - k)
        res["vec_score"] = v
        res["kw_score"] = k
        out.append(res)
    out.sort(key=lambda x: x["score"], reverse=True)
    return _merge_meta(out, kw_list)


def _fuse_weighted(vec_list, kw_list, alpha):
    """分值归一化后加权融合（两路都先换算成 0~1）。"""
    vmap = _vec_unit(vec_list)
    kmap = _kw_unit(kw_list)
    merged = {}
    for r in vec_list:
        merged.setdefault(r["chunk_id"], {"base": r})
    for r in kw_list:
        merged.setdefault(r["chunk_id"], {"base": r})
    out = []
    for cid, ent in merged.items():
        res = dict(ent["base"])
        v = vmap.get(cid, 0.0)
        k = kmap.get(cid, 0.0)
        res["score"] = alpha * v + (1.0 - alpha) * k
        res["vec_score"] = v
        res["kw_score"] = k
        out.append(res)
    out.sort(key=lambda x: x["score"], reverse=True)
    return _merge_meta(out, kw_list)


def search(conn, embedder, query, top_k=10, filters=None, mode=None, alpha=None,
           fusion=None, min_score=None):
    """执行检索。

    mode:  "hybrid"（默认）| "vector" | "keyword"
    fusion: "noisy_or"（默认）| "weighted"
    alpha: weighted 模式下的向量权重 0~1
    min_score: 最终分下限（0~1）。None = 用 config.MIN_SCORE_UI；
               传 0 表示不过滤。
    返回的每条结果附带 score（最终分，0~1，越大越相关）、
    vec_score（向量那一路的 0~1 分）与 kw_score（关键词那一路的 0~1 分）。
    """
    mode = (mode or config.SEARCH_MODE or "hybrid").lower()
    fusion = (fusion or config.FUSION_MODE or "noisy_or").lower()
    alpha = config.HYBRID_ALPHA if alpha is None else float(alpha)
    alpha = min(1.0, max(0.0, alpha))
    if min_score is None:
        min_score = config.MIN_SCORE_UI
    if fusion == "rrf":
        # 旧的"只吃名次"的融合方式，分数不是 0~1 的置信度、没法卡门槛 → 退回默认
        logger.info("fusion=rrf 已不再支持（分数无法用于阈值），改用 noisy_or")
        fusion = "noisy_or"

    # 纯关键词：不加载模型也能查
    if mode == "keyword":
        results = dbmod.search_keyword(conn, query, top_k=top_k, filters=filters)
        units = _kw_unit(results)
        for r in results:
            r["score"] = units.get(r["chunk_id"], 0.0)
            r["vec_score"] = None
            r["kw_score"] = r["score"]
        results = [r for r in results if r["score"] >= min_score]
        return results[:top_k]

    qvec = embedder.embed_query(query)

    if mode == "vector":
        results = dbmod.search_similar(conn, qvec, top_k=top_k, filters=filters)
        units = _vec_unit(results)
        for r in results:
            r["score"] = units.get(r["chunk_id"], 0.0)
            r["vec_score"] = r["score"]
            r["kw_score"] = None
        results = [r for r in results if r["score"] >= min_score]
        return results[:top_k]

    # 混合：两路各自召回 -> 融合 -> 卡门槛
    vec_list = dbmod.search_similar(conn, qvec, top_k=config.VECTOR_TOP_N,
                                    filters=filters)
    kw_list = dbmod.search_keyword(conn, query, top_k=config.KEYWORD_TOP_N,
                                   filters=filters)
    if fusion == "weighted":
        merged = _fuse_weighted(vec_list, kw_list, alpha)
    else:
        merged = _fuse_noisy_or(vec_list, kw_list)
    merged = [r for r in merged if r["score"] >= min_score]
    return merged[:top_k]
