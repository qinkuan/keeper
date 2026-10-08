"""文件系统类内置工具：列目录 / 读文件 / 写文件。

所有操作都限制在 agent 的**工作空间**内（见 ``sandbox.safe_path``）。
写类工具（``fs.write_file``）在只读工作空间下运行时返回拒绝。

工厂签名统一为 ``make_xxx(root, read_only) -> ProcessorTool``：
``root`` / ``read_only`` 是**兜底值**（装配期传入的 agent 默认工作目录），
运行时优先取当前请求的工作空间（``chat.context.current_workspace``），
从而支持「同一 agent 在不同会话指向不同用户空间」。
"""
from __future__ import annotations

import asyncio
import fnmatch
import heapq
import os
from pathlib import Path
from typing import List, Optional

from ..base import ProcessorTool
from .sandbox import TMP_PREFIX, PathEscape, resolve_path

# 单次读取的最大字节数：只取文件头部，避免大文件把内存撑爆 / 阻塞事件循环
# （约对应 20 万 ASCII 字符，或约 6 万汉字；超出部分截断）。
MAX_READ_BYTES = 200_000
# 单次列出的最大条目数
MAX_LIST = 200


def _effective(fallback_root: Path, fallback_ro: bool):
    """取当前请求的 (root, read_only, tmp_root)；无工作空间上下文则回落兜底值。

    ``tmp_root`` 是本 (会话, agent) 的私有目录，模型用 ``@tmp/`` 前缀指代它
    （见 :func:`sandbox.resolve_path`）。它是工作区**之外**的一个额外合法根，
    用来放测试脚本之类的临时文件——不放出去的话，模型写临时文件时只能往用户
    工作区根下堆。
    """
    from ...chat.context import current_workspace

    ws = current_workspace()
    if ws is None:
        return Path(fallback_root), bool(fallback_ro), None
    return ws.root, ws.read_only, ws.agent_space


def make_list_dir(root: Path, read_only: bool) -> ProcessorTool:
    """列出工作空间内的目录内容。"""
    async def run(args: dict) -> str:
        eff_root, _, tmp_root = _effective(root, read_only)
        rel = args.get("path") or "."
        try:
            d = resolve_path(eff_root, tmp_root, rel)
        except PathEscape as e:
            return f"[拒绝] {e}"
        if not d.is_dir():
            return f"[不是目录] {rel}"
        # 先全列举，再只保留 / 排序前 MAX_LIST 个：超大目录也只占一份列表，
        # 并用 nsmallest 省去全局排序开销；超出则标注已截断。
        entries = list(d.iterdir())
        truncated = len(entries) > MAX_LIST
        if truncated:
            items = heapq.nsmallest(MAX_LIST, entries, key=lambda p: p.name)
        else:
            items = sorted(entries, key=lambda p: p.name)
        if not items:
            return "（空目录）"
        body = "\n".join(
            "[D] " + i.name if i.is_dir() else "[F] " + i.name for i in items
        )
        if truncated:
            body += f"\n...(已截断，共 {len(entries)} 项)"
        return body

    return ProcessorTool(
        name="fs.list_dir",
        description="列出工作空间内的目录内容（[D] 目录，[F] 文件）",
        parameters={"path": f"相对用户工作区的目录路径，默认 '.'；{TMP_PREFIX}/ 前缀表示会话临时工作区"},
        read_only=True,  # 只读：工具并行化时可与其它只读工具并发
        run=run,
    )


