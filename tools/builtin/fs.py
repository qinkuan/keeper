"""文件系统类内置工具：列目录 / 读文件 / 写文件。

所有操作都限制在 agent 的**工作空间**内（见 ``sandbox.safe_path``）。
写类工具（``fs.write_file``）在只读工作空间下运行时返回拒绝。

工厂签名统一为 ``make_xxx(root, read_only) -> ProcessorTool``：
``root`` / ``read_only`` 是**兜底值**（装配期传入的 agent 默认工作目录），
运行时优先取当前请求的工作空间（``chat.context.current_workspace``），
从而支持「同一 agent 在不同会话指向不同用户空间」。
"""
from __future__ import annotations

import ast
import asyncio
import fnmatch
import heapq
import json
import os
from pathlib import Path
from typing import List, Optional

from ..base import ProcessorTool
from .sandbox import (
    TMP_PREFIX,
    USER_PREFIX,
    SESSION_PREFIX,
    PathEscape,
    resolve_path,
    is_scratch,
    classify_prefix,
)
from ...viewer import parse_blocks, block_file_ref, validate_chart_spec

# 单次读取的最大字节数：只取文件头部，避免大文件把内存撑爆 / 阻塞事件循环
# （约对应 20 万 ASCII 字符，或约 6 万汉字；超出部分截断）。
MAX_READ_BYTES = 200_000
# 单次列出的最大条目数
MAX_LIST = 200

# 单次 fs.write_file 的字符上限（超出请分段，见 make_write_file 的描述）。
#
# 为什么设上限：LLM 是**逐字生成**的，正文越长越容易在输出里跑飞。实测
# deepseek-flash 一次生成 9.8k 字符的 HTML 后，接着自编了 OBSERVATION 又写了
# 一个 FINAL——整轮作废（见 planner.detect_hallucination）。长内容还会被
# max_tokens 截断，而截断后的 JSON 常常**勉强闭合**，写出来的是残缺文件却
# 照样回「已写入」。
#
# 不是硬性技术限制，调大只是多烧点 token；调小则强制走「分段写 + 拼接」那条
# 已经写进工具描述的路子。6000 是在「一次生成质量」与「少走弯路」之间取的折中。
_MAX_SINGLE_WRITE = 6_000


def _integrity_hint(rel: str, content: str) -> str:
    """写完立刻粗检一遍内容完整性；没问题返回空串。

    为什么必须做：这层存在的意义是**把静默失败变成信号**。工具此前只回
    「已写入 N 字符」——那是**操作结果**，不是**达成情况**。模型据此理所当然
    认为文件写好了，然后在 FINAL 里向用户断言「点开就能跑」，而文件可能
    是个缺尾标签的残片。这个差异对纯文本无所谓，对「产物卡片」是致命的。

    判定全部是启发式，只在**明显可疑**时出声，宁可漏报也不误报——误报会让
    模型反复去「修」一个本来正确的文件。

    覆盖不到的情况也说明白：内容在 JSON 里被截断时，闭合标签/括号/引号
    往往仍然配对齐全（模型会在中途收尾），这种「完整但不完整」检不出来，
    只能靠上面的长度上限拦住。
    """
    name = rel.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    tail = content.rstrip()
    if not tail:
        return "⚠️ 内容是空的。"

    if ext in ("html", "htm"):
        if not tail.lower().endswith("</html>"):
            return "⚠️ HTML 没有 </html> 结束标签，文件很可能被截断了。"
        if tail.lower().count("<script") != tail.lower().count("</script>"):
            return "⚠️ <script> 与 </script> 数量不匹配，文件很可能被截断了。"
    elif ext == "json":
        try:
            json.loads(content)
        except Exception as e:  # noqa: BLE001
            return f"⚠️ 内容不是合法 JSON：{e}"
    elif ext == "py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            return f"⚠️ 内容不是合法 Python：{e}"
    elif ext in ("js", "jsx", "ts", "tsx", "css", "less", "scss"):
        for open_c, close_c in (("{", "}"), ("(", ")"), ("[", "]")):
            if content.count(open_c) != content.count(close_c):
                return (
                    f"⚠️ {open_c}{close_c} 数量不配对（{content.count(open_c)} vs "
                    f"{content.count(close_c)}），文件很可能被截断了。"
                )
    elif ext in ("xml", "svg", "html.j2"):
        if not tail.endswith(">"):
            return "⚠️ XML/SVG 没有以 > 结束，文件很可能被截断了。"
    return ""


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
            "（「看看 / 看下 X 文件」「把 X 给我看下」），请用 **fs.deliver** 交付文件清单，"
            "而不要用本工具把文件内容直接贴进你的回答——那样既无高亮也不美观。"
        ),
        parameters={"path": f"相对用户工作区的文件路径；{TMP_PREFIX}/ 前缀表示会话临时工作区"},
        read_only=True,  # 只读：可并发
        run=run,
    )


