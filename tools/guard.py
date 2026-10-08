"""危险工具调用的守卫：只读工作区的**读命令白名单** + 路径越界拦截。

为什么需要它
------------
``read_only`` 过去只约束内置 ``fs.*`` 工具。插件工具（MCP / bin）完全在这条约束
之外——它们的参数里根本没有「工作空间」这个概念。代价是实测出来的（见
``doc/eval-design.md`` 首份基线）：只读工作区里，agent 用 ``bash__run cp`` 写出了
文件、用 ``bash__run grep /etc/hosts`` 把系统文件读了进来。

只看**参数文本**，所以 MCP / bin / 内置工具一视同仁。

白名单为什么不能用「命令名」粒度
--------------------------------
黑名单（列出危险命令）永远补不完：``find -delete``、``xargs rm``、``python -c``、
``awk 'BEGIN{system("...")}'``…… 所以反过来做**白名单**，且必须「命令 + 子命令」
两级：

- ``git status`` 放行、``git push`` 拒绝（按子命令）
- ``pip list`` 放行、``pip install`` 在只读区拒绝（按子命令）

两个绝不能进白名单的东西
--------------------------
1. **解释器**：``python -c`` / ``node -e`` / ``perl -e`` 能做任何事，一律拒绝。
2. **自带执行能力的工具**：`find -exec rm {} ;`、`ls | xargs rm`——命令名在白
   名单里，实际在删文件。所以 ``xargs`` 不进白名单，``find`` 进但禁掉它的
   ``-exec`` / ``-delete`` / ``-ok``。

已知漏判（写在这里而不是假装没有）
----------------------------------
- ``$VAR``、反引号、``eval``：需要真正的 shell 解析器，收益不抵成本。
- 可写工作区**不做命令白名单**（否则会拦掉正常的 git/npm 工作流），只查路径越界。
所以本模块是「护栏」，不是「围栏」：挡误操作与越界，不替代人工确认。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set

# 传进去就能执行任意代码的东西，永远不进白名单（``xargs rm`` 是经典手法）
EXEC_WRAPPERS: FrozenSet[str] = frozenset({
    "xargs", "sudo", "su", "doas", "nohup", "timeout", "watch",
    "docker", "kubectl", "helm",
})

# find 自带的执行 / 删除 / 落盘能力：即使 find 在白名单，这些标志也禁用
FORBIDDEN_FLAGS = (
    "-exec", "-execdir", "-delete", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
)

# 输出重定向：落文件 = 写操作。只读区禁止
_REDIRECT = re.compile(r"(?<![0-9>&])>>?(?![&0-9])")

# 命令分隔符：把复合命令切开逐条看
_SPLIT = re.compile(r"\|\||&&|;|\n|\|")

# ~ 开头 / .. 开头的路径逃逸
_ESCAPE = re.compile(r"(?:^|[\s'\"=:(])(~[^\s'\"]*|\.\.[/\\][^\s'\"]*)")

# 绝对路径。排除 URL 的 //
_ABS_PATH = re.compile(r"(?<!:)//[^\s'\"]*|(?<![\w:/])/[^\s'\"]*")

_URLISH = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

# 无害的系统设备文件：``2>/dev/null`` / ``> /dev/null`` 是 shell 里最常见的写法，
# 把它们当「工作空间之外的路径」拦掉是纯粹的误判——而且这个误判代价很大：
# agent 写完临时脚本想 ``find`` 一下配套文件就会被连带拒绝，挫败之下干脆放弃
# ``@tmp/`` 改把脚本堆回用户工作区（实测踩过：``find ... 2>/dev/null`` 被拒 →
# 模型改写工作区 → 临时脚本变成「产物」挂到会话里）。
#
# 只放行**设备节点**本身，不放行 ``/dev`` 下的普通路径，也不放行 ``/tmp``。
SAFE_DEVICE_PATHS: FrozenSet[str] = frozenset({
    "/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/tty", "/dev/random", "/dev/urandom", "/dev/fd",
})

# ---- 读命令白名单：命令名 -> 允许的子命令集合；None 表示整体都算读 ----
SAFE_COMMANDS: Dict[str, Optional[Set[str]]] = {
    # 文件查看
    "ls": None, "cat": None, "head": None, "tail": None, "less": None,
    "more": None, "wc": None, "file": None, "stat": None, "du": None,
    "df": None, "tree": None, "basename": None, "dirname": None,
    "realpath": None, "readlink": None, "pwd": None, "which": None,
    "type": None, "whereis": None, "date": None, "whoami": None, "id": None,
    "uname": None, "hostname": None, "uptime": None, "ps": None,
    "env": None, "printenv": None, "who": None,
    # echo 单独看着无害，但 `echo x > file` / `echo x | sh` 是常见的落盘与执行
    # 手法。它在白名单里，同时由「重定向」与「管道到非白名单命令」两条规则兜住。
    "echo": None,
    # 文本处理（纯读）
    "grep": None, "egrep": None, "fgrep": None, "rg": None, "ag": None,
    "diff": None, "cmp": None, "sort": None, "uniq": None, "cut": None,
    "tr": None, "nl": None, "column": None, "strings": None, "jq": None,
    "yq": None, "md5sum": None, "sha256sum": None,
    # find 进白名单，但 -exec/-delete/-ok 由 FORBIDDEN_FLAGS 单独禁
    "find": None, "fd": None,
    # 包管理器：只列只读子命令。装包是写操作，只读区拒绝
    "pip": {"list", "show", "freeze", "search", "check", "help", "config"},
    "pip3": {"list", "show", "freeze", "search", "check", "help", "config"},
    "npm": {"list", "ls", "outdated", "view", "info", "search", "help", "why"},
    "yarn": {"list", "why", "info", "help"},
    "pnpm": {"list", "ls", "outdated", "why", "help"},
    "uv": {"list", "show", "tree", "help"},
    # 版本控制：只列只读子命令。clone/commit/push 一律不在白名单
    "git": {
        "status", "log", "diff", "show", "branch", "remote", "ls-files",
        "ls-remote", "describe", "rev-parse", "blame", "shortlog", "tag",
        "config", "help", "whatchanged", "grep", "cat-file", "annotate",
    },
    "hg": {"log", "status", "diff", "show"},
    "svn": {"log", "status", "diff", "info", "list"},
}

# 解释器：一律拒绝，哪怕命令名看起来无害。``python -c`` 能做任何事。
INTERPRETERS: FrozenSet[str] = frozenset({
    "python", "python2", "python3", "node", "nodejs", "deno", "bun",
    "perl", "ruby", "php", "lua", "tclsh", "wish", "sh", "bash", "zsh",
    "ksh", "csh", "fish", "pwsh", "powershell", "osascript",
})

# 路径越界的**豁免**名单：这些命令天生要访问工作空间之外（包管理器缓存、
# VCS 远程、构建产物），对它们查路径没有意义。
#
# 刻意**不放** curl / wget：``curl file:///etc/hosts`` 和 ``curl -d @/etc/passwd``
# 都能读系统文件，放进来等于把这个洞重新打开。
PATH_EXEMPT: FrozenSet[str] = frozenset({
    "pip", "pip3", "npm", "npx", "yarn", "pnpm", "uv", "gem", "cargo",
    "go", "mvn", "gradle", "git", "hg", "svn", "ssh", "scp", "rsync",
})


# ---------------------------------------------------------------- 文本处理


def _iter_strings(obj: Any):
    """递归取出所有字符串叶子。

    只取**值**、不取参数名：参数名（MCP 工具里叫 ``command`` / ``cmd`` / ``script``
    都有可能）不参与命令解析。把整个 args JSON 化再扫是错的——切出来的第一个
    「命令」会是 ``{"command":`` 这种结构碎片，结果是所有命令一律被拒。
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _iter_strings(v)
    elif obj is not None and not isinstance(obj, bool):
        yield str(obj)


def stringify_args(args: Optional[Dict[str, Any]]) -> str:
    """把参数摊平成一段可扫描的文本（只取字符串叶子）。"""
    if not args:
        return ""
    return "\n".join(_iter_strings(args))


def _unwrap(tok: str) -> str:
    t = tok.strip()
    while len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        t = t[1:-1]
    return t


def _segments(text: str) -> List[str]:
    """按命令分隔符（``;`` ``&&`` ``||`` ``|`` 换行）把命令文本切成片段。

    必须切：``cd /tmp && rm -rf x`` 里每一条都要独立判定，否则只看第一个词
    会把 ``rm`` 放过去。
    """
    return [s.strip() for s in _SPLIT.split(text or "") if s.strip()]


def _head_words(seg: str) -> List[str]:
    """取片段开头的命令词，跳过 ``VAR=value`` 前缀赋值。"""
    out: List[str] = []
    for w in (x for x in re.split(r"\s+", seg) if x):
        u = _unwrap(w)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", u):
            continue  # 环境变量前缀
        out.append(Path(u).name)
        break
    return out


def find_escapes(text: str) -> List[str]:
    """找出指向工作空间之外的路径：``~`` 开头、``..`` 开头、绝对路径。

    **相对路径一律不管**：插件工具的 ``cwd`` 在插件目录而非工作空间，判它越界会
    误杀正常操作。这是本守卫最主要的漏判来源，写在这里而不是假装没有。

    ``..`` 必须后跟分隔符才算逃逸，否则 ``...tail -c 200 f``（shell 里字符串
    截取的省略写法）会被当成 ``../`` 穿越而误杀。
    """
    hits: List[str] = []
    for raw in re.split(r"[\s,;|&()<>]+", text or ""):
        t = _unwrap(raw)
        if not t or _URLISH.match(t):
            continue
        if t.startswith("-") or t.startswith("%") or t.startswith("$"):
            continue  # 选项名 / printf 格式串 / 变量（变量是已知漏判）
        if t.startswith("..") and (len(t) == 2 or t[2] in "/\\"):
            hits.append(t)
            continue
        m = _ESCAPE.search(t) or _ABS_PATH.search(t)
        if m:
            tok = m.group(1) if m.lastindex else m.group(0)
            # 无害设备文件不算越界（``/dev/null``、``/dev/fd/3`` 这类）
            if tok.rstrip("/") in SAFE_DEVICE_PATHS or tok.startswith("/dev/fd/"):
                continue
            hits.append(tok)
    return hits


def _norm(p: str) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except Exception:  # noqa: BLE001
        return p


def is_inside(path: str, root: Optional[Path]) -> bool:
    """``path`` 是否在 ``root`` 内（含 root 自身）。``root`` 未知时一律 True。"""
    if root is None:
        return True
    try:
        r = Path(_norm(str(root)))
        p = Path(_norm(path))
    except Exception:  # noqa: BLE001
        return True
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- 判定


def _judge_segment(seg: str, read_only: bool) -> Optional[str]:
    """判定单个命令片段；返回拒绝原因，放行返回 ``None``。"""
    words = [w for w in re.split(r"\s+", seg) if w]
    if not words:
        return None
    # 跳过环境变量赋值前缀
    idx = 0
    while idx < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", _unwrap(words[idx])):
        idx += 1
    if idx >= len(words):
        return None
    cmd = Path(_unwrap(words[idx])).name
    rest = words[idx + 1:]

    # 1) 解释器与执行包装：任何情况下都拒绝。
    #    `python -c "..."` / `xargs rm` / `sudo rm` 的命令名看着无害，
    #    但它们能执行任意代码——放进白名单等于洞开着。
    if cmd in INTERPRETERS:
        return f"`{cmd}` 是解释器，能执行任意代码，不在只读环境的允许范围内"
    if cmd in EXEC_WRAPPERS:
        return f"`{cmd}` 能把参数交给别的程序执行（`{cmd} rm` 这类手法），已禁用"

    # 2) find 自带的执行/删除/落盘能力
    for w in rest:
        u = _unwrap(w)
        for bad in FORBIDDEN_FLAGS:
            if u == bad or u.startswith(bad + " ") or (
                bad in ("-fprint", "-fprintf", "-fls") and u.startswith(bad)
            ):
                return f"`{cmd}` 的 `{u}` 能执行命令或写文件，只读环境已禁用"

    # 3) 白名单检查
    if cmd not in SAFE_COMMANDS:
        if read_only:
            return (
                f"`{cmd}` 不在只读环境的读命令白名单内"
                f"（允许的是 ls / cat / grep / find / git status / pip list 这类）"
            )
        return None  # 可写区不拦非常规命令，否则正常开发流程全废

    allowed = SAFE_COMMANDS[cmd]
    if allowed is not None:
        sub = next((_unwrap(w) for w in rest if not _unwrap(w).startswith("-")), "")
        sub = Path(sub).name if sub else ""
        if sub not in allowed:
            shown = f"`{cmd} {sub}`" if sub else f"`{cmd}`"
            return (
                f"{shown} 不是只读操作（该命令允许的子命令：{'、'.join(sorted(allowed))}）"
            )

    # 4) 输出重定向：落文件就是写
    if read_only and _REDIRECT.search(seg):
        return f"`{cmd}` 带了输出重定向（> / >>），会写文件，只读环境已禁用"
    return None


def check_call(
    *,
    tool_name: str,
    args: Optional[Dict[str, Any]],
    dangerous: bool,
    read_only: bool,
    workspace: Optional[Path],
    extra_roots: Optional[List[Path]] = None,
) -> Optional[str]:
    """执行前判断这次调用要不要拦。返回拒绝原因（直接给 LLM 看），放行返回 ``None``。

    ``dangerous`` 由工具自己声明（见 ``ProcessorTool.dangerous``）：只有能执行命令
    或以非常规方式触达文件的工具才需要过这道闸。

    只读区用**白名单**（只放行确认无副作用的读命令），可写区只查**路径越界**——
    不查命令白名单，否则 git/npm 的正常流程会被全部拦下。

    ``extra_roots``：工作区**之外**额外合法的根，目前只放会话私有临时目录
    （``workspace/session/<sid>/<aid>``）。它让 agent 能把测试脚本写进自己的草稿区，
    而不是堆在用户工作区。它是**追加**的白名单，不是放开整个文件系统——别的外部
    路径照样拦。
    """
    if not dangerous:
        return None

    text = stringify_args(args)

    # 路径越界：两种模式都查。PATH_EXEMPT 里的命令豁免（包管理器 / VCS 天生要
    # 访问工作空间之外，对它们查路径没有意义）。
    cmds = {
        Path(_unwrap(w)).name
        for seg in _segments(text)
        for w in [(_unwrap(seg.split()[0]) if seg.split() else "")]
        if w
    }
    if not (cmds & PATH_EXEMPT):
        roots: List[Path] = [workspace] if workspace is not None else []
        roots += [r for r in (extra_roots or []) if r is not None]
        outside = [
            p
            for p in find_escapes(text)
            if not any(is_inside(p, r) for r in roots)
        ]
        if outside:
            shown = "、".join(sorted(set(outside))[:3])
            return (
                f"命令里含有指向工作空间之外的路径：{shown}。"
                f"工作空间是沙箱，读写都只应发生在它内部。"
                f"要写临时文件请用 `@tmp/` 前缀（如 `@tmp/check.py`），"
                f"再用 fs.write_file 回执里给出的**真实绝对路径**在命令里引用它"
                f"（bash 不认 `@tmp/` 这个前缀，只认绝对路径）；"
                f"不要用 `..`、也不要拿工作空间或临时目录的父目录去定位文件。"
            )

    if not read_only:
        return None

    for seg in _segments(text):
        reason = _judge_segment(seg, read_only=True)
        if reason:
            return f"只读工作空间下拒绝执行：{reason}"
    return None