def make_read_file(root: Path, read_only: bool) -> ProcessorTool:
    """读取工作空间内的文本文件。

    健壮性（防大文件卡死 / OOM）：
    - 读前先 ``stat`` 查大小，**只取头部 N 字节**（截断在读之前），绝不整文件载入内存；
    - 阻塞 IO 放到线程里跑（``asyncio.to_thread``），不阻塞事件循环，
      从而一个 agent 读大文件不会拖垮进程内其它 agent。
    """
    async def run(args: dict) -> str:
        eff_root, _, tmp_root = _effective(root, read_only)
        rel = args.get("path") or ""
        if not rel:
            return "[缺少参数 path]"
        try:
            p = resolve_path(eff_root, tmp_root, rel)
        except PathEscape as e:
            return f"[拒绝] {e}"
        if not p.is_file():
            return f"[文件不存在] {rel}"
        size = p.stat().st_size
        if size == 0:
            return "（空文件）"
        # 只读头部：无论文件多大，最多进内存 MAX_READ_BYTES 字节
        def _head(path: Path, limit: int) -> bytes:
            with path.open("rb") as f:
                return f.read(limit)
        data = await asyncio.to_thread(_head, p, MAX_READ_BYTES)
        text = data.decode("utf-8", errors="replace")
        if size > MAX_READ_BYTES:
            return (
                text
                + f"\n...(已截断，共 {size} 字节，上限 {MAX_READ_BYTES} 字节)"
            )
        return text

    return ProcessorTool(
        name="fs.read_file",
        description=(
            "读取工作空间内文本文件的内容，**仅供你（agent）自己处理 / 分析 / 引用**，"
            "不会在对话里给用户展示文件。注意：若用户只是想*看到*某个文件本身"
            "（「看看 / 看下 X 文件」「把 X 给我看下」），请用 **fs.publish** 生成可预览卡片，"
            "而不要用本工具把文件内容直接贴进你的回答——那样既无高亮也不美观。"
        ),
        parameters={"path": f"相对用户工作区的文件路径；{TMP_PREFIX}/ 前缀表示会话临时工作区"},
        read_only=True,  # 只读：可并发
        run=run,
    )


def _write_arg_missing(content: str) -> str:
    """``write_file`` 收到空 path 时的诊断信息。

    绝大多数情况不是「模型忘了传 path」，而是**它想写的正文太长，LLM 输出在
    JSON 闭合前就被 max_tokens 截断**，于是解析出来的 args 是空的
    （实测一次 30 步打转的根因就是这个）。

    原来只回「[缺少参数 path]」——模型拿不到任何线索，只能原样重试；下一轮
    继续输出同样长度的内容、继续被截断、继续空参数，于是原地打转。

    所以这里要把**成因和对策**一起给它：分段写 + 合并，而且明确给出可用的
    路径前缀，让它不用再去猜临时目录怎么访问。
    """
    got_content = bool(content)
    return (
        "[缺少参数 path —— 很可能是正文太长，LLM 输出在本工具的 JSON 闭合之前"
        "就被 max_tokens 截断了，所以 path 和 content 一起丢掉。]\n"
        "不要原样重试（同样长度的内容会再次被截断），改用分段写入：\n"
        f"1. 用 fs.write_file 分几次写 {TMP_PREFIX}/part1.txt、part2.txt…，"
        "每次只写一两千字符（仍用 @tmp/ 前缀，它是会话私有目录，不会出现在交付物里）；\n"
        "2. 全部写完后用一条 bash 命令把它们按顺序拼起来，"
        "写到用户工作区里的目标文件名；\n"
        "3. 对那个目标文件调用 fs.publish，用户才能看到可点开的产物卡片。"
        + ("" if got_content else "\n（另外这次连 content 也是空的，说明整个 ACTION 都没解析出来。）")
    )


