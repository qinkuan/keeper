"""关键词搜索的「打分器」：决定哪一块内容更相关、排在前面。

先用一句话说清分工：

    FTS5（数据库自带的那个）负责回答「**哪些块里有这些词**」
    本文件负责回答「**这些块里哪个更相关**」

为什么不用 FTS5 自带的打分
--------------------------
FTS5 自带一个 bm25 打分，但它数「这个词出现在多少**行**」——而这里的"行"
是一段一段的文本块，不是"一个文件"。于是有两个坑：

坑一：文件名这种"整个文件都有的信息"会被数成很多次。
    比如一份有 170 段的大 PDF，它的文件名会写进它的**每一段**里 →
    FTS5 一看「这个词出现了 170 次（总共 241 段）」→ 认为它太常见了 →
    给它的"罕见度"分数算出来是**负数** → SQLite 把负数当成 0 →
    等于这个信号（文件名）**完全消失了**。

坑二：同一个词，不管出现在哪一列，罕见度只算**一次**。
    所以文件名里出现过的词，它在正文里那些本来很值钱的命中，
    也会被文件名撑大的"出现次数"一起拖成 0。

我们的做法
----------
1. **罕见度按"文件"算**：这个词出现在几个**文件**里（这才是它的真实价值）。
   并且用 log(1 + ...) 的写法，保证分数**永远是正数**，不会出现"算成负数被当成 0"。
2. **出现次数按"位置"分开算**：正文 / 小节标题 / 文件名 / 文档标题，各算各的，
   各自有自己的权重（正文 1、小节标题 3、文件名 8、文档标题 3）。
3. **文件名、文档标题这种"整个文件共有的信息"，一个文件只算一次**，
   不会因为这个文件有 170 段就算成 170 次。
4. **加分给这个文件里最相关的那一段**——所以"搜文件名"命中的是那个文件，
   而不是那个文件的 170 段一起刷屏。

（这套做法在教科书里叫 BM25F；这里只是把每个数字在干什么讲清楚。）

顺便还算了另一个数：证据充分度（evidence，0~1）
----------------------------------------------------
BM25F 那个原始分**不能直接当"有用程度"用**：它的绝对值会随语料大小漂移
（7 篇语料里"北京大学"能拿 13.4 分，1000 篇语料里同一个词能拿 60 分），
跟一个固定的门槛（0.5 / 0.8）根本没法比。

所以这里另算一个**跟语料大小无关**的数：

    证据充分度 = 这个词有多稀有 × 命中在多显眼的位置

"有多稀有"用的是相对稀有度（0~1，见 _idf_norm）；"多显眼的位置"是
文件名 > 文档标题 > 章节路径 > 正文（见 config.KEYWORD_EVIDENCE_WEIGHTS）。
最后把各个词的证据用"或"的方式合并（命中两个罕见词 > 命中一个）。

这个数就是给"分数下限"用的：见 ``search/query.py``。
"""
from __future__ import annotations

import math
from typing import Dict, List

from . import config
from . import tokenize as tokmod


def _idf(ndocs: int, df: int) -> float:
    """算一个词的「罕见度」分数：这个词越少见，分数越高（越值钱）。

    ndocs —— 一共有多少个文件
    df    —— 这个词出现在几个**文件**里

    比如「布隆过滤器」只出现在 1 个文件里 → 分数高，一搜就该排前面；
    「缓存」出现在 30 个文件里 → 分数低，说明不了什么。

    用 log(1 + ...) 而不是 log(...)：保证结果**永远是正数**，
    不会出现"算成负数、被当成 0、信号直接消失"的情况。
    """
    if df <= 0:
        df = 1
    if ndocs <= df:
        ndocs = df + 1
    return math.log(1.0 + (ndocs - df + 0.5) / (df + 0.5))


def _count_terms(field: str, terms: List[str]) -> Dict[str, int]:
    """数一数：这个位置里，各个查询词分别出现了几次。

    field —— 用空格隔开的一串词。正文、小节标题、文件名在数据库里
             都存成这个形式（jieba 切好词、用空格连起来）。
    terms —— 要数的那些词（就是从用户输入里切出来的）。

    只返回真正出现过的词（没出现的不占位置）。
    """
    if not field:
        return {}
    parts = field.split()
    out = {}
    for t in terms:
        n = parts.count(t)
        if n:
            out[t] = n
    return out


def _norm(length: int, avg: float, b: float) -> float:
    """长度惩罚：块越长，同样只命中一个词，分数就越低（要拿它去除）。

    length —— 这个块有多少个词
    avg    —— 所有块的平均词数
    b      —— 惩罚力度（0 = 不惩罚，1 = 完全按长度惩罚）

    为什么要惩罚：不然一段特别长的文字只要沾上了一个词，就能排到最前面，
    而真正短短一段就写在这件事上的内容反而被埋掉。
    """
    if avg <= 0:
        return 1.0
    return 1.0 - b + b * (length / avg)


