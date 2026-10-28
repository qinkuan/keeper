#!/usr/bin/env python3
"""plugin-installer 的可执行组件：从 GitHub / 直链安装、卸载、列出 keeper 插件。

协议（keeper 插件 bin 约定）：
- stdin 读**一行** JSON：{"tool": "...", "args": {...}, "context": {...}}
- stdout **最后一行**打 JSON：{"ok": true, "result": "..."} / {"ok": false, "error": "..."}
- 日志一律走 stderr，stdout 只留最终 JSON。

刻意只用标准库、不 import keeper：插件该能独立跑（单独执行、别的机器上跑）。
格式判定（keeper-plugin.json / .claude-plugin/plugin.json）与
``keeper/plugin/formats.py`` 保持一致——那边是唯一真源，这里镜像一份。

安全口径（这个脚本的存在意义就是它）：
- 只往插件库（context.plugin_root，默认 ~/.keeper/plugins）里写；
- 解压防穿越：拒绝绝对路径、``..`` 逃逸、逃逸的链接、设备文件；
- 不静默覆盖：同名已装时必须 force=true，且目标长得不像插件就拒绝替换；
- 装完自动补 bin/ 与 mcp/ 下文件的可执行位（zip 归档会丢权限）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import urllib.error
import urllib.request

MARKER_KEEPER = "keeper-plugin.json"
MARKER_CLAUDE = ".claude-plugin/plugin.json"
MARKERS = (MARKER_KEEPER, MARKER_CLAUDE)

# 上限：归档下载 / 解压后体积 / 文件数。装个插件不该拖走半个磁盘。
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_FILES = 20000
DOWNLOAD_TIMEOUT = 120

USER_AGENT = "keeper-plugin-installer/1.0"


class UserError(Exception):
    """给模型/用户看的错误（ok=false 返回），不是脚本 bug。"""


def log(msg):
    print(msg, file=sys.stderr)


# ── 来源解析 ──────────────────────────────────────────────────────────────

_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.*))?$")


def _split_ref_subdir(rest):
    """``owner/repo@v1//sub/dir`` → (repo_part, ref, subdir)。"""
    subdir = None
    if "//" in rest:
        rest, subdir = rest.split("//", 1)
        subdir = subdir.strip("/")
    ref = None
    if "@" in rest:
        rest, ref = rest.split("@", 1)
    return rest.strip(), (ref.strip() or None) if ref is not None else None, subdir or None


