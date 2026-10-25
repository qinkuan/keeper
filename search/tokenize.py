"""中文分词：jieba 预分词 + FTS5 可索引的 token 串。

为什么需要这一层：
  - SQLite FTS5 的 unicode61 分词器把「连续的中文」当成一个 token，
    直接索引原文等于没法用（查「任务」匹配不到「……挂在消息/任务上……」）。
  - trigram 分词器要求查询词 ≥3 字符，中文 2 字词（任务、接口）直接失效。
  - 所以索引时先用 jieba 把正文与标题路径切成词，再用空格连接成
    「预分词文本」入库；查询时用同样的规则切查询词，命中的是**词**而不是字。

术语：
  token_string(text)   -> "产物 挂 在 消息 与 任务 上"（入库用，空格分隔）
  match_expression(q)  -> '"产物" OR "任务"'（FTS5 MATCH 表达式，OR 召回）
  name_token_string(s) -> 文件名专用（整体规范化形式 + 分词形式）
  normalize_name(s)    -> 文件名规范化（全角转半角、去标点空白、转小写）
"""
import logging
import re

import jieba

# jieba 初始化会往 stderr 打日志，静音
jieba.setLogLevel(logging.ERROR)

_ASCII_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: 全角字符 → 半角（ASCII 可见区）+ 全角空格。
_FULLWIDTH_TABLE = {
    i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)
}
_FULLWIDTH_TABLE[0x3000] = 0x20

#: 文件名规范化时保留的字符：字母、数字、汉字。
#: **下划线也要去掉**——FTS5 的 unicode61 把非字母数字当分隔符，
#: 留着下划线会让「a_b」被切成两个 token，破坏「整体形式」的完整性。
_KEEP_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def tokenize(text):
    """切词：中文用 jieba，英文/数字按词；过滤纯标点。"""
    if not text:
        return []
    out = []
    for w in jieba.cut(str(text), cut_all=False):
        w = w.strip()
        if not w:
            continue
        if _ASCII_RE.match(w):
            out.append(w.lower())
        elif any(ch.isalnum() for ch in w):
            out.append(w)
    return out


def token_string(text):
    """生成入库用的预分词文本（空格分隔）。"""
    return " ".join(tokenize(text))


def normalize_name(text):
    """文件名规范化：全角转半角、去标点与空白、转小写。

    目的：让「文件名精确匹配」不受标点 / 大小写 / 空格影响。
    「00【新旧大纲变动对比】2026新大纲」→ 「00新旧大纲变动对比2026新大纲」
    """
    if not text:
        return ""
    s = str(text).translate(_FULLWIDTH_TABLE)
    s = _KEEP_RE.sub("", s)
    return s.lower()


def name_token_string(name):
    """文件名专用的入库文本（存进 FTS5 的 ``name_tokens`` 列）。

    = **整体规范化形式** + jieba 分词形式，两者互补：

    - 整体形式：FTS5 的 unicode61 把连续的汉字/字母/数字当成**一个** token，
      所以「北京大学deepseedd内部研讨系列二…」会成为一个完整 token →
      搜完整文件名能**字面精确命中**（这正是"文件名匹配"该有的行为；
      光靠 jieba 分词做不到——文件名里的标点会把它切碎）。
    - 分词形式：让「搜部分词」（如"内部研讨"）也能命中。
    """
    norm = normalize_name(name)
    words = token_string(name)
    return " ".join(p for p in (norm, words) if p)


def query_terms(query):
    """查询词列表（打分时逐词算 tf 用）。

    = jieba 分词形式 + **整体规范化形式**（与 ``name_token_string`` 同一套规则）。

    为什么要单独给一份「词列表」：``match_expression()`` 产出的是一个 OR 表达式，
    只够做**召回**（谁含这些词）；要自己算分（BM25F）就得逐词知道
    「这个词在这个字段里出现了几次」。
    """
    seen = set()
    terms = []
    for t in tokenize(query):
        if t not in seen:
            seen.add(t)
            terms.append(t)
    norm = normalize_name(query)
    if norm and norm not in seen:
        terms.append(norm)
    return terms


def match_expression(query):
    """把查询词转成 FTS5 MATCH 表达式（OR 召回 + 去重 + 引号转义）。

    除了 jieba 的分词形式，还带上**文件名的整体规范化形式**：
    用户搜完整文件名时，靠这一项才能字面命中 ``name_tokens`` 列里
    那个"整体 token"（分词形式永远匹配不上，因为标点把文件名切碎了）。

    返回空串表示查询里没有任何可检索的词（例如全是标点）。
    """
    seen = set()
    terms = []
    for t in tokenize(query):
        if t in seen:
            continue
        seen.add(t)
        terms.append('"' + t.replace('"', '""') + '"')
    norm = normalize_name(query)
    if norm and norm not in seen:
        terms.append('"' + norm + '"')
    return " OR ".join(terms)