def make_write_file(root: Path, read_only: bool) -> ProcessorTool:
    """把内容写入文件（父目录自动创建）。

    路径支持 ``@tmp/`` 前缀指向会话私有临时目录（见 :func:`sandbox.resolve_path`）。
    只读工作区下 ``@tmp/`` **仍可写**：只读的语义是「别改用户的文件」，而临时目录
    是 agent 自己的草稿区——禁掉它等于逼 agent 把测试脚本写进用户工作区。
    """
    async def run(args: dict) -> str:
        eff_root, eff_ro, tmp_root = _effective(root, read_only)
        rel = args.get("path") or ""
        content = args.get("content", "")
        if not rel:
            return _write_arg_missing(content)
        is_tmp = rel.strip().lstrip("/") == "@tmp" or rel.strip().lstrip("/").startswith(
            "@tmp/"
        )
        if eff_ro and not is_tmp:
            return "[工作空间为只读，不能写文件]"
        try:
            p = resolve_path(eff_root, tmp_root, rel)
        except PathEscape as e:
            return f"[拒绝] {e}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # 临时目录要给真实绝对路径：``bash__run`` 这类插件不认 ``@tmp/`` 前缀
        # （它起的是真子进程，没有我们的路径解析），agent 只能拿绝对路径去
        # ``cd`` / 传参。写完就告诉它一次，省得它把脚本又写回用户工作区。
        if is_tmp:
            return (
                f"已写入 {rel}（{len(content)} 字符），"
                f"实际路径 {p}（这是会话临时目录，bash 命令里请用这个绝对路径）"
            )
        return f"已写入 {rel}（{len(content)} 字符）"

    return ProcessorTool(
        name="fs.write_file",
        description=(
            "把内容写入文件（父目录自动创建）。"
            "路径相对工作空间；**临时文件请用 `@tmp/` 前缀**"
            "（如 `@tmp/check.py`、`@tmp/test_a.js`）——它落在本会话私有的临时目录，"
            "不会弄脏用户工作区。只有**要交付给用户的文件**才直接写在工作空间里。"
            "\n\n【content 长度限制 —— 超过就会整个调用失败】"
            "content 是要**由你逐字生成**并放进 JSON 的，"
            "所以单次上限受你的输出长度约束（约一万字符）。"
            "超过这个量级时本工具会收到**空的** path 与 content（输出在 JSON 闭合前"
            "就被截断），报「缺少参数 path」——那不是路径写错，别去改路径。\n"
            "长文件必须**分段写**：先用本工具分几次写 `@tmp/part1.txt`、"
            "`part2.txt`…（每次一两千字符），再用一条 bash 命令把它们"
            "按顺序拼成目标文件，最后对目标文件调用 fs.publish 挂载。"
        ),
        parameters={
            "path": (
                "文件路径。相对工作空间；临时文件用 `@tmp/` 前缀（如 `@tmp/check.py`）"
            ),
            "content": (
                "要写入的文本。**单次请控制在约一万字符以内**；更长请分段写入"
                "多个 @tmp/partN.txt 后再合并"
            ),
        },
        run=run,
    )


# ---- 可编辑性判定（fs.edit_file 用）----
# 用**黑名单 + 特征探测**而不是白名单：白名单永远在漏（.toml/.go/.sh/…），
# 每遇一种新后缀就得改代码；黑名单漏判的后果也轻得多。
MAX_EDIT_BYTES = 512_000  # 超过这个大小不做整文件编辑（基本都是生成物）
_SNIFF_BYTES = 8192
_BINARY_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
        ".pdf", ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".class",
        ".wasm", ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf", ".otf",
    }
)
# 这些目录里的文件改了也会被下次构建 / 安装覆盖，纯浪费
_SKIP_DIR_PARTS = frozenset(
    {
        "node_modules", "__pycache__", ".git", ".venv", "venv", "env",
        "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".idea", ".vscode", ".next", ".nuxt", "site-packages",
    }
)


def _is_binary(data: bytes) -> bool:
    """前若干字节里含 NUL 就当二进制（文本里不会出现）。"""
    return b"\x00" in data[:_SNIFF_BYTES]


