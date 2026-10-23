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

还有一类是「**命令名能进、但参数要逐条查**」的
--------------------------------------------------
`sed` / `awk` / `test`：命令本身只读，参数却能写盘或执行
（`sed -i`、`sed 'w out'`、GNU sed 的 `e`、`awk 'BEGIN{system(...)}'`、
`awk 'print > "f"'`）。这类命令放行——挡掉它们 agent 只会退回用 grep 反复试，
实测一条评测用例因此从 5 步烧到 22 步——但参数按 `_READONLY_EXTRA_CHECKS`
逐条查。

已知漏判（写在这里而不是假装没有）
----------------------------------
- ``$VAR``、反引号、``eval``：需要真正的 shell 解析器，收益不抵成本。
- 可写工作区**不做命令白名单**（否则会拦掉正常的 git/npm 工作流），只查路径越界。
- **只读区禁一切写入，但拦不到绕过工具的写入**：code 子 agent 的沙箱是「独立
  venv + 同权限子进程」，它 Python 自己 ``open(path, 'w')`` 没人管。守卫已经接到
  子 agent 的 ``kt.call`` 桥上，但那条路只覆盖「工具调用」。
  这**不是漏洞，是产品决策**：只读约束的威胁模型是「第三方插件乱写用户文件」，
  而 code 子 agent 跑的是大模型自己写的代码，所以它被显式标记为可写
  （``keeper/agent/delegatee.py`` 的 ``Delegatee.writable``），守卫对它按可写区
  处理。**只放开「能不能写工作区」，路径越界照样拦**——委派桥与主链路共用同一套
  越界检查。
所以本模块是「护栏」，不是「围栏」：挡误操作与越界，不替代人工确认。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

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
# 命令分隔符：切片段用。**引号内不切**（见 _segments 的说明），
# 所以这里只作参考，真正的切分在 _segments 里逐字符做。
_SPLIT = re.compile(r"\|\||&&|;|\n|\|")

# ~ 开头 / .. 开头的路径逃逸
_ESCAPE = re.compile(r"(?:^|[\s'\"=:(])(~[^\s'\"]*|\.\.[/\\][^\s'\"]*)")

# 绝对路径：必须是「以 / 开头、且整段都是路径合法字符（字母/数字/_ . - /）」的 token。
# 这样不会把代码 / 正则 / sed 里的斜杠误判成越界路径——像 node -e 的
# /const CELL = (\d+)/、sed 的 s|/old|/new|，那些 / 要么不在 token 开头，要么
# 夹杂空格、( ) = 等非路径字符。真正越界的绝对路径（/etc/passwd、/Users/...）
# 总是独立成一个 clean 的 token。
_ABS_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")

_URLISH = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

# here-doc 操作符：<< 或 <<-，后面跟可选空白与定界符（可加引号）
_HEREDOC_OP = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*|[^'\"\s]+)['\"]?")

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
    # 条件判断：只读、且不执行任何东西（test / [ 是同一个内建）
    "test": None, "[": None,
    # 文本处理：默认只读，但各自有写盘 / 执行的门（-i / w / e、system()…），
    # 由 _READONLY_EXTRA_CHECKS 逐条查
    "sed": None, "awk": None,
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