def _idf_norm(idf: float, ndocs: int) -> float:
    """把「罕见度」换算成 0~1 的「这个词有多稀有」。

    idf   —— 这个词的罕见度（见 _idf）
    ndocs —— 一共有多少个文件

    为什么要多换算这一次：idf 是个对数，它的**绝对值会随语料大小漂移**——
    同一个词，7 篇语料里 idf ≈ 1.7，1000 篇语料里 idf ≈ 7.6。
    直接拿 idf 跟一个固定分数线比，语料一变门槛就失效了。

    换算办法：除以「这个语料里最罕见能达到的上限」log(1 + 文件数)，
    得到的就是**相对稀有度**（0~1），跟语料大小基本无关：

        7 篇语料里，"的" 出现在 7 篇   → 0.09（到处都是，说明不了什么）
        7 篇语料里，"北京大学" 出现在 1 篇 → 0.81（很难得碰上，说服力强）
    """
    scale = math.log(1.0 + max(int(ndocs), 2))
    if scale <= 0:
        return 0.0
    v = idf / scale
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _tf_sat(tf: int, norm: float, k1: float) -> float:
    """词频饱和：同一个词出现得越多，**多出来那次的收益越小**（BM25 标准做法）。

    tf   —— 这个词在这个块里出现了几次
    norm —— 这个块的长度惩罚系数（见 _norm）
    k1   —— 饱和速度：越大则"多出现几次"越值钱

    为什么要做这件事：不做的话，一个到处都是的常用词（"的"，一篇文档里出现
    24 次）累加出来的分，能和一个罕见词出现 1 次一样高——那么"这个命中有多
    说明问题"就判断不出来了，**分数也就没法当"有用程度"来卡阈值**。

    做了之后：出现 1 次 ≈ 0.92，出现 24 次 ≈ 2.10——
    多出现 23 次只多拿 1.3 倍，而不是 24 倍。
    """
    return tf * (k1 + 1.0) / (tf + k1 * max(norm, 1e-6))