def _not_editable_reason(p: Path, rel: str) -> Optional[str]:
    """返回「为什么不能编辑这个文件」，可以编辑则返回 ``None``。"""
    if not p.is_file():
        return f"[文件不存在] {rel} —— 新建文件请用 fs.write_file，编辑只针对已有文件"
    parts = {part.lower() for part in p.parts}
    if parts & _SKIP_DIR_PARTS:
        hit = sorted(parts & _SKIP_DIR_PARTS)[0]
        return (
            f"[不可编辑] {rel} 位于 {hit}/ —— 依赖与构建产物改了也会被下次安装/构建"
            "覆盖，等于白改。要改请改源码文件。"
        )
    if p.suffix.lower() in _BINARY_EXTS:
        return f"[不可编辑] {rel} 是二进制文件（{p.suffix}），文本替换会破坏它的结构"
    size = p.stat().st_size
    if size > MAX_EDIT_BYTES:
        return (
            f"[不可编辑] {rel} 有 {size} 字节，超过编辑上限 {MAX_EDIT_BYTES}。"
            "这么大的文件基本是生成物，整体重写更合适。"
        )
    try:
        with p.open("rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError as e:
        return f"[不可编辑] 读取失败：{e}"
    if _is_binary(head):
        return f"[不可编辑] {rel} 看起来是二进制文件（内容含 NUL 字节）"
    return None


def _syntax_check(p: Path, text: str) -> Optional[str]:
    """按扩展名做结构校验。返回错误说明，通过则返回 ``None``。

    目的是把「改坏了」从**污染工作区**降级成**一次失败的尝试**：校验不过就不落盘，
    模型拿到的是「第几行错了、改动已撤销」，而不是一个坏掉的文件等下次编译才发现。
    """
    ext = p.suffix.lower()
    try:
        if ext == ".py":
            import ast

            ast.parse(text)
        elif ext == ".json":
            import json

            json.loads(text)
        elif ext in (".yaml", ".yml"):
            import yaml  # type: ignore[import-untyped]

            yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 任何一种解析异常都算校验失败
        return f"{type(e).__name__}: {e}"
    return None


def _diff_summary(old_text: str, new_text: str) -> str:
    """改动处的 unified diff。

    刻意用标准 diff 而不是「把 new 全列一遍」：后者会把没变的行也显示成 ``+``，
    模型看到的是「我改了 3 行」，实际只加了 1 行——它会据此误判改动范围。
    """
    import difflib

    diff = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="改动前",
            tofile="改动后",
            lineterm="",
            n=1,
        )
    )
    if not diff:
        return ""
    # 去掉 --- / +++ 两行文件头，只留 hunk 标记与增删行
    return "\n".join(diff[2:][:40])


