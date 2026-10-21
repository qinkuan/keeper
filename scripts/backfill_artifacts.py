"""一次性回填：把工作区里还在磁盘上的产物文件，重新挂回对应的助手消息。

适用场景
--------
产物元数据没落库（历史消息 ``session_messages.artifacts`` 为 NULL / ``[]``），
但文件还在 ``~/.keeper/workspace/session/<session_id>/`` 下。典型是「交付完图又追问 /
生成到一半被停」这类轮次，产物捕获被跳过，重启后历史里图全丢——文件没丢，只是没登记。

做法
----
遍历每个会话工作区，把文件按 mtime 映射到「修改时间之前最新的一条助手消息」，
给那条（且仅有那条）artifacts 为空的助手消息补登产物。文本型（mermaid / csv / json /
md…）会内联 ``src``，重启后即使文件路径取不到也能渲染；二进制（图片 / 视频 / pdf）只登记
``path``，由前端按原路径取。

幂等：artifacts 已非空的消息不动；同一文件只会挂到一条消息；``--dry-run`` 只打印不发写。

用法
----
    python scripts/backfill_artifacts.py                 # 默认库，真正写入
    python scripts/backfill_artifacts.py --dry-run       # 只预览
    python scripts/backfill_artifacts.py --db /path/to/keeper.db
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

# 必须在导入 keeper 之前指好 HOME（模块里多处用 Path.home() 定位 ~/.keeper）
_TMP_HOME = Path.home()
# keeper 包位于仓库根的父目录（repo/keeper/ 是包本身），把仓库根上一级加进路径
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select, update  # noqa: E402

from keeper.agent.config import default_agent_space_root  # noqa: E402
from keeper.store import get_session_factory, init_db  # noqa: E402
from keeper.store.database import DEFAULT_DB_PATH  # noqa: E402
from keeper.store.models import ChatSession, SessionMessage  # noqa: E402

# 与 service.py 一致的文本型扩展名：这些内联 src，避免重启后路径失效。
_INLINE_EXT = {
    "mmd", "mermaid", "md", "markdown", "mdx",
    "csv", "tsv", "json", "json5", "geojson", "yaml", "yml", "toml",
    "txt", "log", "html", "htm", "svg", "py", "js", "ts", "tsx", "jsx",
    "java", "go", "rs", "c", "cpp", "h", "sh", "bash", "css", "scss", "sql",
}
_SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git", ".idea", ".vscode", "tmp"}
_MAX_INLINE = 200_000


def _guess_mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
        "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
        "pdf": "application/pdf", "html": "text/html", "htm": "text/html",
        "json": "application/json", "csv": "text/csv", "md": "text/markdown",
        "mmd": "text/vnd.mermaid", "yaml": "text/yaml", "yml": "text/yaml",
        "txt": "text/plain", "log": "text/plain",
    }.get(ext, "application/octet-stream")


def _render_hint(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "svg",
               "mp4", "webm", "ogg", "ogv", "mov", "m4v", "avi", "mkv", "pdf"):
        return "preview"
    if ext in ("html", "htm"):
        return "run"
    if ext in _INLINE_EXT:
        return "inline"
    return "folder"


def _build_artifact(message_id: str, p: Path, ref: str | None = None) -> dict:
    name = p.name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    rel = str(p)
    art: dict = {
        "id": hashlib.sha1(f"{message_id}:{rel}".encode()).hexdigest()[:12],
        "name": name,
        "path": "file://" + str(p),
        "mime": _guess_mime(name),
        "size": p.stat().st_size,
    }
    if ext == "mmd" or name.endswith(".mmd"):
        art["kind"] = "mermaid"
    else:
        art["kind"] = "file"
        art["render"] = _render_hint(name)
    # 内联 ref：让正文里的 [[art:<ref>]] 令牌能就地替换成卡片（否则只能沉到底部）
    if ref:
        art["ref"] = ref
    # 文本型内联 src：重启后不依赖磁盘路径
    if ext in _INLINE_EXT:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            if len(text) <= _MAX_INLINE:
                art["src"] = text
        except Exception:
            pass
    return art


def _assign_refs(files: list[Path], refs: list[str]) -> list[tuple[Path, str | None]]:
    """把工作区文件映射到正文里的 [[art:<ref>]] 令牌。

    优先按文件名 stem 精确 / 包含匹配（agent 常把 ref 设成文件名），剩余文件按出现顺序
    兜底到剩余令牌；实在对不上的给 None（会沉到底部，不报错）。
    """
    used: set[str] = set()
    pairs: list[tuple[Path, str | None]] = []
    leftover: list[Path] = []
    for f in files:
        stem = f.stem
        matched: str | None = None
        if stem in refs and stem not in used:
            matched = stem
        else:
            for r in refs:
                if r not in used and (r in stem or stem in r):
                    matched = r
                    break
        if matched:
            used.add(matched)
            pairs.append((f, matched))
        else:
            leftover.append(f)
    leftover_refs = [r for r in refs if r not in used]
    for i, f in enumerate(leftover):
        pairs.append((f, leftover_refs[i] if i < len(leftover_refs) else None))
    return pairs


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    await init_db(f"sqlite+aiosqlite:///{args.db}")
    factory = get_session_factory()

    total, patched = 0, 0
    async with factory() as db:
        sessions = (await db.execute(select(ChatSession))).scalars().all()

    for sess in sessions:
        ws = default_agent_space_root(sess.id, sess.agent_id or "")
        if not Path(ws).exists():
            continue
        async with factory() as db:
            msgs = (
                await db.execute(
                    select(SessionMessage)
                    .where(SessionMessage.chat_session_id == sess.id)
                    .where(SessionMessage.role == "assistant")
                    .order_by(SessionMessage.created_at)
                )
            ).scalars().all()

        # 需处理的消息：artifacts 为空，或存在但至少缺 ref（即此前回填 / 异常捕获的产物，
        # 没有 ref 就无法被正文里的 [[art:REF]] 令牌就地引用，只能沉到底部）。
        # 已经带 ref 的正常产物不动，避免覆盖。可重跑。
        def _needs(m: SessionMessage) -> bool:
            raw = m.artifacts
            if not (raw and json.loads(raw)):
                return True
            return any(not a.get("ref") for a in json.loads(raw))

        targets = [m for m in msgs if _needs(m)]
        if not targets:
            continue
        targets_sorted = sorted(targets, key=lambda m: m.created_at or "")
        # 会话全部工作区文件（用于「文件名包含 [[art:REF]] 令牌」的匹配）
        all_files = [
            f for f in Path(ws).rglob("*")
            if f.is_file()
            and not any(part in _SKIP_DIRS or part.startswith(".") for part in f.relative_to(ws).parts)
        ]
        # 工作区文件 → 修改时间之前最新的一条 target 消息（mtime 兜底归属）
        by_msg: dict[str, list] = {m.id: [] for m in targets_sorted}
        for f in all_files:
            mt = f.stat().st_mtime
            target = None
            for m in reversed(targets_sorted):
                ct = m.created_at.timestamp() if m.created_at else 0
                if ct <= mt:
                    target = m
                    break
            if target is not None:
                by_msg[target.id].append(f)

        for m in targets_sorted:
            content = m.content or ""
            refs = [r.strip() for r in re.findall(r"\[\[art:([^\]]+)\]\]", content)]
            # 令牌名匹配：正文 [[art:REF]] 的 REF 出现在某文件名里 → 该文件挂到这条消息并带 ref，
            # 这样内联令牌能就地解析（图/表常在被引用的那条消息上，而非最初生成的那条）。
            token_files: list[Path] = []
            for r in refs:
                for f in all_files:
                    if r.lower() in f.name.lower() and f not in token_files:
                        token_files.append(f)
            # 候选文件 = 令牌名命中 ∪ mtime 兜底归属（去重保序）
            cand = list(dict.fromkeys(token_files + by_msg.get(m.id, [])))
            # 现有已带 ref 的产物保留（合并，不覆盖正常捕获），按 name 去重
            existing = json.loads(m.artifacts) if (m.artifacts and json.loads(m.artifacts)) else []
            kept = [a for a in existing if a.get("ref")]
            kept_names = {a.get("name") for a in kept}
            new_arts = list(kept)
            # 令牌命中的文件直接带对应 ref
            used_refs: set[str] = set()
            for f in token_files:
                matched = next((r for r in refs if r.lower() in f.name.lower()), None)
                if matched and f.name not in kept_names:
                    used_refs.add(matched)
                    new_arts.append(_build_artifact(m.id, f, ref=matched))
            # 其余候选走 stem/顺序兜底（排除已令牌命中的），用剩余未用 ref
            rest = [f for f in cand if f not in set(token_files)]
            for f, ref in _assign_refs(rest, [r for r in refs if r not in used_refs]):
                if f.name in kept_names:
                    continue
                new_arts.append(_build_artifact(m.id, f, ref=ref))
            # 无变化则跳过（可重跑安全）
            if len(new_arts) == len(existing) and all(a.get("ref") for a in new_arts):
                continue
            # 最终去重（name + ref）
            seen: set = set()
            final = []
            for a in new_arts:
                key = (a.get("name"), a.get("ref"))
                if key in seen:
                    continue
                seen.add(key)
                final.append(a)
            total += len(final)
            if args.dry_run:
                print(f"[dry-run] session={sess.id} msg={m.id} ← "
                      f"{[ (a['name'], a.get('ref')) for a in final ]}")
                continue
            async with factory() as wdb:
                await wdb.execute(
                    update(SessionMessage)
                    .where(SessionMessage.id == m.id)
                    .values(artifacts=json.dumps(final, ensure_ascii=False))
                )
                await wdb.commit()
            patched += 1
            print(f"[ok] session={sess.id} msg={m.id} ← {len(final)} 个产物"
                  f"（含 ref：{[a['ref'] for a in final if a.get('ref')]}）")

    print(f"\n扫描完成：登记 {total} 个产物，写入 {patched} 条消息"
          f"{'（dry-run，未写入）' if args.dry_run else ''}")


if __name__ == "__main__":
    asyncio.run(main())