def keyword_search(conn, query: str, top_k: int = 50, filters=None) -> List[dict]:
    """关键词搜索：先找出「哪些块里有这些词」，再给每个块打分、排序。

    返回按分数从高到低排好的列表，每条都带：
      - ``score``    —— BM25F 原始分（**只用来排序**，绝对值会随语料大小漂移）
      - ``evidence`` —— 证据充分度 0~1（跟语料大小无关，**用来卡分数下限**）
    """
    from .store import db as dbmod

    # 1) 把用户输入切成词。
    #    query_terms 给出"要算分的词"（打分时要逐词数出现次数），
    #    match_expression 给出"数据库怎么找"（FTS5 用的 OR 表达式）。
    terms = tokmod.query_terms(query)
    expr = tokmod.match_expression(query)
    if not expr or not terms:
        return []

    # 2) 召回：让 FTS5 找出含有这些词的块，最多取 KEYWORD_CANDIDATE_LIMIT 个。
    #    这里的结果**只是候选**，还没排过序（最终名次由我们自己算）。
    cands = dbmod.keyword_candidates(
        conn, expr,
        limit=config.KEYWORD_CANDIDATE_LIMIT,
        filters=filters,
    )
    if not cands:
        return []

    # 3) 准备打分要用的"公共信息"：
    #    - 每个词的罕见度（按文件数算）
    #    - 每个词的相对稀有度（0~1，用来算"证据充分度"）
    #    - 正文和标题的平均长度（用来做长度惩罚）
    ndocs = dbmod.count_indexed_docs(conn)
    idf = {t: _idf(ndocs, dbmod.keyword_doc_df(conn, t)) for t in terms}
    rarity = {t: _idf_norm(idf[t], ndocs) for t in terms}
    avg_body, avg_path = dbmod.keyword_avg_lengths(conn)

    weights = config.KEYWORD_WEIGHTS
    ev_w = config.KEYWORD_EVIDENCE_WEIGHTS
    b = config.KEYWORD_B

    # 4) 先算"块级"的部分：正文里命中几次、小节标题里命中几次。
    #    这一部分每块各算各的。
    #    注意：命中次数要走"饱和"（_tf_sat），不然常用词靠数量就能刷高分。
    #    同时记一份"证据充分度"（_ev）：这个词有多稀有 × 命中在多显眼的位置。
    #    证据充分度**不套长度惩罚**——长度归一是排序的事：一段很长的文字里
    #    提到"北京大学"，这条证据依然很硬，不该因为段落长就被打折。
    k1 = config.KEYWORD_K1
    for c in cands:
        tf_body = _count_terms(c["_tokens"], terms)
        tf_path = _count_terms(c["_title_tokens"], terms)
        score = 0.0
        ev: Dict[str, float] = {}
        for t, n in tf_body.items():
            sat = _tf_sat(n, _norm(c["_body_len"], avg_body, b), k1)
            score += idf[t] * weights["body"] * sat
            ev[t] = max(ev.get(t, 0.0),
                        rarity[t] * ev_w["body"] * min(1.0, _tf_sat(n, 1.0, k1)))
        for t, n in tf_path.items():
            sat = _tf_sat(n, _norm(c["_title_len"], avg_path, b), k1)
            score += idf[t] * weights["path"] * sat
            ev[t] = max(ev.get(t, 0.0),
                        rarity[t] * ev_w["path"] * min(1.0, _tf_sat(n, 1.0, k1)))
        c["_chunk_score"] = score
        c["_ev"] = ev
        # 记下这一块**实际命中了哪些查询词**——前端要拿它做关键词高亮。
        # 用户输入的是整串（「缓存穿透」），但正文里往往只出现其中一段
        # （「缓存」），前端拿整串去高亮会一个都标不出来。
        c["_matched"] = set(tf_body) | set(tf_path)

    # 5) 再算"文档级"的部分：文件名里命中几次、文档标题里命中几次。
    #    这两个是"整个文件共有的信息"，所以**同一个文件只算一次**，
    #    哪怕这个文件有 170 段，也不会被算成 170 次。
    doc_tf: Dict[int, Dict[str, Dict[str, int]]] = {}
    for c in cands:
        d = c["doc_id"]
        if d in doc_tf:
            continue
        doc_tf[d] = {
            "name": _count_terms(c["_name_tokens"], terms),
            "doc_title": _count_terms(c["_doc_title_tokens"], terms),
        }
    doc_score: Dict[int, float] = {}
    doc_ev: Dict[int, Dict[str, float]] = {}
    for d, fields in doc_tf.items():
        score = 0.0
        ev: Dict[str, float] = {}
        for t, n in fields["name"].items():
            sat = _tf_sat(n, 1.0, k1)
            score += idf[t] * weights["name"] * sat
            ev[t] = max(ev.get(t, 0.0),
                        rarity[t] * ev_w["name"] * min(1.0, sat))
        for t, n in fields["doc_title"].items():
            sat = _tf_sat(n, 1.0, k1)
            score += idf[t] * weights["doc_title"] * sat
            ev[t] = max(ev.get(t, 0.0),
                        rarity[t] * ev_w["doc_title"] * min(1.0, sat))
        doc_score[d] = score
        doc_ev[d] = ev

    # 6) 找出每个文件里"块级分最高"的那一块——文档级的加分就加在它身上。
    #    分数一样时取块序号小的（也就是文档靠前的那块）。
    best: Dict[int, dict] = {}
    for c in cands:
        d = c["doc_id"]
        cur = best.get(d)
        if cur is None or (c["_chunk_score"], -c["chunk_index"]) > (
                cur["_chunk_score"], -cur["chunk_index"]):
            best[d] = c

    # 7) 把两部分加在一起 = 最终分数。
    #    只被"文件名命中"、但又不是这个文件最优块的那些块，分数为 0 → 丢掉
    #    （它们没有真正的内容分，只是被文件名顺带捞出来的）。
    out = []
    for c in cands:
        d = c["doc_id"]
        score = c["_chunk_score"]
        ev = dict(c["_ev"])
        matched = set(c["_matched"])
        if best.get(d) is c:
            score += doc_score.get(d, 0.0)
            for t, v in doc_ev.get(d, {}).items():
                if v > ev.get(t, 0.0):
                    ev[t] = v
            # 文档级命中（文件名 / 文档标题）也算命中
            matched |= set(doc_tf.get(d, {}).get("name", {}))
            matched |= set(doc_tf.get(d, {}).get("doc_title", {}))
        if score <= 0:
            continue
        res = {k: v for k, v in c.items() if not k.startswith("_")}
        res["score"] = score
        # 实际命中的查询词（前端高亮用）
        res["matched_terms"] = sorted(matched)
        # 证据充分度（0~1）：这次命中有多说明问题。
        #   每个词各算各的（取它命中最有力的那个位置），再用"或"的方式合并——
        #   命中两个罕见词，比只命中一个更有说服力。
        miss = 1.0
        for v in ev.values():
            miss *= (1.0 - min(1.0, max(0.0, v)))
        res["evidence"] = 1.0 - miss
        out.append(res)

    # 8) 按分数从高到低排序，只留前 top_k 条。
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:max(1, int(top_k))]