def make_edit_file(root: Path, read_only: bool) -> ProcessorTool:
    """精确替换文件中的一段文本（编辑已有文件）。

    为什么要它：``fs.write_file`` 的 ``content`` 是**全量**的，改 500 行文件里的
    3 行也要把另 497 行逐字重写一遍（几千 token）。编辑只传改动片段，省一个量级。

    它**只保证机械正确**（改的是你指的那处），保证不了语义正确（改完逻辑对不对）。
    后者只能靠执行——所以 description 里钉死了「改完必须运行验证」。

    防呆（都是踩过的坑）：
    - ``old_string`` 不唯一就拒绝，避免改错地方；
    - 原子写（临时文件 + os.replace），不留半截文件；
    - 写完回读校验，确认改动真的落在预期位置；
    - 结构校验（.py/.json/.yaml）不过就**自动回滚**。
    """
    async def run(args: dict) -> str:
        eff_root, eff_ro, tmp_root = _effective(root, read_only)
        rel = args.get("path") or ""
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))
        if not rel:
            return "[缺少参数 path]"
        if old is None or old == "":
            return "[缺少参数 old_string] 必须给出要替换的原文（含缩进与换行，原样照抄）"
        if new is None:
            return "[缺少参数 new_string] 替换后的文本（删除这段就传空字符串）"

        is_tmp = rel.strip().lstrip("/") == TMP_PREFIX or rel.strip().lstrip(
            "/"
        ).startswith(TMP_PREFIX + "/")
        if eff_ro and not is_tmp:
            return "[工作空间为只读，不能改文件]"
        try:
            p = resolve_path(eff_root, tmp_root, rel)
        except PathEscape as e:
            return f"[拒绝] {e}"

        reason = _not_editable_reason(p, rel)
        if reason:
            return reason

        try:
            original = await asyncio.to_thread(p.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            return f"[不可编辑] {rel} 不是 UTF-8 文本"
        except OSError as e:
            return f"[读取失败] {e}"

        if old == new:
            return "[无意义改动] old_string 与 new_string 相同"
        if old.strip() == original.strip() and old != original:
            return (
                "[请用 fs.write_file] old_string 基本等于整个文件，这是整体重写，"
                "用 fs.write_file 一次性写完省 token 也更可靠"
            )

        count = original.count(old)
        if count == 0:
            return _no_match_hint(rel, original, old)
        if count > 1 and not replace_all:
            lines = original.splitlines()
            hits = [
                i + 1
                for i in range(len(lines))
                if old.splitlines()[0] in lines[i]
            ]
            return (
                f"[匹配不唯一] old_string 在 {rel} 里出现 {count} 次"
                f"（首次约在第 {hits[0] if hits else '?'} 行）。"
                "请**扩大上下文**让它唯一：多带上前后几行、完整缩进，"
                "或把整个函数签名包含进来；确认要改所有同名片段时置 replace_all=true。"
            )

        new_text = (
            original.replace(old, new) if replace_all else original.replace(old, new, 1)
        )
        if new_text == original:
            return "[没有变化] 替换后内容与原文相同"

        err = _syntax_check(p, new_text)
        if err:
            return (
                f"[改动已撤销] 结构校验没通过（{p.suffix}）：{err}\n"
                f"{rel} 保持原样，没有被写坏。请修正后重试。"
            )

        # 原子写：临时文件 + os.replace，避免写一半崩掉留下半截文件
        tmp = p.with_name(p.name + ".keeper-edit-tmp")
        try:
            await asyncio.to_thread(tmp.write_text, new_text, encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            return f"[写入失败] {e}"

        # 回读校验：确认改动真的落在预期位置
        try:
            written = await asyncio.to_thread(p.read_text, encoding="utf-8")
        except OSError as e:
            return f"[写入后校验失败] {e}"
        if new not in written:
            return "[写入后校验失败] 磁盘上的内容里找不到 new_string，文件可能未被保存"

        # 「替换块行数」与「净变化」分开报：只说 +3/-2 会被误读成改了 5 行，
        # 实际可能只是把 2 行换成 3 行（净 +1）。模型靠这个数字判断改动范围。
        block_old = len(old.splitlines()) or 1
        block_new = len(new.splitlines()) or 1
        net = len(written.splitlines()) - len(original.splitlines())
        head_line = original[: original.index(old)].count("\n") + 1
        return (
            f"已修改 {rel}（第 {head_line} 行起：{block_old} 行 → {block_new} 行，"
            f"文件净 {'+' if net >= 0 else ''}{net} 行，现共 {len(written.splitlines())} 行"
            + (f"，替换了全部 {count} 处" if replace_all else "")
            + "）\n"
            + _diff_summary(original, new_text)
            + "\n改动位置正确不等于逻辑正确 —— 请用 bash__run 实际运行验证"
            "（跑测试 / 跑一遍看输出），不要用「应该没问题」代替执行。"
        )

    return ProcessorTool(
        name="fs.edit_file",
        description=(
            "编辑**已有**文件：用 new_string 精确替换 old_string，"
            "改几行只需传那几行，不必像 fs.write_file 那样重写整个文件（省 token）。"
            "\n\n【怎么用】"
            "1. 先 fs.read_file 看清当前内容；"
            "2. old_string **原样照抄**（缩进、空行、换行都要一致），"
            "并带上足够上下文使其在文件中唯一；"
            "3. 删除某段就把 new_string 传空字符串。"
            "\n\n【什么时候别用它】"
            "新建文件 → fs.write_file；"
            "整体重构 / 小文件大改（改动超过三成）→ fs.write_file 整体重写更可靠；"
            "改完一定要 fs.publish 挂成产物，否则用户看不到可点开的卡片。"
            "\n\n【改完必须验证】"
            "改完代码后必须调用 bash__run 实际运行（pytest / node test.js / 跑一遍），"
            "以真实输出为准；不许凭空声称「已验证通过」。"
        ),
        parameters={
            "path": "相对工作空间的文件路径（也可用 @tmp/ 前缀指向会话临时目录）",
            "old_string": "要被替换的原文，必须原样包含缩进与换行，且最好足够长以保证唯一",
            "new_string": "替换后的文本；删除这段则传空字符串",
            "replace_all": "可选，默认 false。确认要替换全部同名片段时置 true",
        },
        run=run,
    )


def _no_match_hint(rel: str, original: str, old: str) -> str:
    """匹配不到时给出**可操作**的提示，而不是让模型原地重发。

    实测过一次 30 步打转：工具只回「没找到」，模型拿不到任何线索，就原样重试，
    每一步都失败且理由相同。这里用 difflib 找出最接近的几行并给出行号，
    它就能直接照着修正。
    """
    import difflib

    lines = original.splitlines()
    target = old.strip().splitlines()
    best: List[tuple] = []
    if target:
        # 以 old_string 的首行为锚点找最相似的行
        for i, line in enumerate(lines):
            ratio = difflib.SequenceMatcher(None, target[0].strip(), line.strip()).ratio()
            if ratio >= 0.5:
                best.append((ratio, i + 1))
    best.sort(reverse=True)
    hint = ""
    if best:
        shown = best[:3]
        hint = "\n最接近的几行：" + "；".join(
            f"第 {ln} 行「{lines[ln - 1].strip()[:60]}」" for _, ln in shown
        )
    return (
        f"[没找到匹配] old_string 不在 {rel} 当前内容里。{hint}\n"
        "常见原因：缩进/空行不一致、文件已被改过、或你凭记忆写的原文。"
        "请重新 fs.read_file 拿到当前内容，再原样截取要改的那段（务必包含缩进）。"
    )


def make_find(root: Path, read_only: bool) -> ProcessorTool:
    """按文件名 glob 在工作空间内（含子目录）搜索已存在的文件。

    用于「帮我找 xx 文件」类请求：agent 先定位到具体文件，再交给 ``fs.publish``
    登记为产物展示给用户——而不是把工作空间里所有文件都挂上。
    """
    async def run(args: dict) -> str:
        eff_root, _, tmp_root = _effective(root, read_only)
        pattern = args.get("pattern") or ""
        if not pattern:
            return "[缺少参数 pattern]"
        # 搜索根支持 @tmp/ 前缀：否则模型没法探查自己的临时工作区里有什么
        # （它没有任何别的办法知道那儿有哪些文件）。不传则搜用户工作区。
        base = args.get("path") or "."
        try:
            search_root = resolve_path(eff_root, tmp_root, base)
        except PathEscape as e:
            return f"[拒绝] {e}"
        if not search_root.is_dir():
            return f"[不是目录] {base}"
        matches: list[Path] = []
        scanned = 0
        for p in search_root.rglob("*"):
            scanned += 1
            if scanned > 50_000:  # 防超大目录跑飞
                break
            if p.is_file() and fnmatch.fnmatch(p.name, pattern):
                try:
                    matches.append(p.relative_to(search_root))
                except ValueError:
                    continue
            if len(matches) >= MAX_LIST:
                break
        if not matches:
            return "（未找到匹配文件）"
        matches.sort(key=lambda x: str(x))
        shown = [
            f"{TMP_PREFIX}/{m}" if base.strip().lstrip("/").startswith(TMP_PREFIX) else str(m)
            for m in matches[:MAX_LIST]
        ]
        body = "\n".join(shown)
        if len(matches) >= MAX_LIST:
            body += f"\n...(已截断，前 {MAX_LIST} 个)"
        return body

    return ProcessorTool(
        name="fs.find",
        description=(
            "当用户要你「找某个文件」（如「帮我找录取通知书」「有没有 travel 相关的 pdf」）时，"
            "按文件名 glob 在指定目录内（含子目录）搜索，返回匹配文件的相对路径列表，例如 "
            "pattern='*录取*' 或 '*.pdf'。找到后通常紧接着用 fs.publish 让该文件出现在对话里供用户查看。"
            f"（用 path='{TMP_PREFIX}/' 可以搜会话临时工作区）"
        ),
        parameters={
            "pattern": "文件名 glob 模式，如 '*录取*'、'*.pdf'、'report*.png'",
            "path": (
                f"可选，搜索起点目录。默认 '.'（用户工作区）；"
                f"传 '{TMP_PREFIX}/' 则搜会话临时工作区"
            ),
        },
        read_only=True,  # 只读：可并发
        run=run,
    )


def make_publish(root: Path, read_only: bool) -> ProcessorTool:
    """把工作空间内**已存在**的文件登记为对话产物（不复制 / 不重写）。

    与 ``fs.write_file`` 不同：它不写内容，只是「展示已有文件」。捕获逻辑
    （``service._capture_artifacts``）会扫 ``react_steps`` 里的 ``fs.publish`` 调用，
    把该文件挂成 artifact，从而复用现有预览 / 打开文件夹 / 下载。
    """
    async def run(args: dict) -> str:
        eff_root, _, tmp_root = _effective(root, read_only)
        rel = args.get("path") or ""
        if not rel:
            return "[缺少参数 path]"
        # 临时工作区的文件**不作为产物**（捕获层会跳过它们）。这里必须显式拒绝而不是
        # 默默接受：否则模型以为卡片已经展示给用户了，实际什么都没出现——静默失败
        # 比报错难查得多。要给用户看，就先复制到用户工作区再 publish。
        _r = rel.strip().lstrip("/")
        if _r == TMP_PREFIX or _r.startswith(TMP_PREFIX + "/"):
            return (
                f"[拒绝] {TMP_PREFIX}/ 是会话临时工作区，里面的文件不作为产物展示给用户"
                f"（测试脚本、中间产物这类过程文件本来就不该给用户看）。"
                f"请先用 fs.read_file 读出内容、再用 fs.write_file 写进用户工作区，"
                f"然后对本工具用用户工作区里的那个路径。"
            )
        try:
            p = resolve_path(eff_root, tmp_root, rel)
        except PathEscape as e:
            return f"[拒绝] {e}"
        if not p.is_file():
            return f"[文件不存在] {rel}"
        return f"已登记为产物 {rel}"

    return ProcessorTool(
        name="fs.publish",
        description=(
            "把工作空间里**已经存在**的文件，作为一张产物卡片推到当前对话里，让用户直接在对话中查看。"
            "文件的展示方式由聊天界面按类型自动处理，与你无关：图片 / 文档可预览，代码（.py/.java…）"
            "可内联高亮查看，其余可下载，本机部署还能「打开文件夹」。**任何类型的文件都统一走这个入口**。"
            "它与 fs.read_file 的区别：read_file 是把内容读给你（agent）自己处理、会作为纯文本出现在你的"
            "回答里；本工具是给用户看的展示卡片。一句话——**用户想*看文件*，就用 publish，别用 read_file**。"
            "\n\n"
            "【场景】用户想在对话里直接看到 / 打开工作空间里的某个文件时。例如用户说"
            "「想看看我的个人大头照」「我想看看 add.py」或「把那段代码给我看下」，而历史记忆 / fs.find"
            "指向工作空间里的某个文件（之前常因「无法渲染图片 / 只能读取文本」而没展示成功）——这时就该用它，"
            "**不要**说你无法展示文件，也**不要**用 fs.read_file 把文件内容贴进回答，交给卡片去呈现即可。"
            "\n\n"
            "【怎么用】\n"
            "1) 先确认文件存在：路径已知（来自记忆、之前的 fs.find 返回、或你刚写的文件）就直接用；\n"
            "   不确定文件在哪，先调 fs.find 按文件名 glob 搜（如 `*大头照*`、`*.py`），拿到相对路径；\n"
            "2) 调用本工具，参数 path 填该文件的**相对工作空间路径**（如 `个人大头照.png`、`main.py`）；\n"
            "3) 不要在回复里说「我无法显示图片 / 只能读取文本」这类话——展示由卡片负责。"
            "\n\n"
            "【效果】该文件作为一张产物卡片出现在对话流中；图片 / 文档可预览、代码可内联高亮、"
            "其余可下载，本机部署还能「打开文件夹」。它**不写内容、不复制文件**，只登记已有文件的展示入口。"
        ),
        parameters={"path": "相对用户工作区的文件路径（只能是用户工作区里的文件）"},
        run=run,
    )