def _write_too_long(rel: str, n: int) -> str:
    """超长单次写入的回执。**不写文件**——写一半比不写更糟。

    为什么要硬拦：LLM 逐字生成，正文越长越容易在输出里跑飞或被 max_tokens
    截断，而截断后的 JSON 常常勉强闭合，结果是「写成功了」但内容残缺。
    与其事后靠完整性自检去发现，不如一开始就不让它写这么大。
    """
    return (
        f"[拒绝写入] {rel} 单次 {n} 字符，超过上限 {_MAX_SINGLE_WRITE}。\n"
        "长内容在生成途中被截断时，会**静默写坏**——写出来看着成功、其实缺尾巴。\n"
        "请分段：\n"
        "  1. 分几次用 fs.write_file 写 @tmp/part1.txt、@tmp/part2.txt…"
        "（每次一两千字符）；\n"
        "  2. 用一条 bash 命令（cat / 顺序拼接）把它们合成目标文件；\n"
        "  3. 对目标文件调用 fs.deliver 交付（列入交付清单）。\n"
        "（@tmp/ 下的分片文件不受此限制。）"
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
        "3. 对那个目标文件调用 fs.deliver 交付，用户才能看到可点开的产物卡片。"
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
        prefix = classify_prefix(rel)
        scratch = prefix == "tmp"
        in_session = prefix in ("session", "tmp")
        if eff_ro and prefix in ("user", "default"):
            return "[工作空间为只读，不能写文件]"
        if len(content) > _MAX_SINGLE_WRITE:
            return _write_too_long(rel, len(content))
        try:
            p = resolve_path(eff_root, tmp_root, rel)
        except PathEscape as e:
            return f"[拒绝] {e}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        hint = _integrity_hint(rel, content)
        # 会话工作区（@session/ @tmp）要给真实绝对路径：``bash__run`` 这类插件不认前缀
        # （它起的是真子进程，没有我们的路径解析），agent 只能拿绝对路径去
        # ``cd`` / 传参。写完就告诉它一次，省得它把脚本又写回用户工作区。
        if in_session:
            return (
                f"已写入 {rel}（{len(content)} 字符），"
                f"实际路径 {p}（这是会话工作区，bash 命令里请用这个绝对路径）"
            ) + (f"\n{hint}" if hint else "")
        return f"已写入 {rel}（{len(content)} 字符）" + (f"\n{hint}" if hint else "")

    return ProcessorTool(
        name="fs.write_file",
        description=(
            "把内容写入文件（父目录自动创建）。路径用前缀区分工作区："
            "`@user/`（用户工作区，只读时不可写）、`@session/`（会话工作区，始终可写，"
            "用于交付给用户的成品）、`@tmp/`（会话内的临时草稿，每轮清空、不交付）。"
            "没有前缀时按用户工作区处理。"
            "随手测试脚本、分片、中间产物请用 `@tmp/`（如 `@tmp/check.py`）；"
            "要交付的成品写到 `@session/`（如 `@session/report.md`），最后用 fs.deliver 交付。"
            "\n\n【content 长度限制 —— 超过会被直接拒绝、文件不会被创建】"
            "content 是要**由你逐字生成**并放进 JSON 的，"
            f"所以单次上限是 {_MAX_SINGLE_WRITE} 字符。"
            "超过时本工具返回「拒绝写入」——那不是路径写错，是内容太长，别去改路径。\n"
            "为什么要卡：正文越长越容易在生成途中被截断或跑飞，"
            "而截断后的 JSON 常常**勉强闭合**，结果是「写成功了」但内容缺尾巴。\n"
            "长文件必须**分段写**：先用本工具分几次写 `@tmp/part1.txt`、"
            "`part2.txt`…（每次一两千字符），再用一条 bash 命令把它们"
            "按顺序拼成目标文件，最后对目标文件调用 fs.deliver 交付。"
            "（`@tmp/` 下的分片文件本身不受此限制。）"
            "\n\n【写完会自动体检】写入后会按扩展名粗检一遍内容完整性"
            "（HTML 是否闭合、JSON/Python 能否解析、括号是否配对），"
            "有问题会在回执里附上 ⚠️ 提示——看到它就说明文件可能被截断了，"
            "请改用分段写重来。"
        ),
        parameters={
            "path": (
                "文件路径。可用前缀：@user/（用户工作区）、@session/（会话工作区，交付成品）、"
                "@tmp/（临时草稿）；无前缀按用户工作区处理，如 `@session/report.md`"
            ),
            "content": (
                f"要写入的文本。**单次上限 {_MAX_SINGLE_WRITE} 字符**；"
                "更长请分段写入多个 @tmp/partN.txt 后用 bash 合并"
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
        return f"[文件不存在] {rel} —— 新建文件请用 bash 写（如 cat > file），编辑只针对已有文件"
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

    为什么要它：整体重写（``bash`` 整体写）是**全量**的，改 500 行文件里的
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

        prefix = classify_prefix(rel)
        if eff_ro and prefix in ("user", "default"):
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
                "[请用 bash 整体写] old_string 基本等于整个文件，这是整体重写，"
                "用 bash 一次性写完省 token 也更可靠"
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
            "改几行只需传那几行，不必像整体重写那样重写整个文件（省 token）。"
            "\n\n【怎么用】"
            "1. 先用 bash（cat 等）看清当前内容；"
            "2. old_string **原样照抄**（缩进、空行、换行都要一致），"
            "并带上足够上下文使其在文件中唯一；"
            "3. 删除某段就把 new_string 传空字符串。"
            "\n\n【什么时候别用它】"
            "新建文件 → 用 bash 写（如 ``cat > file``）；"
            "整体重构 / 小文件大改（改动超过三成）→ 用 bash 整体重写更可靠；"
            "改完若要把结果交付给用户，收尾时用一次 fs.deliver([...]) 列出**最终文件**（只在交付最终结果时调用，中间文件不要交付）。"
            "\n\n【改完必须验证】"
            "改完代码后必须调用 bash__run 实际运行（pytest / node test.js / 跑一遍），"
            "以真实输出为准；不许凭空声称「已验证通过」。"
        ),
        parameters={
            "path": "文件路径，支持 @user/（用户工作区）、@session/（会话工作区）、@tmp/ 前缀（无前缀按用户工作区）",
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
        "请重新用 bash（cat 等）拿到当前内容，再原样截取要改的那段（务必包含缩进）。"
    )


def make_find(root: Path, read_only: bool) -> ProcessorTool:
    """按文件名 glob 在工作空间内（含子目录）搜索已存在的文件。

    用于「帮我找 xx 文件」类请求：agent 先定位到具体文件，再交给 ``fs.deliver``
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
        base_s = base.strip().lstrip("/")
        prefix_out = (
            base_s.rstrip("/") + "/"
            if base_s.startswith(
                (USER_PREFIX + "/", SESSION_PREFIX + "/", TMP_PREFIX + "/", TMP_PREFIX)
            )
            else ""
        )
        shown = [prefix_out + str(m) for m in matches[:MAX_LIST]]
        body = "\n".join(shown)
        if len(matches) >= MAX_LIST:
            body += f"\n...(已截断，前 {MAX_LIST} 个)"
        return body

    return ProcessorTool(
        name="fs.find",
        description=(
            "当用户要你「找某个文件」（如「帮我找录取通知书」「有没有 travel 相关的 pdf」）时，"
            "按文件名 glob 在指定目录内（含子目录）搜索，返回匹配文件的相对路径列表，例如 "
            "pattern='*录取*' 或 '*.pdf'。找到后通常紧接着用 fs.deliver 把该文件列入交付清单，出现在对话里供用户查看。"
            f"（path 支持 @user/、@session/、@tmp/ 前缀，分别搜用户工作区、会话工作区、临时草稿）"
        ),
        parameters={
            "pattern": "文件名 glob 模式，如 '*录取*'、'*.pdf'、'report*.png'",
            "path": (
                "可选，搜索起点目录。默认 '.'（用户工作区）；"
                "传 '@user/'、'@session/' 或 '@tmp/' 指定对应工作区"
            ),
        },
        read_only=True,  # 只读：可并发
        run=run,
    )


def make_deliver(root: Path, read_only: bool) -> ProcessorTool:
    """把工作空间内**已存在**的文件（或结构化内容），作为本次对话的交付清单推给用户
    （不复制 / 不重写）。

    它不写内容（文件用 ``bash`` 生成），只是「声明本次交付了哪些块」。捕获逻辑
    （``service._capture_artifacts``）会扫 ``react_steps`` 里的 ``fs.deliver`` 调用，
    把每个块挂成 artifact，前端按块的 ``kind`` 选渲染器（图片预览、HTML 沙箱、代码高亮、
    图表、表格、流程图…）。

    这是文件 / 多模态交付的**唯一正式入口**：agent 用 ``bash`` 把内容写进工作空间后，
    末尾用一次 ``fs.deliver([...])`` 列出要交付的**块**即可，不必为单个文件反复调用。
    """
    async def run(args: dict) -> str:
        eff_root, _, tmp_root = _effective(root, read_only)
        blocks = parse_blocks(args)
        if not blocks:
            return (
                "[缺少有效交付] 请传入块列表 blocks，如 "
                "[{\"kind\":\"file\",\"path\":\"report.html\"}]；"
                "兼容旧写法 paths=[\"report.html\"]"
            )
        ok, skip = [], []
        for b in blocks:
            rel = block_file_ref(b)
            if rel is None:
                # 内联 mermaid 等无文件块：src 已齐，直接通过
                if b.get("src"):
                    ok.append(b)
                else:
                    skip.append(f"{b.get('kind')}（既无 file 也无 src）")
                continue
            rel = rel.strip().strip("/")
            if not rel or is_scratch(rel):
                skip.append(f"{rel}（临时目录，不作为交付）")
                continue
            try:
                p = resolve_path(eff_root, tmp_root, rel)
            except PathEscape as e:
                skip.append(f"{rel}（越界：{e}）")
                continue
            if not p.is_file():
                skip.append(f"{rel}（文件不存在）")
                continue
            # 体检：file 块校验内容完整性；chart 块校验 spec 可解析
            if b["kind"] == "file":
                try:
                    body = p.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    skip.append(f"{rel}（读取失败：{e}）")
                    continue
                hint = _integrity_hint(rel, body)
                if hint:
                    skip.append(f"{rel}（{hint}）")
                    continue
            elif b["kind"] == "chart":
                try:
                    body = p.read_text(encoding="utf-8")
                except OSError as e:
                    skip.append(f"{rel}（读取失败：{e}）")
                    continue
                err = validate_chart_spec(body, b.get("engine") or "vega-lite")
                if err:
                    skip.append(f"{rel}（{err}）")
                    continue
            ok.append(b)
        if not ok:
            detail = "；".join(skip) if skip else "清单为空"
            return f"[无有效交付] {detail}"
        tail = f"；跳过 {len(skip)} 个：{'，'.join(skip)}" if skip else ""
        summary = ", ".join(f"{b['kind']}:{block_file_ref(b) or 'inline'}" for b in ok)
        return f"已交付 {len(ok)} 个块：{summary}{tail}"

    return ProcessorTool(
        name="fs.deliver",
        description=(
            "**唯一正式交付入口**——把要交付给用户的「块」用一次 `fs.deliver(blocks=[...])` 列出，前端按 kind 渲染成可交互内容：\n"
            "- 文件 `{\"kind\":\"file\",\"path\":\"x\",\"render\":\"preview|run|inline|folder\"}`（render 不写按扩展名默认：图/视频/PDF→打开，HTML→沙箱，代码/文档→内联）；\n"
            "- 图表 `{\"kind\":\"chart\",\"file\":\"x.vg.json\",\"engine\":\"vega-lite\"}`；流程图 `{\"kind\":\"mermaid\",\"src\":\"...\"}` 或 `{\"kind\":\"mermaid\",\"file\":\"x.mmd\"}`；表格 `{\"kind\":\"table\",\"file\":\"d.csv\"}`；地图 `{\"kind\":\"map\",\"file\":\"m.geojson\"}`；音频 `{\"kind\":\"audio\",\"file\":\"a.mp3\"}`。兼容旧写法 `paths=[\"x\"]`。\n"
            "- 先 `bash` 把内容写好（cat > / npm run build 等）；重内容（spec、数据）写进文件，块只写「怎么渲染」几行 JSON，别把巨型内容塞进参数。\n"
            "- **交付给用户看**才用本工具；自己分析文件用 `bash`（cat / less）即可。\n"
            "- bash 生成的文件不会自动上卡片：写完必须显式交付，否则用户只听到「文件已生成」却点不开。\n"
            "- 只交最终成品；草稿 / 临时 / 中间产物（测试脚本、分片、构建缓存）不要交——会被跳过。要交付 bash 写出的成品，`path` 用 `@session/` 前缀（如 `@session/report.md`）。\n"
        ),
        parameters={
            "blocks": "要交付的块列表；每个块是带 kind 的字典（见上方写法），或兼容地传 paths/路径字符串列表",
        },
        run=run,
    )