# 只读区里「命令本身只读、但参数能写盘 / 能执行」的少数命令：
# **命令名放行，参数逐条查**。
#
# 为什么要放行
# ------------
# 这些命令**默认不写文件**（sed 不带 -i、awk 不重定向、test 只做判断）。
# 把它们挡在白名单外，agent 就只能退回去用 grep 反复试——实测一条评测用例
# 因为 ``test -f x`` 被拦，从 5 步变成 22 步（预算 8 步），token 翻了 2.7 倍。
# 被拦一次就要换姿势重试，这个代价远大于命令本身的写风险。
#
# 为什么必须查参数
# ----------------
# 它们各自都有暗门：
# - ``sed -i`` 原地改文件；``sed 'w out'`` 落盘；GNU sed 的 ``e`` 命令能执行 shell
# - ``awk 'BEGIN{system("...")}'`` 能执行任意命令；``awk 'print > "f"'`` 能落盘
#
# 查不准的写法一律拒绝：宁可误杀，不可放过。
_READONLY_EXTRA_CHECKS: Dict[str, Tuple[re.Pattern, str]] = {
    "sed": (
        re.compile(
            # -i / -i.bak / -ni / --in-place / --in-place=.bak
            r"(?:^|\s)(?:-[a-zA-Z]*i(?:\.[^\s]*)?|--in-place(?:\s*=\s*\S+)?)(?:\s|$)"
            # w 命令：s/a/b/w out.txt —— 落盘。目标必须含词字符，避免把 s/w /x/ 误杀
            r"|\bw\s+(?:[\"']?[\w./-]*\w[\w./-]*[\"']?)"
            # e 命令（GNU）：s/a/b/e —— 执行模式空间。分隔符 / ; { } 之后才算独立命令
            r"|(?:^|[;{}/])\s*e(?:\s|$|;|[\"'])",
            re.IGNORECASE,
        ),
        "`sed` 的 -i / w / e 能改文件或执行命令，只读环境已禁用",
    ),
    "awk": (
        re.compile(
            # BEGIN{system("...")} —— 执行任意命令
            r"system\s*\("
            # cmd | getline —— 起子进程
            r"|\bgetline\b"
            # print > "文件" —— 落盘（目标必须是字符串字面量，避免误杀 $1 > 5 这类比较）
            r"|\bprint\b[^;\n]*>\s*[\"'][^\"']+[\"']",
            re.IGNORECASE,
        ),
        "`awk` 的 system() / getline / 输出重定向能执行命令或写文件，只读环境已禁用",
    ),
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

# 解释器的「版本探测」：只打印版本号，没有副作用，语义上等同 `ls`。
# 装插件时经常需要先确认运行时版本（`node -v`），把它整个当"执行任意代码"拦掉
# 会让 agent 反复绕路。**只有参数全是版本开关**时才当读操作——带上脚本、
# `-c`、`-e` 一律照旧拒绝。
_VERSION_FLAGS = frozenset({"--version", "-V"})
# `-v` 在不同解释器里含义不同：node/deno/bun 是版本号，python 是 verbose（会执行
# 脚本），所以短横小写只对前者开放。
_VERSION_SHORT_V = frozenset({"node", "nodejs", "deno", "bun"})


def _is_version_probe(cmd: str, rest: List[str]) -> bool:
    """这次调用是不是「只问版本」的解释器调用（``node -v`` / ``python3 --version``）。"""
    if cmd not in INTERPRETERS or not rest:
        return False
    flags = {_unwrap(w) for w in rest}
    if not flags <= (_VERSION_FLAGS | {"-v"}):
        return False
    if "-v" in flags and cmd not in _VERSION_SHORT_V:
        return False
    return True


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

    **引号内不切**（为什么要认引号）
    ------------------------------
    ``awk '{print $1}'`` 里的 ``;``、``grep 'a|b'`` 里的 ``|`` 都不是命令分隔符。
    不认引号就会把正则 / 脚本切成碎片，再把碎片当命令名去查白名单——
    实测 ``grep -E 'foo|bar' a.txt`` 在只读区被判成「``bar'`` 不在白名单」而拦下，
    ``awk '{print $1}'`` 同理。agent 被拦一次就得换姿势重试，代价远超误判本身。

    反过来，正因为引号内的内容不再被切开，``awk 'BEGIN{system("...")}'`` 会作为
    **一整段**进入判定——所以只读区必须靠 _READONLY_EXTRA_CHECKS 查它的参数。
    """
    out: List[str] = []
    cur: List[str] = []
    quote = ""      # 当前所在的引号（' / " / `），空 = 不在引号里
    i = 0
    n = len(text or "")
    while i < n:
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                # 双引号里 \" 是转义，不算结束引号
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            # 转义字符：\; \/ \| 都不是分隔符
            cur.append(ch)
            cur.append(text[i + 1])
            i += 2
            continue
        two = text[i:i + 2]
        if two in ("||", "&&"):
            out.append("".join(cur))
            cur = []
            i += 2
            continue
        if ch in ";|\n":
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


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


def _strip_heredocs(text: str) -> str:
    """删除 here-document 的正文，只留命令骨架。

    守卫不做真正的 shell 解析，而 here-doc 的正文是**文件内容**（HTML / 脚本源码），
    里面会出现 ``</html>``、``/usr/bin/env`` 这种看着像路径、其实只是数据的片段。
    不剥掉它们，就会被当成越界路径误杀，导致「用 heredoc 写整个文件」被整条拒绝。
    """
    if "<<" not in text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC_OP.search(line)
        if not m:
            out.append(line)
            i += 1
            continue
        # 只保留 << 操作符之前的部分（命令 + 重定向目标），正文整段丢弃
        out.append(line[: m.start()].rstrip())
        delim = m.group(1)
        i += 1
        while i < len(lines) and lines[i].strip() != delim:
            i += 1
        if i < len(lines):
            i += 1  # 跳过 delimiter 本身
    return "\n".join(out)


def _quoted_positions(text: str) -> set:
    """返回被引号包住的位置集合（引号本身也算在内）。

    用途：判断某个 ``>`` / ``|`` 是不是**shell 语法**。引号里的 ``>`` 永远不是
    重定向（shell 里 ``'>'`` 是文件名的一部分），引号里的 ``|`` 永远不是管道。
    不认引号就会把 ``awk '$1 > 5'`` 的比较当成写文件、把 ``grep 'a|b'`` 的正则
    当成管道。
    """
    pos: set = set()
    quote = ""
    i = 0
    n = len(text or "")
    while i < n:
        ch = text[i]
        if quote:
            pos.add(i)
            if ch == "\\" and quote == '"' and i + 1 < n:
                pos.add(i + 1)
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            pos.add(i)
            i += 1
            continue
        i += 1
    return pos


def _tokens(text: str) -> List[Tuple[str, bool]]:
    """把命令文本切成 token：``(内容, 是否被引号包住)``。

    引号感知是必须的：``grep 'a|b'`` 的正则、``awk '{print $1}'`` 的脚本里都有
    ``|`` / ``;``，按空白和 ``|`` 硬切会把一个 token 切成两半，碎片再被当成
    命令名 / 路径去判定——于是 ``grep -E 'foo|bar'`` 被判成「``bar'`` 不在白名单」。
    """
    out: List[Tuple[str, bool]] = []
    cur = ""
    quoted = False
    started = False
    quote = ""
    i = 0
    n = len(text or "")
    while i < n:
        ch = text[i]
        if quote:
            cur += ch
            if ch == "\\" and quote == '"' and i + 1 < n:
                cur += text[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            quoted = True
            started = True
            cur += ch
            i += 1
            continue
        if ch in " \t\n\r,;|&":
            if started:
                out.append((cur, quoted))
            cur = ""
            quoted = False
            started = False
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            cur += ch + text[i + 1]
            started = True
            i += 2
            continue
        cur += ch
        started = True
        i += 1
    if started:
        out.append((cur, quoted))
    return out


def find_escapes(text: str) -> List[str]:
    """找出指向工作空间之外的路径：``~`` 开头、``..`` 开头、绝对路径。

    **相对路径一律不管**：插件工具的 ``cwd`` 在插件目录而非工作空间，判它越界会
    误杀正常操作。这是本守卫最主要的漏判来源，写在这里而不是假装没有。

    ``..`` 必须后跟分隔符才算逃逸，否则 ``...tail -c 200 f``（shell 里字符串
    截取的省略写法）会被当成 ``../`` 穿越而误杀。

    这里只扫**命令本身**：先剥掉 here-document 的正文（那是文件内容，不是参数），
    再跳过含 ``<`` / ``>`` 的片段（HTML / XML 标签，如 ``</body>`` 会被切成
    ``/body`` 误判成越界绝对路径）。否则「用 heredoc 写一整个 HTML 文件」这种
    正常操作会被整条命令拒绝。
    """
    text = _strip_heredocs(text)
    hits: List[str] = []
    # 注意：这里**不**按 ( ) 断词。路径 token 本就由空白 / 重定向分隔；按 ( ) 断
    # 只会把内联代码（如 node -e 的 (\d+)/.exec(s)[1];）碎成「/.exec」这种看着像
    # 干净绝对路径的假片段，造成误杀。$(...) 这类本就是守卫解析不了的已知盲区。
    for raw, _quoted in _tokens(text):
        t = _unwrap(raw)
        if not t or _URLISH.match(t):
            continue
        if "<" in t or ">" in t:
            continue  # HTML / XML 标签片段不是路径
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


def _redirect_matches(seg: str) -> List[re.Match]:
    """片段里**不在引号内**的输出重定向（``>`` / ``>>``）。

    引号里的 ``>`` 永远不是重定向：``awk '$1 > 5'`` 是比较、``echo "a > b"`` 是
    字符串——shell 里只有**未被引用**的 ``>`` 才是重定向。不认引号就会把
    awk 的比较当成写文件而整条拦下。
    """
    q = _quoted_positions(seg)
    return [m for m in _REDIRECT.finditer(seg) if m.start() not in q]


def _redirect_targets(seg: str, matches: Optional[List[re.Match]] = None) -> List[str]:
    """取出片段里所有输出重定向（> / >>）后面的目标 token。"""
    out: List[str] = []
    for m in (matches if matches is not None else _redirect_matches(seg)):
        rest = seg[m.end():]
        mm = re.match(r"\s*([^\s;&|]+)", rest)
        if mm:
            out.append(mm.group(1))
    return out


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
    # 解释器的「版本探测」是纯读操作（只打印版本号，无副作用）：装插件时经常要
    # 先确认运行时版本（`node -v`）。仅当参数**只有**版本开关时才按读命令放行。
    version_probe = _is_version_probe(cmd, rest)

    # 1) 解释器与执行包装：任何情况下都拒绝。
    #    `python -c "..."` / `xargs rm` / `sudo rm` 的命令名看着无害，
    #    但它们能执行任意代码——放进白名单等于洞开着。
    if cmd in INTERPRETERS:
        if not version_probe:
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
        if read_only and not version_probe:
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

    # 3.5) 「命令只读、参数能写 / 能执行」的那几个：命令名放行，参数逐条查。
    # 详见 _READONLY_EXTRA_CHECKS 的说明（sed -i / w / e、awk system() / getline）。
    extra = _READONLY_EXTRA_CHECKS.get(cmd)
    if extra is not None:
        pat, reason = extra
        if pat.search(seg):
            return reason

    # 4) 输出重定向：落文件就是写。只读区里**一律禁**——包括指向工作区内部的
    #    绝对路径。
    #
    #    ⚠️ 以前有个例外：「重定向目标是工作空间 / 会话草稿区内部就放行」。
    #    那是**可写区**的思路，被留在只读区的判定里就成了洞：只读工作区里
    #    ``cat /abs/workspace/data.csv > /abs/workspace/data_copy.csv`` 直接
    #    写成了文件（评测里 `read-only-write-refused` 就这样红过一次）。
    #    只读就是只读：写入一律禁，唯一例外是丢输出（2>/dev/null）。
    #
    #    引号里的 > 不算重定向（awk 的比较、echo 的字符串）。
    redirects = _redirect_matches(seg)
    if read_only and redirects:
        for tgt in _redirect_targets(seg, redirects):
            if tgt.rstrip("/") in SAFE_DEVICE_PATHS or tgt.startswith("/dev/fd/"):
                continue  # 丢弃输出，不落盘
            return (
                f"`{cmd}` 带了输出重定向（> / >>），会写文件，只读环境已禁用"
                f"（要写文件请让用户提供可写工作区）"
            )
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
    # 先剥掉 here-document 正文：那是文件内容（HTML / 脚本），不是命令参数，
    # 否则正文里的 </html> 会被当路径、脚本行会被当成命令片段误判。
    text = _strip_heredocs(text)

    # 合法根：用户工作区 + 会话私有草稿区（extra_roots）。两者内部都算「沙箱内」，
    # 路径越界检查与只读重定向豁免都基于这套根。
    roots: List[Path] = [workspace] if workspace is not None else []
    roots += [r for r in (extra_roots or []) if r is not None]

    # 路径越界：两种模式都查。PATH_EXEMPT 里的命令豁免（包管理器 / VCS 天生要
    # 访问工作空间之外，对它们查路径没有意义）。
    cmds = {
        Path(_unwrap(w)).name
        for seg in _segments(text)
        for w in [(_unwrap(seg.split()[0]) if seg.split() else "")]
        if w
    }
    if not (cmds & PATH_EXEMPT):
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
                f"再用 ② 临时工作区的**真实绝对路径**（已在「工作区」说明里给出）在命令里引用它"
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
