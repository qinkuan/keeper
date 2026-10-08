"""上下文治理：工具输出**外部化** + 早期步骤**自动压缩**。

设计见 ``doc/context-design.md``，这里只写实现要点：

**外部化（L2）**
工具输出超阈值就写盘，上下文里只留「首尾预览 + ``ctx:<id>``」，模型需要时
``read(block_id="ctx:xxx")`` 展开。id 用**内容哈希**，所以重放（resume）同一份
输出会得到同一个 id，不会重复落盘。

**压缩（L1）**
输入 token 到水位时，把最早的一批步骤压成「骨架（有序 step + 一行结论）+
block_id」，原文同样落盘可追回。要点：

- 只压**中段**，头部（history + 用户问题）与尾部（最近 K 步）不动——
  前缀缓存命中的正是头部，动它等于每步都按全价重算；
- 攒批压，且两次压缩之间至少隔 N 步，避免抖动；
- 压缩本身是一次 LLM 调用，失败只记日志并跳过，绝不能让本轮失败。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 骨架消息的标记与其中 block_id 的提取（用于「合并旧骨架」与「检索块」）
_SKELETON_MARK = "[上下文压缩]"
# 骨架里可能列多个 id（block_id="a"、"b"），所以按「引号包裹的 ctx: 串」提取
_CHUNK_ID_RE = re.compile(r'"(ctx:[0-9a-f]{6,})"')

# 清理节流：最多每小时扫一次盘
_last_cleanup_at: float = 0.0

# 配置缓存（同 a2a/settings.py：TTL + 文件指纹，改完立即生效）
_cfg_cache: Any = None
_cfg_cache_at: float = 0.0
_cfg_fp: Any = None
_CFG_TTL = 5.0

# 块 id 前缀：与记忆块的 turn: 并列，read 工具按前缀分流
CHUNK_PREFIX = "ctx:"


def invalidate_cfg_cache() -> None:
    global _cfg_cache, _cfg_cache_at, _cfg_fp
    _cfg_cache = None
    _cfg_cache_at = 0.0
    _cfg_fp = None


def ctx_cfg() -> Any:
    """读上下文配置；读不到就用默认值（保守：不外部化、不压缩）。"""
    global _cfg_cache, _cfg_cache_at, _cfg_fp
    now = time.time()
    try:
        from ..setting import fingerprint

        fp: Any = fingerprint()
    except Exception:  # noqa: BLE001
        fp = None
    if (
        _cfg_cache is not None
        and fp is not None
        and _cfg_fp == fp
        and now - _cfg_cache_at < _CFG_TTL
    ):
        return _cfg_cache
    try:
        from ..config import load_config

        _cfg_cache = load_config().context
    except Exception as e:  # noqa: BLE001
        logger.debug("读取上下文配置失败，用默认值: %s", e)
        try:
            from ..config import ContextSection

            _cfg_cache = ContextSection()
        except Exception:  # noqa: BLE001
            _cfg_cache = None
    _cfg_cache_at = now
    _cfg_fp = fp
    return _cfg_cache


# ---- 块存储 ----

def _base_dir() -> Optional[Path]:
    cfg = ctx_cfg()
    if cfg is None:
        return None
    d = os.path.expanduser(getattr(cfg, "dir", "") or "~/.keeper/context-store")
    return Path(d)


def _scope() -> Tuple[str, str]:
    """当前归属（session / message）；拿不到就归到 unknown，仍能存取。"""
    try:
        from ..chat.context import current_message_id, current_session_id

        return (current_session_id() or "unknown-session", current_message_id() or "unknown-message")
    except Exception:  # noqa: BLE001
        return ("unknown-session", "unknown-message")


def _chunk_id(content: str) -> str:
    """内容哈希做 id：同内容 → 同 id，重放时不会重复落盘。"""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]


def save_chunk(content: str) -> Optional[str]:
    """把内容存成块，返回 ``ctx:<id>``；失败返回 None（调用方降级为截断）。"""
    try:
        base = _base_dir()
        if base is None:
            return None
        cid = _chunk_id(content)
        sid, mid = _scope()
        d = base / sid / mid
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{cid}.txt"
        if not f.exists():  # 同内容已存过（重放场景）→ 不动
            f.write_text(content, encoding="utf-8")
        maybe_cleanup(base)
        return CHUNK_PREFIX + cid
    except Exception as e:  # noqa: BLE001
        logger.debug("外部化上下文块失败: %s", e)
        return None


def maybe_cleanup(base: Optional[Path] = None) -> None:
    """清理过期的块文件与空目录（最多每小时一次）。

    为什么必须有：外部化会不断产生新块，而且「先外部化、后又被压缩打包」
    会留下一些不再被引用的旧块。没有回收，磁盘会无限长。
    """
    global _last_cleanup_at
    now = time.time()
    if now - _last_cleanup_at < 3600:
        return
    _last_cleanup_at = now
    try:
        cfg = ctx_cfg()
        days = int(getattr(cfg, "retain_days", 7) or 0) if cfg is not None else 0
        base = base or _base_dir()
        if days <= 0 or base is None or not base.exists():
            return
        deadline = now - days * 86400
        for f in base.rglob("*.txt"):
            try:
                if f.stat().st_mtime < deadline:
                    f.unlink()
            except Exception:  # noqa: BLE001
                pass
        # 由深到浅删空目录
        for d in sorted(base.rglob("*"), key=lambda p: -len(p.parts)):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.debug("清理上下文块失败: %s", e)


def read_chunk(block_id: str) -> str:
    """按 ``ctx:<id>`` 取回原文（供 read 工具展开）。"""
    cid = block_id[len(CHUNK_PREFIX):] if block_id.startswith(CHUNK_PREFIX) else block_id
    cid = "".join(ch for ch in cid if ch.isalnum())[:10]  # 防路径穿越
    base = _base_dir()
    if base is None or not cid:
        return "[上下文存储不可用]"
    sid, mid = _scope()
    for p in (base / sid / mid / f"{cid}.txt", base / sid / f"{cid}.txt"):
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
    # 归属变了（比如从详情面板回看）：按 id 全目录找一次
    try:
        for p in base.rglob(f"{cid}.txt"):
            return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return f"[未找到上下文块 {CHUNK_PREFIX}{cid}（可能已被清理）]"


def _is_skeleton(msg: dict) -> bool:
    """是否是一条骨架消息（压缩产物）。"""
    return str(msg.get("content") or "").lstrip().startswith(_SKELETON_MARK)


def search_chunks(query: str, limit: int = 5) -> List[Tuple[str, str]]:
    """按关键词在**本轮外部化的块**里检索，返回 ``(block_id, 摘要)``。

    为什么需要：模型只能靠上下文里还看得见的 block_id 去 read；块一旦被压进骨架，
    它若没记住 id 就再也找不到。补上检索后，``recall`` 既能搜历史记忆（``turn:``）
    也能搜本轮被外部化的上下文（``ctx:``），取回路径闭合。

    实现上刻意保持笨而快：只扫当前会话、最多 200 个块、每块只读前 64KB 做子串
    匹配——命中率够用，不会为了检索把磁盘读穿。
    """
    out: List[Tuple[str, str]] = []
    try:
        base = _base_dir()
        if base is None or not query or not query.strip():
            return out
        sid, _mid = _scope()
        scope = base / sid
        if not scope.exists():
            return out
        terms = [t for t in re.split(r"[\s,，;；]+", query) if len(t) >= 2][:6]
        if not terms:
            return out
        scored: List[Tuple[int, float, str, str]] = []
        files = sorted(scope.rglob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:200]:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")[:65536]
            except Exception:  # noqa: BLE001
                continue
            hit = sum(1 for t in terms if t in text)
            if hit:
                scored.append((hit, f.stat().st_mtime, f.stem, text))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        for _hit, _mtime, stem, text in scored[: max(1, limit)]:
            snippet = ""
            for t in terms:
                p = text.find(t)
                if p >= 0:
                    snippet = text[max(0, p - 60): p + 140].replace("\n", " ")
                    break
            summary = (snippet or text[:200].replace("\n", " ")).strip()
            out.append(
                (
                    CHUNK_PREFIX + stem,
                    f"（本轮外部化的上下文块，{len(text)} 字符）…{summary}…",
                )
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("检索上下文块失败: %s", e)
    return out


# ---- 外部化 ----

# 可选豁免名单：**填了的工具永不外部化**（连退出保活窗口也不压）。
# 默认空——即所有工具一视同仁：只要退出保活窗口且超过阈值就换引用。
# 想给某类输出开小灶（比如某工具的产物每一步都要回头引用）时再填。
NEVER_EXTERNALIZE_DEFAULT = ""


def _never_externalize(tool: str) -> bool:
    """工具是否在豁免名单里（支持 ``task.*`` 这类前缀）；名单为空则一律不豁免。"""
    cfg = ctx_cfg()
    raw = (
        getattr(cfg, "never_externalize", None)
        if cfg is not None
        else None
    ) or NEVER_EXTERNALIZE_DEFAULT
    name = (tool or "").strip()
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        if item.endswith("*"):
            if name.startswith(item[:-1]):
                return True
        elif item == name:
            return True
    return False


def _preview(raw: str, head: int, tail: int) -> str:
    """生成预览：**列表类输出按行截断**，让模型看得到「共多少条」。

    首尾硬截断会把一份目录清单切成半截——模型既看不全，也不知道缺了多少；
    按行截断并报出行数，它至少知道「还有 N 行没看到」，会去 read 而不是瞎猜。
    """
    lines = raw.splitlines()
    if len(lines) > 12:
        keep_head, keep_tail = 8, 2
        omitted = len(lines) - keep_head - keep_tail
        head_txt = "\n".join(lines[:keep_head])
        tail_txt = "\n".join(lines[-keep_tail:]) if keep_tail else ""
        return (
            f"{head_txt}\n…（共 {len(lines)} 行，中间省略 {omitted} 行）…\n{tail_txt}"
        )
    if head <= 0:
        return ""
    if head + tail >= len(raw):
        return raw
    out = raw[:head]
    if tail:
        out += f"\n…（中间省略 {len(raw) - head - tail} 字符）…\n" + raw[-tail:]
    return out


def clip_observation(raw: str, limit: int) -> str:
    """把单条工具输出截到 ``limit``：**保留开头和结尾**，砍中间。

    原来直接 ``raw[:limit]`` 是硬切——而报错信息、最终结论、最后一条命中常常在
    结尾，被砍掉后模型会拿着半截结果下结论（比如以为"没有匹配"，实际尾部有）。
    现在按 70/30 分配头尾，中间明确标出省略了多少字符。
    """
    try:
        if limit <= 0 or len(raw) <= limit:
            return raw
        head = max(1, int(limit * 0.7))
        tail = max(0, limit - head)
        omitted = len(raw) - head - tail
        out = raw[:head]
        if tail:
            out += f"\n…（中间省略 {omitted} 字符，共 {len(raw)} 字符）…\n" + raw[-tail:]
        else:
            out += f"\n…（后文省略 {omitted} 字符）"
        return out
    except Exception:  # noqa: BLE001
        return raw[:limit] if limit > 0 else raw


def externalize(tool: str, raw: str) -> str:
    """把工具输出换成「引用 + 预览」；不触发时原样返回。

    **只由滚动外部化调用**——即「这一步已退出保活窗口」且「超过阈值」时才压。
    刚返回的结果永远完整可见：模型下一步要用它，换成预览就会漏内容
    （漏文件、漏条目），比多花 token 严重得多。单条过大的保护交给
    ``Process.observation_limit`` 的硬截断，不在这里做事后补救。

    只影响**给模型看的**文本；落库/展示仍用原文（UI 不受影响）。
    """
    cfg = ctx_cfg()
    if cfg is None or not getattr(cfg, "enabled", True):
        return raw
    try:
        min_chars = int(getattr(cfg, "externalize_min_chars", 0) or 0)
        if min_chars <= 0 or len(raw) < min_chars:
            return raw
        if _never_externalize(tool):
            return raw

        block_id = save_chunk(raw)
        if not block_id:
            return raw
        head = int(getattr(cfg, "preview_head", 400) or 0)
        tail = int(getattr(cfg, "preview_tail", 200) or 0)
        preview = _preview(raw, head, tail)
        return (
            f"[工具 {tool} 返回 {len(raw)} 字符，已存为外部块 "
            f'block_id="{block_id}"。以下只是预览，**不完整**：需要据此下结论时'
            f"先用 read 工具传入该 block_id 展开全文。]\n{preview}"
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("外部化工具输出失败: %s", e)
        return raw


# ---- 压缩 ----

def estimate_tokens(messages: List[dict]) -> int:
    """粗估输入 token：字符数 / ``chars_per_token``（中文≈1.5，英文≈4，默认 2.5）。

    只用于判断水位，不参与记账——记账以 provider 上报为准。系数可按所用模型
    与语料调整：全是中文就调小（1.5~2），代码/英文多就调大（3~4）。
    """
    try:
        cfg = ctx_cfg()
        per = float(getattr(cfg, "chars_per_token", 2.5) or 2.5) if cfg else 2.5
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        return int(chars / per)
    except Exception:  # noqa: BLE001
        return 0


class Compactor:
    """单轮 ReAct 的上下文压缩器（每轮一个实例）。"""

    def __init__(self, llm: Any) -> None:
        self.llm = llm
        self.last_compacted_step = 0
        self.compacted_steps = 0  # 已压掉多少步（用于骨架里的序号）
        self.chunks: List[str] = []

    # -- 触发判断 --
    def should_compact(self, messages: List[dict], steps_done: int) -> bool:
        cfg = ctx_cfg()
        if cfg is None or not getattr(cfg, "enabled", True):
            return False
        if not getattr(cfg, "compact_enabled", True):
            return False
        if steps_done - self.last_compacted_step < int(
            getattr(cfg, "compact_min_interval", 5) or 0
        ):
            return False  # 刚压过：抖动保护

        limit = int(getattr(cfg, "model_context_limit", 64000) or 64000)
        ratio = float(getattr(cfg, "compact_ratio", 0.7) or 0.7)
        tokens = estimate_tokens(messages)

        # ① 真·水位触发：上下文确实胖了
        if tokens >= int(limit * ratio):
            return True

        # ② 步数触发：必须**同时**到「水位下限」（compact_min_ratio）。
        # 只看步数是错的——实测 20 步时输入才 14k token（窗口 22%），此时压缩
        # 省下的多是**缓存命中价**的 token，却让后续一批变成未命中价，每步成本
        # 反而涨 13%。所以步数只能是「并且」条件，不能单独触发。
        floor = float(getattr(cfg, "compact_min_ratio", 0.5) or 0.5)
        if (
            steps_done >= int(getattr(cfg, "compact_min_steps", 20) or 20)
            and tokens >= int(limit * floor)
        ):
            return True
        return False

    # -- 滚动外部化 --
    def rolling_externalize(self, messages: List[dict]) -> List[dict]:
        """把**退出保活窗口**的旧工具输出换成引用（最近 K 步保持原文）。

        这是外部化的主力路径：刚返回的结果完整可见（模型正在用它），等它走出
        最近 K 步、大概率已被消化后，才把原文换成「引用 + 预览」。与压缩共用
        同一个保活窗口，语义统一：**只有「最近 K 步」是模型必须看得全的**。
        """
        cfg = ctx_cfg()
        if cfg is None or not getattr(cfg, "enabled", True):
            return messages
        keep_steps = max(1, int(getattr(cfg, "keep_recent_steps", 6) or 6))
        assistant_idx = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if len(assistant_idx) <= keep_steps:
            return messages  # 还在保活窗口内，一步都不动
        start = assistant_idx[len(assistant_idx) - keep_steps]

        # 追回（read）回来的内容只保**最近 N 条**：保太多会让上下文重新膨胀，
        # 不保则会出现「读了 → 被压 → 再读」的抖动。
        pin_n = int(getattr(cfg, "pin_recent_reads", 2) or 0)
        pinned = [i for i, m in enumerate(messages) if m.get("_pinned")]
        keep_pins = set(pinned[-pin_n:]) if pin_n > 0 else set()

        out: List[dict] = []
        changed = False
        for i, m in enumerate(messages):
            if i in keep_pins:
                out.append(m)
                continue
            if m.get("_cap"):
                # 按需加载的能力正文（load_capabilities 的结果）：它是「接下来几步
                # 照着做」的说明书，**不是**一次性的工具结果，换成引用等于把说明书
                # 抽走。什么时候清它归 Process 的容量管理（FIFO + 换出告知），
                # 这里不碰——两边职责分开，互不猜对方的心思。
                out.append(m)
                continue
            if (
                i < start
                and m.get("role") == "tool"
                and not m.get("_ext")  # 已换过的不再处理
                and "block_id=" not in str(m.get("content") or "")[:200]
            ):
                content = str(m.get("content") or "")
                if content:
                    new = externalize(m.get("name") or "tool", content)
                    if new != content:
                        m = {**m, "content": new, "_ext": True}
                        changed = True
            out.append(m)
        if changed:
            logger.info("[context-compact] 滚动外部化：%s 条旧工具结果已换成引用", sum(
                1 for m in out if m.get("_ext")
            ))
        return out if changed else messages

    # -- 执行压缩 --
    async def compact(self, messages: List[dict], steps_done: int) -> Optional[List[dict]]:
        """把最早的一批步骤压成骨架；返回新的 messages，没压/失败返回 None。"""
        cfg = ctx_cfg()
        if cfg is None:
            return None
        keep_steps = max(1, int(getattr(cfg, "keep_recent_steps", 6) or 6))
        # 一条步 = assistant(THOUGHT/ACTION) + tool(OBSERVATION) 两条消息
        keep_msgs = keep_steps * 2
        if len(messages) <= keep_msgs + 2:
            return None

        # 头部：一直压到「倒数 keep_msgs 条」之前；但不动 history 与第一条用户问题
        # （它们是前缀缓存命中的部分，动了就每步全价）
        start = self._first_step_index(messages)
        if start < 0:
            return None
        end = len(messages) - keep_msgs
        if end - start < 2:
            return None

        old = messages[start:end]
        block_id = save_chunk(self._render(old))

        # 已有骨架（上一次压缩留下的）：把它**合并**进新骨架，而不是并排放着。
        # 否则压 N 次就留下 N 条骨架，越压越多——骨架本来就是为了省地方。
        prior_idx = [i for i, m in enumerate(messages) if _is_skeleton(m)]
        prior_ids: List[str] = []
        prior_text = ""
        for i in prior_idx:
            c = str(messages[i].get("content") or "")
            prior_ids.extend(_CHUNK_ID_RE.findall(c))
            prior_text += c + "\n"

        skeleton = await self._summarize(old, cfg, prior_summary=prior_text)
        if not skeleton:
            return None  # 摘要失败 → 不压缩，宁可多花 token 也别丢信息

        all_ids = ([block_id] if block_id else []) + [
            i for i in prior_ids if i not in ([block_id] if block_id else [])
        ]
        ids_txt = "、".join(f'"{i}"' for i in all_ids) if all_ids else "（无）"
        first = self.compacted_steps + 1
        last = self.compacted_steps + self._count_steps(old)
        rng = f"{first}-{last}" if prior_idx else f"{first}-{last}（含更早已压步骤）"
        note = (
            f"{_SKELETON_MARK} 早期步骤 {rng} 的原文已存为外部块 "
            f"block_id={ids_txt}，需要细节时用 read 工具传入 block_id 展开。\n"
            "以下为按步骤顺序保留的摘要（含结论、关键数字与路径）：\n"
        )
        new_messages = (
            [m for i, m in enumerate(messages[:start]) if i not in prior_idx]
            + [{"role": "user", "content": note + skeleton}]
            + messages[end:]
        )
        self.compacted_steps += self._count_steps(old)
        self.last_compacted_step = steps_done
        if block_id:
            self.chunks.append(block_id)
        before, after = estimate_tokens(messages), estimate_tokens(new_messages)
        logger.info(
            "[context-compact] 压缩 %s 条消息（合并 %s 条旧骨架）：约 %s → %s token",
            len(old), len(prior_idx), before, after,
        )
        return new_messages

    # -- 内部 --
    @staticmethod
    def _first_step_index(messages: List[dict]) -> int:
        """第一条「ReAct 步骤」消息的下标（跳过 history / 用户问题 / 已有骨架）。"""
        for i, m in enumerate(messages):
            if m.get("role") == "assistant":
                return i
        return -1

    @staticmethod
    def _count_steps(chunk: List[dict]) -> int:
        return sum(1 for m in chunk if m.get("role") == "assistant")

    @staticmethod
    def _render(chunk: List[dict]) -> str:
        """把待压消息渲染成可回看的原文（保留顺序与 step 序号）。"""
        out: List[str] = []
        n = 0
        for m in chunk:
            role = m.get("role")
            content = str(m.get("content") or "")
            if role == "assistant":
                n += 1
                out.append(f"=== step {n}（模型输出）===\n{content}")
            elif role == "tool":
                out.append(f"--- step {n} 工具 {m.get('name') or ''} 返回 ---\n{content}")
            else:
                out.append(f"--- {role} ---\n{content}")
        return "\n\n".join(out)

    async def _summarize(
        self, chunk: List[dict], cfg: Any, prior_summary: str = ""
    ) -> str:
        """调 LLM 生成骨架摘要；失败返回空串（调用方据此放弃压缩）。"""
        max_chars = int(getattr(cfg, "compact_max_chars", 1500) or 1500)
        src = self._render(chunk)
        if prior_summary.strip():
            # 已有骨架：要求「合并」而不是另写一份——否则旧结论会在多份骨架里
            # 各说一遍，越压越长。
            src = (
                "【已有的早期摘要（必须与之合并，不要重复、不要丢结论）】\n"
                + prior_summary.strip()
                + "\n\n【本次新增的早期步骤原文】\n"
                + src
            )
        system = (
            "你是上下文压缩器。把下面「早期对话步骤」压缩成一份按步骤顺序排列的骨架，"
            "供后续推理使用。要求：\n"
            "1) 保留顺序与编号，每步一行，格式：`step N 结论`；\n"
            "2) 每步只留**结论与关键事实**（路径、文件名、数字、报错、接口名），"
            "不要写「调用了某工具」这种无信息量的动作描述；\n"
            "3) 必须保留**决策与否定约束**（决定了不用某方案及其原因），"
            "丢掉它会导致后续重复走回头路；\n"
            "4) 用户明确说过的要求原样保留；\n"
            f"5) 不超过 {max_chars} 字，不要输出任何前言或解释。"
        )
        try:
            from ..chat.context import set_llm_call_kind, set_step_seq

            # 压缩不是某一步：清掉 step_seq 并单独标 kind，时间线里才看得懂
            set_step_seq(None)
            set_llm_call_kind("context_compact")
        except Exception:  # noqa: BLE001
            pass
        try:
            text = await self.llm.chat(
                [{"role": "user", "content": src}], system=system
            )
            return (text or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("上下文压缩失败（保持原上下文）: %s", e)
            return ""