def parse_source(src, ref_arg=None, subdir_arg=None, registry=None):
    """把用户给的来源归一成 {kind, url, ref, subdir, display, …}。

    支持：
      npm:<pkg>[@version]                                （npm 技能包，自动包壳）
      owner/repo[@ref][//subdir]
      https://github.com/owner/repo(.git)[@ref][//subdir]
      git@github.com:owner/repo.git[@ref][//subdir]
      https://github.com/owner/repo/tree/<ref>/<path>   （浏览器地址栏粘来的）
      http(s)://…/*.tar.gz|.tgz|.tar|.zip                （直链归档）
    """
    s = (src or "").strip()
    if not s:
        raise UserError("source 不能为空")

    # npm 包：``npm:dashi-ppt-skill@latest``（用户说"npx 装个 skill"时指这个）
    if s.lower().startswith("npm:"):
        spec = s[len("npm:"):].strip()
        if not spec:
            raise UserError("npm: 后面要跟包名，例如 npm:dashi-ppt-skill@latest")
        if spec.startswith("@"):  # 作用域包 @scope/name@version：按最后一个 @ 切
            pkg, sep, ver = spec.rpartition("@")
            if not sep:
                pkg, ver = spec, ""
        else:
            pkg, _, ver = spec.partition("@")
        pkg = pkg.strip()
        if not pkg:
            raise UserError("npm 包名不能为空：%s" % s)
        ver = (ver.strip() or (ref_arg or "").strip()) or None
        return {
            "kind": "npm",
            "pkg": pkg,
            "version": ver,
            "registry": (registry or "").strip() or None,
            "display": "npm:%s%s" % (pkg, ("@" + ver) if ver else ""),
        }

    # 直链归档：http(s) 且不是 github 仓库地址
    if s.lower().startswith(("http://", "https://")) and "github.com" not in s.lower():
        if not s.lower().split("?")[0].endswith((".tar.gz", ".tgz", ".tar", ".zip")):
            raise UserError(
                "直链只支持 .tar.gz/.tgz/.tar/.zip 归档；"
                "GitHub 仓库请用 owner/repo 简写（如 openai/plugin）；"
                "npm 包请写 npm:<包名>"
            )
        return {
            "kind": "archive",
            "url": s,
            "ref": ref_arg,
            "subdir": subdir_arg,
            "display": s,
        }

    # GitHub：先统一成 owner/repo[...] 形式
    t = s
    if t.lower().startswith("git@github.com:"):
        t = t[len("git@github.com:"):]
    else:
        t = re.sub(r"^https?://github\.com/", "", t, flags=re.I)
    t = t.strip("/")
    if t.lower().endswith(".git"):
        t = t[: -len(".git")]

    # 浏览器粘来的 /tree/<ref>/<path>
    m = _TREE_RE.search(s) or _TREE_RE.search("github.com/" + t)
    if m:
        owner, repo, ref, path = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        if path.endswith(".git"):
            path = path[: -len(".git")]
        return {
            "kind": "github",
            "url": "https://codeload.github.com/%s/%s/tar.gz/%s"
            % (owner, repo, ref),
            "ref": ref,
            "subdir": path.strip("/") or subdir_arg,
            "display": "github.com/%s/%s@%s%s"
            % (owner, repo, ref, ("/" + path) if path else ""),
        }

    repo_part, ref, subdir = _split_ref_subdir(t)
    if ref_arg:
        ref = ref_arg
    if subdir_arg:
        subdir = subdir_arg
    parts = [p for p in repo_part.split("/") if p]
    if len(parts) != 2:
        raise UserError(
            "认不出这个来源：%s。支持 owner/repo[@ref][//子目录]、"
            "GitHub 完整 URL、或 .tar.gz/.zip 直链" % s
        )
    owner, repo = parts
    ref = ref or "HEAD"
    return {
        "kind": "github",
        "url": "https://codeload.github.com/%s/%s/tar.gz/%s" % (owner, repo, ref),
        "ref": ref,
        "subdir": subdir,
        "display": "github.com/%s/%s@%s%s"
        % (owner, repo, ref, ("/" + subdir) if subdir else ""),
    }


# ── 下载与解压 ────────────────────────────────────────────────────────────


