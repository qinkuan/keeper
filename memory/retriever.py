"""记忆检索封装：jieba 预分词 + SQLite FTS5。

设计依据见 ``keeper/doc/memory.md`` 第 5.3.2 节。核心结论：
- FTS5 自带分词器（``unicode61`` / ``trigram``）都不会做中文分词，分词必须
  在 Python 侧用 jieba 做完，再喂给 ``tokenize='unicode61'`` 的 FTS5 表。
- 三道防线：
    ① 用户词典（治本）：把专有名词注册成词，避免被拆成单字。
    ② 单字 bigram 兜底（防漏切）：相邻单字中文补成 2 字 token。
    ③ ≤2 字 fallback 全扫（最后保险）：极短查询直接 Python ``in`` 判断。

注意：FTS5 的 ``unicode61`` **不索引单字 CJK token**，因此 ``seg`` 在生成
索引/查询串时会剔除单字中文，只保留 ≥2 字 token 与 bigram——否则 AND 查询
因含未索引 token 会整体 0 命中（这就是早期实测「张三」检索不到的根因）。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

import jieba

jieba.setLogLevel(20)

_CJK = re.compile(r"[\u4e00-\u9fff]")

# 防线 ①：项目专有名词。可在此扩展，或运行时调用 ``add_user_word``。
_USER_WORDS: Tuple[str, ...] = ("OpenAI", "DeepSeek", "Qwen", "Claude", "API", "token")

# 高频但**无检索区分度**的词：进索引与查询都剔掉。
#
# 为什么不用 jieba 自带的停用词表（`jieba.analyse.stop_words`，上千词）：
# 它含「我们 / 你们 / 他们」这类词，而记忆摘要里这些词是**有意义的**
# （「用户询问我们…」）。全量引入属于过度过滤。
# 所以这里只列两类**确定不携带检索意图**的：
#   ① 问候客套：你好 / 请问 / 谢谢
#   ② 疑问与虚词：什么 / 怎么 / 帮我 / 一下
# 实测（36 轮真实会话）噪音全部来自「你好」——`你好，你链接了什么mcp`
# 召回 3 条里 2 条是「用户仅问候'你好吗'」。剔掉后这类噪音消失。
#
# 影响范围**仅限索引与查询**：召回返回给模型的文本走 turns.jsonl 里的**原文
# summary**（见 store.SessionMemory.recall），不是分词串——所以模型看到的内容
# 一个字节都不变，只是「能不能被检索到」变了。
STOPWORDS: Tuple[str, ...] = (
    # 问候客套
    "你好", "您好", "哈喽", "嗨", "请问", "请", "麻烦", "谢谢", "感谢",
    "多谢", "抱歉", "对不起", "打扰",
    # 泛疑问词：多是「怎么弄」里的语气成分，核心约束在后面的实词上。
    # 刻意**不收**「多少 / 哪里 / 哪个 / 什么时候」——它们常常就是查询的语义
    # 骨架（"多少钱""哪个端口""什么时候重启"），过滤掉会连查询意图一起丢。
    # 实测「请问服务器端口是多少」过滤掉「多少」后只剩「服务器端 口是」，更糟。
    "什么", "怎么", "怎样", "如何", "为什么",
    # 虚词与客套动作
    "帮我", "帮忙", "一下", "一下子", "吧", "啊", "哦", "嗯", "呢", "吗",
    "的话",
)
_STOP = frozenset(STOPWORDS)


def add_user_word(word: str) -> None:
    """运行时注册一个用户词典词（治本防线）。"""
    jieba.add_word(word)


for _w in _USER_WORDS:
    jieba.add_word(_w)


def seg(text: str) -> str:
    """分词为空格连接的 token 串（写入与查询共用）。

    规则：
    - 长度 ≥ 2 的 token 直接保留；
    - 单字中文与其相邻单字组成 bigram（防线 ②）；
    - 单字（中文 / 标点 / 单字母）一律剔除——它们不被 FTS5 索引，留在查询里
      会因 AND 语义导致整体 0 命中；
    - 停用词剔除（``STOPWORDS``）：问候客套与虚词没有检索区分度，留在索引里
      只会让 OR 召回被「你好」「请问」这类高频词占满。
    """
    tokens = [t.strip() for t in jieba.lcut(text) if t and t.strip()]
    out: List[str] = []
    for i, t in enumerate(tokens):
        if len(t) >= 2:
            out.append(t)
            continue
        if _CJK.fullmatch(t):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt and _CJK.fullmatch(nxt):
                out.append(t + nxt)
    # 停用词过滤放在 bigram 之后，这样「你+好→你好」这类合成词也能被剔掉
    kept = [t for t in out if t.lower() not in _STOP]
    # **全被过滤掉时回退到原 token**：宁可过滤没生效，也不要让一条记忆彻底
    # 消失。纯问候轮次（摘要只有「你好，请问」）召不到本来是对的，但那是
    # 「低价值」而不是「不存在」——用回退保证行为变化是渐进的。
    return " ".join(kept or out)


class MemoryIndex:
    """围绕单个 FTS5 数据库文件的检索封装。

    一个实例对应一个 ``index.db``（可以是某 session 的，或长期记忆的）。
    ``doc_id`` 以 UNINDEXED 列存储，可更新/过滤但不参与分词。
    """

    #: 索引内容格式版本。**改动 :func:`seg` 的产出格式时必须 +1**，否则老索引里
    #: 的分词串与新查询对不上——表现为「改了配置却没效果」且没有任何报错，
    #: 因为索引不会自动重建（见 :meth:`is_stale`）。
    SEG_VERSION = 2

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._con = sqlite3.connect(self.db_path)
        self._con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem "
            "USING fts5(doc_id UNINDEXED, content, tokenize='unicode61')"
        )
        # meta 表不参与检索，只记索引内容的格式版本
        self._con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self._con.commit()

    def _seg_version(self) -> Optional[str]:
        row = self._con.execute("SELECT v FROM meta WHERE k = 'seg_version'").fetchone()
        return row[0] if row else None

    def is_stale(self) -> bool:
        """索引是否由旧版 :func:`seg` 生成（需要重建）。"""
        return self._seg_version() != str(self.SEG_VERSION)

    def mark_version(self) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES ('seg_version', ?)",
            (str(self.SEG_VERSION),),
        )
        self._con.commit()

    def upsert(self, doc_id: str, content: str) -> None:
        """索引 / 更新一条记忆；同 doc_id 自动覆盖。"""
        self._con.execute("DELETE FROM mem WHERE doc_id = ?", (doc_id,))
        self._con.execute(
            "INSERT INTO mem(doc_id, content) VALUES (?, ?)",
            (doc_id, seg(content)),
        )
        self._con.commit()

    def remove(self, doc_id: str) -> None:
        self._con.execute("DELETE FROM mem WHERE doc_id = ?", (doc_id,))
        self._con.commit()

    def search(self, query: str, limit: int = 10, mode: str = "and") -> List[Tuple[str, str]]:
        """检索，返回 ``[(doc_id, 原文)]``。

        mode:
        - ``"and"``（默认）：查询词之间 AND——精确，适合 LLM 主动 recall 的聚焦查询；
        - ``"or"``：查询词之间 OR——宽松，适合「用整句问题做系统侧自动召回」
          （整句多 token，AND 几乎必 0 命中，OR 才能粗筛出候选）。

        先走 FTS5；当查询无有效分词（极短 / 纯单字）或 MATCH 报错时，
        降级为全文 ``in`` 扫描（防线 ③）。
        """
        tokens = seg(query).split()
        if mode == "or" and len(tokens) > 1:
            q = " OR ".join(tokens)
        else:
            q = " ".join(tokens)
        if q.strip():
            try:
                rows = self._con.execute(
                    "SELECT doc_id, content FROM mem WHERE mem MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (q, limit),
                ).fetchall()
                if rows:
                    return [(r[0], r[1]) for r in rows]
            except sqlite3.OperationalError:
                pass
        return self._fallback(query, limit)

    def _fallback(self, query: str, limit: int) -> List[Tuple[str, str]]:
        q = query.strip()
        if not q:
            return []
        return [
            (r[0], r[1])
            for r in self._con.execute("SELECT doc_id, content FROM mem").fetchall()
            if q in r[1]
        ][:limit]

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "MemoryIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