def download(url, dest_file):
    log("下载 %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UserError(
                "下载 404：%s。ref 可能不存在（分支/tag 名打错？私有仓库？）" % url
            )
        raise UserError("下载失败 HTTP %s：%s" % (e.code, url))
    except urllib.error.URLError as e:
        raise UserError("下载失败（网络？）：%s（%s）" % (url, e.reason))
    except OSError as e:
        raise UserError("下载失败：%s（%s）" % (url, e))
    with resp, open(dest_file, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise UserError(
                    "归档超过 %.0fMB 上限，拒绝下载" % (MAX_ARCHIVE_BYTES / 1048576)
                )
            f.write(chunk)
    if total == 0:
        raise UserError("下载到空文件：%s" % url)
    log("下载完成 %.1f KB" % (total / 1024))


def _is_within(child, parent):
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False


def _check_member_name(name):
    """归档成员名校验：不许绝对路径、不许 ``..`` 穿越。"""
    if name.startswith("/") or name.startswith("\\"):
        raise UserError("归档内含绝对路径成员：%s" % name)
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        raise UserError("归档内含路径穿越成员：%s" % name)
    return name


def _apply_caps(total_bytes, files):
    if files > MAX_FILES:
        raise UserError("归档内文件数超过 %d，拒绝解压" % MAX_FILES)
    if total_bytes > MAX_PACKAGE_BYTES:
        raise UserError(
            "解压后体积超过 %.0fMB 上限，拒绝安装" % (MAX_PACKAGE_BYTES / 1048576)
        )


def extract_tar(archive, dest):
    dest = Path(dest)
    with tarfile.open(archive, "r:*") as tf:
        members = []
        total, files = 0, 0
        for m in tf.getmembers():
            _check_member_name(m.name)
            target = (dest / m.name).resolve()
            if not _is_within(target, dest.resolve()):
                raise UserError("归档成员解压位置越界：%s" % m.name)
            if m.issym() or m.islnk():
                link = (target.parent / m.linkname).resolve()
                if not _is_within(link, dest.resolve()):
                    raise UserError("归档内含逃逸链接：%s -> %s" % (m.name, m.linkname))
            elif not (m.isfile() or m.isdir()):
                raise UserError("归档内含不支持的文件类型：%s" % m.name)
            if m.isfile():
                files += 1
                total += m.size
                _apply_caps(total, files)
            members.append(m)
        # 3.12+ 支持 filter="data"（再挡一层 setuid / 异常元数据）；
        # 老 Python 没有这个参数，退回默认（上面的手工校验已经做了能做的事）。
        try:
            tf.extractall(str(dest), members=members, filter="data")
        except TypeError:
            tf.extractall(str(dest), members=members)
    log("解压完成：%d 个文件，%.1f KB" % (files, total / 1024))


def extract_zip(archive, dest):
    dest = Path(dest)
    with zipfile.ZipFile(archive) as z:
        total, files = 0, 0
        for info in z.infolist():
            _check_member_name(info.filename)
            if info.is_dir():
                continue
            files += 1
            total += info.file_size
            _apply_caps(total, files)
        z.extractall(str(dest))
    log("解压完成：%d 个文件，%.1f KB" % (files, total / 1024))


def extract(archive, dest):
    if archive.lower().endswith(".zip"):
        extract_zip(archive, dest)
    else:
        extract_tar(archive, dest)


def fetch_npm(parsed, workdir, extract_dir):
    """``npm pack`` 到临时区再解包。npm 随 node 走，本机没有就明确报错。"""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise UserError(
            "本机找不到 npm（node 环境没装？），无法安装 npm 来源的插件。"
            "GitHub / 直链来源不受影响"
        )
    spec = parsed["pkg"] + ("@" + parsed["version"] if parsed["version"] else "")
    cmd = [npm, "pack", spec, "--pack-dest", workdir]
    if parsed.get("registry"):
        cmd.append("--registry=" + parsed["registry"])
    log("npm pack %s" % spec)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise UserError("npm pack 超时（%ds）" % DOWNLOAD_TIMEOUT)
    except OSError as e:
        raise UserError("npm pack 执行失败：%s" % e)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise UserError("npm pack 失败：%s" % " / ".join(tail))
    tgzs = sorted(Path(workdir).glob("*.tgz"))
    if not tgzs:
        raise UserError("npm pack 没有产出 .tgz（%s）" % workdir)
    extract(str(tgzs[0]), extract_dir)


def find_skill_file(pkg):
    """在散装包里找 SKILL.md：包根 > skill/ > skills/<name>/ > skills/。"""
    cands = [
        Path(pkg) / "SKILL.md",
        Path(pkg) / "skill.md",
        Path(pkg) / "skill" / "SKILL.md",
        Path(pkg) / "skill" / "skill.md",
    ]
    skills_dir = Path(pkg) / "skills"
    if skills_dir.is_dir():
        for sub in sorted(skills_dir.iterdir()):
            if sub.is_dir():
                for n in ("SKILL.md", "skill.md"):
                    if (sub / n).is_file():
                        cands.append(sub / n)
        for n in ("SKILL.md", "skill.md"):
            if (skills_dir / n).is_file():
                cands.append(skills_dir / n)
    for c in cands:
        if c.is_file():
            return c
    return None


def auto_shell(pkg):
    """给散装技能包生成 ``keeper-plugin.json`` 壳（npm 技能包的常见形态）。

    只**加一份清单**，不动包内任何文件——SKILL.md 里的相对引用
    （scripts/、project/、references/）全靠原样保留才继续有效。清单的
    ``skill`` 字段指向找到的 SKILL.md（keeper 支持清单指向任意位置）。
    """
    pkg = Path(pkg)
    skill = find_skill_file(pkg)
    if skill is None:
        raise UserError(
            "这个 npm 包里找不到 SKILL.md（找过包根、skill/、skills/ 下），"
            "不像一个技能包，拒绝安装"
        )
    pj = {}
    pj_path = pkg / "package.json"
    if pj_path.is_file():
        try:
            pj = json.loads(pj_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 package.json 坏不致命，用目录名兜底
            pj = {}
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(pj.get("name") or pkg.name)).strip("-.")
    name = name or pkg.name
    rel = skill.relative_to(pkg).as_posix()
    manifest = {
        "name": name,
        "version": str(pj.get("version") or "").strip(),
        "description": str(pj.get("description") or "").strip()
        or "由 npm 包 %s 转换而来的 keeper 插件" % name,
        "skill": rel,
    }
    (pkg / MARKER_KEEPER).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log("散装技能包已自动生成 keeper-plugin.json（name=%s, skill=%s）" % (name, rel))
    return pkg


# ── 包识别 / 清单（镜像 keeper.plugin.formats，刻意不 import 内核）─────────


def has_marker(pkg):
    return any((Path(pkg) / m).is_file() for m in MARKERS)


def package_format(pkg):
    if (Path(pkg) / MARKER_KEEPER).is_file():
        return "keeper"
    if (Path(pkg) / MARKER_CLAUDE).is_file():
        return "claude"
    return ""


def read_manifest(pkg):
    """返回 (manifest, error)。认得出的格式才读；认不出返回 ({}, '')。"""
    fmt = package_format(pkg)
    if not fmt:
        return {}, ""
    path = Path(pkg) / (MARKER_KEEPER if fmt == "keeper" else MARKER_CLAUDE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {}, "清单解析失败（%s）：%s" % (path, e)
    if not isinstance(data, dict):
        return {}, "清单必须是 JSON 对象（%s）" % path
    return data, ""


def kinds_of(manifest):
    out = []
    if manifest.get("bin"):
        out.append("bin")
    if manifest.get("mcpServers"):
        out.append("mcp")
    if not out and (
        manifest.get("skill") or manifest.get("skills") or manifest.get("name")
    ):
        out.append("skill")
    return out


def resolve_package(root, subdir=None):
    """在解压目录里定位插件包根：subdir 指定 > 根有标志 > 唯一子层有标志。"""
    cand = Path(root) / subdir if subdir else Path(root)
    if has_marker(cand):
        return cand
    if not cand.is_dir():
        raise UserError("来源里没有这个子目录：%s" % (subdir or "."))
    subs = [
        p
        for p in sorted(cand.iterdir())
        if p.is_dir() and not p.name.startswith(".") and has_marker(p)
    ]
    if len(subs) == 1:
        return subs[0]  # GitHub tarball 的 repo-ref/ 根目录
    if not subs:
        raise UserError(
            "在这个来源里找不到插件包根（需要 %s 或 %s）"
            % (MARKER_KEEPER, MARKER_CLAUDE)
        )
    raise UserError(
        "这个来源里有多个插件包（%s），请用 //子目录 指明要装哪个，"
        "例如 owner/repo//plugins/pdf-tools" % "、".join(p.name for p in subs[:5])
    )


def package_summary(pkg, manifest, fmt):
    files, total = [], 0
    for p in sorted(Path(pkg).rglob("*")):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:  # noqa: BLE001
                continue
            if len(files) < 40:
                files.append(str(p.relative_to(pkg)))
    name = str(manifest.get("name") or "").strip()
    return {
        "name": name or Path(pkg).name,
        "version": str(manifest.get("version") or "").strip(),
        "description": str(manifest.get("description") or "").strip(),
        "format": fmt,
        "kinds": kinds_of(manifest),
        "files": files,
        "file_count": len(files) if len(files) < 40 else "40+",
        "size_kb": round(total / 1024, 1),
    }


# ── 插件库操作 ────────────────────────────────────────────────────────────


def _keeper_home():
    """keeper 数据根：默认 ``~/.keeper``，可用 ``KEEPER_HOME`` 覆盖（便携安装 / 测试）。"""
    env = os.environ.get("KEEPER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".keeper"


def plugin_root(ctx):
    """插件库根目录：优先上下文给的（内核装配期传真路径），否则按 KEEPER_HOME 兜底。"""
    root = (ctx or {}).get("plugin_root") or ""
    if not root:
        root = _keeper_home() / "plugins"
    return Path(root).expanduser()


def _safe_name(name):
    n = (name or "").strip()
    if not n or "/" in n or "\\" in n or n in (".", ".."):
        raise UserError("插件名 %r 不合法（不能为空、不能带路径分隔符）" % name)
    return n


def _agent_link_dirs():
    base = _keeper_home() / "agents"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def _fetch_to_tmp(src, ref_arg, subdir_arg, workdir, registry=None):
    """下载 + 解压 + 定位包根，返回 (pkg, manifest, fmt, parsed, shelled)。

    ``shelled`` = 是不是给散装包自动生了 keeper-plugin.json 壳（npm 技能包）。
    """
    parsed = parse_source(src, ref_arg, subdir_arg, registry)
    extract_dir = os.path.join(workdir, "extract")
    shelled = False
    if parsed["kind"] == "npm":
        fetch_npm(parsed, workdir, extract_dir)
        # npm tarball 约定解出 package/；没有标志就按散装技能包包壳
        root = Path(extract_dir) / "package"
        if not root.is_dir():
            root = Path(extract_dir)
        if parsed.get("subdir"):
            root = root / parsed["subdir"]
            if not root.is_dir():
                raise UserError("npm 包里没有这个子目录：%s" % parsed["subdir"])
        pkg = root
        if not has_marker(pkg):
            pkg = auto_shell(pkg)
            shelled = True
    else:
        archive = os.path.join(workdir, "pkg.archive")
        download(parsed["url"], archive)
        extract(archive, extract_dir)
        pkg = resolve_package(extract_dir, parsed.get("subdir"))
    fmt = package_format(pkg)
    if not fmt:
        raise UserError(
            "这个包没有 %s 也没有 %s，不像一个 keeper 插件，拒绝安装"
            % (MARKER_KEEPER, MARKER_CLAUDE)
        )
    manifest, err = read_manifest(pkg)
    if err:
        raise UserError(err)
    if not str(manifest.get("name") or "").strip():
        raise UserError("清单里没有 name 字段，无法确定插件名")
    return pkg, manifest, fmt, parsed, shelled


def _fix_exec_bits(pkg):
    """zip 归档会丢可执行位；装完把 bin/ 与 mcp/ 下的直接文件补上 +x。"""
    fixed = []
    for sub in ("bin", "mcp"):
        d = Path(pkg) / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and not os.access(f, os.X_OK):
                try:
                    f.chmod(f.stat().st_mode | 0o755)
                    fixed.append(str(f.relative_to(pkg)))
                except OSError as e:  # noqa: BLE001
                    log("补可执行位失败 %s：%s" % (f, e))
    return fixed


# ── Windows 兼容：给无后缀的 Python 脚本补 .py 后缀 ──────────────────────
#
# 为什么需要这一步
# ----------------
# Windows 上**没有 shebang**：内核起子进程走 CreateProcess，只认真可执行文件。
# 内核现在会按「后缀 → shebang」选解释器（framework/runtime.py 的
# script_kind），所以无后缀脚本其实也起得来——但那依赖内核能读到文件头，
# 且不同内核版本行为可能不同。把包**规范化**成带后缀，是让包在任何环境下
# 都不依赖这些前提的稳妥做法（也让清单里的路径与文件一一对应）。
#
# 只动**清单引用到的**命令
# ----------------------
# 不扫目录无脑改名：包里没被清单引用的文件，用户可能自己要用，也可能被插件内部
# 脚本互相调用——改名会打断那些引用。只处理清单里 bin[].command 与
# mcpServers[].command 指向的文件。
#
# 同步改掉包内文档里的路径引用
# --------------------------
# 模型是照着 SKILL.md / README 里写的路径去找文件的。文档还写着 bin/xxx 而文件
# 已经叫 bin/xxx.py，模型就会扑空——所以文档里的引用一并改写。
#
# node 脚本不用改
# --------------
# 内核认 .js / .mjs / .cjs，也会读 shebang 里的 node——所以 node 脚本原样
# 保留，只在结果里提一句「这个包需要 node 运行时」。

_SHEBANG_PY = re.compile(rb"^#!\s*/usr/bin/env\s+python[\d.]*\b", re.I)
_SHEBANG_NODE = re.compile(rb"^#!\s*/usr/bin/env\s+node\b", re.I)

# Windows 上内核能直接执行的（真 PE / 批处理），不用动
_WIN_EXEC_EXTS = {".exe", ".com", ".cmd", ".bat"}


def _script_kind(path):
    """看文件头判断它是不是脚本：'python' / 'node' / ''（二进制或不是脚本）。"""
    try:
        with open(path, "rb") as f:
            head = f.readline(200)
    except OSError:
        return ""
    if _SHEBANG_PY.match(head):
        return "python"
    if _SHEBANG_NODE.match(head):
        return "node"
    return ""


def _rel_command_path(command):
    """从 command 字符串里取出包内相对路径。

    清单里通常写 ``${PLUGIN_ROOT}/bin/xxx``；也有的把参数直接跟在后面
    （``${PLUGIN_ROOT}/bin/xxx --flag``）。返回 (相对路径, 余下的参数部分)。
    """
    s = (command or "").strip()
    if not s:
        return "", ""
    s = s.replace("${PLUGIN_ROOT}/", "").replace("${PLUGIN_ROOT}", "")
    parts = s.split(None, 1)
    rel = parts[0] if parts else ""
    rest = (" " + parts[1]) if len(parts) > 1 else ""
    return rel, rest


def _manifest_commands(manifest):
    """清单里所有会被当命令执行的条目：bin[].command 与 mcpServers[].command。"""
    out = []
    for b in manifest.get("bin") or []:
        if isinstance(b, dict) and b.get("command"):
            out.append(("bin", b))
    for name, m in (manifest.get("mcpServers") or {}).items():
        if isinstance(m, dict) and m.get("command"):
            out.append(("mcp", m))
    return out


def _scan_script_commands(pkg):
    """扫清单里引用的命令，返回 [(相对路径, 'python'/'node')]。

    只认**无已知可执行后缀**且文件头有 shebang 的——那是脚本；
    有 .exe / .cmd 的是真可执行文件，.py 的本来就对，都不动。
    """
    pkg = Path(pkg)
    manifest, err = read_manifest(pkg)
    if err or not manifest:
        return []
    found = []
    seen = set()
    for _kind, entry in _manifest_commands(manifest):
        rel, _rest = _rel_command_path(entry.get("command"))
        if not rel or rel in seen:
            continue
        if rel.startswith("/") or ".." in rel.split("/"):
            continue
        target = pkg / rel
        if not target.is_file():
            continue
        ext = target.suffix.lower()
        if ext in _WIN_EXEC_EXTS or ext in (".py", ".pyw"):
            continue
        k = _script_kind(target)
        if k:
            seen.add(rel)
            found.append((rel, k))
    return found


def _write_manifest(pkg, manifest):
    """按包的格式把清单写回去（保持 2 空格缩进的风格）。"""
    fmt = package_format(pkg)
    if not fmt:
        return
    path = Path(pkg) / (MARKER_KEEPER if fmt == "keeper" else MARKER_CLAUDE)
    try:
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as e:  # noqa: BLE001
        log("清单回写失败 %s：%s" % (path, e))


def _rewrite_doc_refs(pkg, rename_map):
    """把包内文档（*.md）里提到的旧路径改成新路径。

    用占位符做两段替换：直接 replace 会把 "bin/xxx" 在 "bin/xxx.py" 里再匹配一次，
    变成 "bin/xxx.py.py"。正则则额外守住边界——前面不能是单词字符（避免 "mybin/xxx"
    被误伤），后面不能紧跟 ".py" 或单词字符。
    """
    changed = []
    for md in sorted(Path(pkg).rglob("*.md")):
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = text
        for i, old in enumerate(rename_map):
            pattern = r"(?<![\w.-])" + re.escape(old) + r"(?!\.py\b)(?!\w)"
            new = re.sub(pattern, "\x00%d\x00" % i, new)
        for i, new_rel in enumerate(rename_map.values()):
            new = new.replace("\x00%d\x00" % i, new_rel)
        if new != text:
            try:
                md.write_text(new, encoding="utf-8")
                changed.append(str(md.relative_to(pkg)))
            except OSError as e:  # noqa: BLE001
                log("文档改写失败 %s：%s" % (md, e))
    return changed


def _compat_windows_scripts(pkg):
    """给清单里引用的无后缀 Python 脚本补 .py 后缀，并同步改写清单与文档。

    返回 (renamed: [(旧相对路径, 新相对路径)], node_scripts: [相对路径])。
    """
    pkg = Path(pkg)
    manifest, err = read_manifest(pkg)
    if err or not manifest:
        return [], []
    renamed = []
    node_scripts = []
    for _kind, entry in _manifest_commands(manifest):
        rel, rest = _rel_command_path(entry.get("command"))
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        target = pkg / rel
        if not target.is_file():
            continue
        ext = target.suffix.lower()
        if ext in _WIN_EXEC_EXTS or ext in (".py", ".pyw"):
            continue
        k = _script_kind(target)
        if k == "node":
            # 内核认 .js/.mjs，也会读 node 的 shebang → 不用改名，
            # 只记下来提醒「这个包要 node 运行时」。
            node_scripts.append(rel)
            continue
        if k != "python":
            continue
        new_rel = rel + ".py"
        if (pkg / new_rel).exists():
            continue
        try:
            target.rename(pkg / new_rel)
        except OSError as e:  # noqa: BLE001
            log("改名失败 %s：%s" % (rel, e))
            continue
        entry["command"] = "${PLUGIN_ROOT}/" + new_rel + rest
        renamed.append((rel, new_rel))
    if renamed:
        _write_manifest(pkg, manifest)
        _rewrite_doc_refs(pkg, {old: new for old, new in renamed})
    return renamed, node_scripts


# ── 工具实现 ──────────────────────────────────────────────────────────────


def tool_preview(args, ctx):
    src = args.get("source")
    workdir = tempfile.mkdtemp(prefix="keeper-plugin-preview-")
    try:
        pkg, manifest, fmt, parsed, shelled = _fetch_to_tmp(
            src, args.get("ref"), args.get("subdir"), workdir, args.get("registry")
        )
        s = package_summary(pkg, manifest, fmt)
        target = plugin_root(ctx) / _safe_name(s["name"])
        exists = target.is_dir()
        lines = [
            "可安装：%s%s" % (s["name"], (" v" + s["version"]) if s["version"] else ""),
            "格式：%s｜能力：%s"
            % (s["format"], "、".join(s["kinds"]) if s["kinds"] else "（无）"),
        ]
        if s["description"]:
            lines.append("说明：%s" % s["description"])
        lines.append("文件 %s 个，共 %.1f KB" % (s["file_count"], s["size_kb"]))
        lines.append("来源：%s" % parsed["display"])
        if shelled:
            lines.append(
                "注意：这是散装技能包（没有 keeper 清单），安装时会自动生成一份 "
                "keeper-plugin.json 壳，包内文件原样不动"
            )
        # Windows 兼容预告：装的时候会改包内文件（补 .py 后缀 + 改清单/文档），
        # 得让用户在 preview 阶段就知道，别装完才发现被动过。
        scripts = _scan_script_commands(pkg)
        py_scripts = [r for r, k in scripts if k == "python"]
        node_scripts = [r for r, k in scripts if k == "node"]
        if py_scripts:
            lines.append(
                "Windows 兼容：安装时会给这些无后缀 Python 脚本补 .py 后缀并改写"
                "清单与文档里的路径（%s）" % "、".join(py_scripts)
            )
        if node_scripts:
            lines.append(
                "注意：%s 是 node 脚本，这个包需要 node 运行时" % "、".join(node_scripts)
            )
        lines.append(
            "目标：%s（%s）"
            % (
                target,
                "已安装同名插件，替换需 force=true 并再经用户确认"
                if exists
                else "当前未安装",
            )
        )
        lines.append("安装后需在「智能体 → 插件」勾选绑定才生效。")
        return "\n".join(lines)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def tool_install(args, ctx):
    src = args.get("source")
    force = bool(args.get("force"))
    root = plugin_root(ctx)
    workdir = tempfile.mkdtemp(prefix="keeper-plugin-install-")
    try:
        pkg, manifest, fmt, parsed, shelled = _fetch_to_tmp(
            src, args.get("ref"), args.get("subdir"), workdir, args.get("registry")
        )
        s = package_summary(pkg, manifest, fmt)
        name = _safe_name(s["name"])
        target = root / name
        if target.is_dir():
            if not force:
                cur_manifest, _ = read_manifest(target)
                cur = str(cur_manifest.get("version") or "未知版本")
                raise UserError(
                    "插件库已存在 %s（当前 %s）。确认要替换成 %s 请带 "
                    "force=true 再调一次——替换必须经用户明确同意。"
                    % (name, cur, s["version"] or "新版本")
                )
            if not has_marker(target):
                raise UserError(
                    "%s 已存在且不是插件目录（没有清单标志），拒绝覆盖——"
                    "请先人工确认这个目录是什么" % target
                )
            log("force 替换已存在的 %s" % target)
            shutil.rmtree(target)
        root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pkg), str(target))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    fixed = _fix_exec_bits(target)
    renamed, node_scripts = _compat_windows_scripts(target)
    lines = [
        "已安装：%s%s（%s 格式，能力：%s）"
        % (
            name,
            (" v" + s["version"]) if s["version"] else "",
            fmt,
            "、".join(s["kinds"]) if s["kinds"] else "（无）",
        ),
        "位置：%s" % target,
        "来源：%s" % parsed["display"],
    ]
    if shelled:
        lines.append(
            "这是 npm 散装技能包：已自动生成 keeper-plugin.json 壳（包内文件原样未动）"
        )
    if fixed:
        lines.append("已补可执行位：%s" % "、".join(fixed))
    if renamed:
        lines.append(
            "已做 Windows 兼容处理：给无后缀的 Python 脚本补了 .py 后缀（%s），"
            "并同步改了清单与包内文档里的路径——Windows 上没有 shebang，"
            "内核靠后缀判断要用解释器启动脚本。"
            % "、".join("%s → %s" % (a, b) for a, b in renamed)
        )
    if node_scripts:
        lines.append(
            "注意：%s 是 node 脚本，这个包需要 node 运行时（桌面版自带；"
            "裸内核部署要确保 PATH 上有 node）" % "、".join(node_scripts)
        )
    lines.append(
        "还没生效：工具是装配期读取的，请用户在「智能体 → 插件」勾选绑定，"
        "实例重建后工具才出现（可用 list 核验库内现状）。"
    )
    return "\n".join(lines)


def tool_remove(args, ctx):
    name = _safe_name(args.get("name"))
    root = plugin_root(ctx)
    target = root / name
    if not target.is_dir():
        raise UserError(
            "插件库里没有 %s（目录 %s 不存在）。用 list 看看现在装了什么。"
            % (name, target)
        )
    if not has_marker(target):
        raise UserError(
            "%s 没有清单标志、不像插件目录，拒绝删除——请人工确认" % target
        )
    cleaned = []
    for agent_dir in _agent_link_dirs():
        link = agent_dir / "plugins" / name
        if link.is_symlink():
            try:
                link.unlink()
                cleaned.append(agent_dir.name)
            except OSError as e:  # noqa: BLE001
                log("清链接失败 %s：%s" % (link, e))
    shutil.rmtree(target)
    lines = ["已卸载：%s（%s）" % (name, target)]
    if cleaned:
        lines.append(
            "已清理这些 agent 的插件链接：%s；它们要等实例重建后才不再加载该插件。"
            % "、".join(cleaned)
        )
    lines.append("只删了插件库目录，没有碰任何工作区文件。")
    return "\n".join(lines)


def tool_list(args, ctx):
    root = plugin_root(ctx)
    if not root.is_dir():
        return "插件库目录不存在：%s" % root
    rows = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        manifest, err = read_manifest(d)
        fmt = package_format(d)
        if not fmt:
            continue
        name = str(manifest.get("name") or "").strip() or d.name
        version = str(manifest.get("version") or "").strip()
        kinds = "、".join(kinds_of(manifest)) or "（无）"
        note = "（清单损坏：%s）" % err if err else ""
        rows.append(
            "%s%s｜%s｜%s%s"
            % (name, (" v" + version) if version else "", fmt, kinds, note)
        )
    if not rows:
        return "插件库是空的：%s" % root
    return "插件库（%s）现有 %d 个插件：\n%s" % (root, len(rows), "\n".join(rows))


TOOLS = {
    "preview": tool_preview,
    "install": tool_install,
    "remove": tool_remove,
    "list": tool_list,
}


def main():
    raw = sys.stdin.readline()
    if not raw.strip():
        print(json.dumps({"ok": False, "error": "stdin 为空（协议要求一行 JSON）"}))
        return 0
    try:
        req = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": "请求不是合法 JSON：%s" % e}))
        return 0
    tool = req.get("tool")
    args = req.get("args") or {}
    ctx = req.get("context") or {}
    fn = TOOLS.get(tool)
    if fn is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "未知工具 %r（可用：%s）"
                    % (tool, "、".join(sorted(TOOLS))),
                },
                ensure_ascii=False,
            )
        )
        return 0
    try:
        result = fn(args, ctx)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except UserError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 兜底：协议要求最后一行必须是 JSON
        log("未捕获异常：%r" % e)
        print(
            json.dumps(
                {"ok": False, "error": "安装器内部错误：%r" % e}, ensure_ascii=False
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
