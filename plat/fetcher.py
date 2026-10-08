"""资源代码下载器（轻量）。

职责单一：把平台下发的 ``source``（见平台 ``SourceSpec``）描述的代码，**下载 / 更新**
到本地指定目录。不负责「下载后怎么跑」——那是 ``agent.config`` / MCP / tool 装配的事。

设计要点：
- 幂等：目录已存在就 ``fetch`` + ``checkout``，不重复全量 clone；
- 失败即抛 ``FetchError``，由调用方（装载流程）据此**阻断 agent 装载**；
- 目前实现 git；http（压缩包下载）预留，调用即抛 ``FetchError``。

目录约定：``resource_dir(agent_root, kind, name)`` ⇒ ``<agent_root>/<kind>/<name>``，
``agent_root`` 由调用方决定（每个 agent 独立目录，或 agent 显式配置的缓存目录）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tarfile

from dulwich import porcelain
from dulwich.errors import GitProtocolError
from dulwich.repo import Repo
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 单次命令超时（秒）。clone 大仓库可能较慢，给足 5 分钟。
_GIT_TIMEOUT = 300


class FetchError(Exception):
    """资源下载失败。调用方应据此阻断 agent 装载。"""


def resource_dir(agent_root: str | Path, kind: str, name: str) -> Path:
    """该资源在本地的目标目录：``<agent_root>/<kind>/<name>``。"""
    return Path(agent_root) / kind / name


def resource_code_dir(dest: str | Path, source: dict) -> Path:
    """仅根据 ``source.subpath`` 推算实际代码目录，**不触发任何下载 / 网络**。

    与 :func:`fetch_resource` 返回同一个目录——运行时（装载后启动命令）用来确定
    ``cwd``，而不必再跑一遍 git。``source`` 取不到 subpath 时返回 ``dest`` 本身。
    """
    dest = Path(dest)
    subpath = source.get("subpath") if isinstance(source, dict) else None
    return dest / subpath if subpath else dest


async def _fetch_git(dest: Path, url: str, ref: str | None, subpath: str | None) -> None:
    """git 方式：clone 或更新到 ``dest``，再（如需要）进入 ``subpath``。

    基于嵌入式 dulwich 实现，**不再依赖宿主机装了 git 二进制**——普通用户机上
    没有 git 也能拉插件。行为对齐原 git 子进程方案：

    - 目录不存在：``clone``，随后若有 ``ref`` 则精确 checkout（游离态，确定性强）；
    - 目录已存在：``fetch`` 更新，再 checkout 到 ``ref``（或默认分支快进）。
    """
    loop = asyncio.get_running_loop()
    dest = Path(dest)
    if dest.joinpath(".git").exists():
        await loop.run_in_executor(None, _git_update, dest, url, ref)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await loop.run_in_executor(None, _git_clone, dest, url, ref)


def _git_clone(dest: Path, url: str, ref: str | None) -> None:
    """首次克隆；若有 ref 则游离态切到精确提交。"""
    try:
        porcelain.clone(source=url, target=str(dest))
    except (GitProtocolError, OSError) as e:
        raise FetchError(f"git clone 失败（{url}）：{e}") from e
    if ref:
        _checkout_ref(dest, ref)


def _git_update(dest: Path, url: str, ref: str | None) -> None:
    """已克隆：fetch 更新，再按 ref 精确切，或默认分支快进。"""
    try:
        repo = Repo(str(dest))
        porcelain.fetch(repo, remote_location=url, prune=True, include_tags=True)
        if ref:
            _checkout_ref(dest, ref)
        else:
            # 默认分支快进：直接把工作树对齐到 origin/<当前分支>
            try:
                branch = porcelain.active_branch(repo).decode("utf-8")
            except Exception:
                branch = "main"
            porcelain.reset(repo, "hard", treeish=f"refs/remotes/origin/{branch}")
    except (GitProtocolError, OSError) as e:
        raise FetchError(f"git 更新失败（{url}）：{e}") from e


def _checkout_ref(dest: Path, ref: str) -> None:
    """把仓库游离态切到 ref（分支 / 标签 / commit 均可），并硬重置工作树。"""
    repo = Repo(str(dest))
    commit = _resolve_commit(repo, ref)
    try:
        porcelain.update_head(repo, commit, detached=True)
        porcelain.reset(repo, "hard")
    except (GitProtocolError, OSError) as e:
        raise FetchError(f"git checkout {ref} 失败：{e}") from e


def _resolve_commit(repo: Repo, ref: str) -> bytes:
    """把分支名 / 标签名 / 短 sha 解析成 40 位 commit sha（bytes）。"""
    ref_b = ref.encode("utf-8") if isinstance(ref, str) else ref
    # 1) 直接是完整 sha
    try:
        obj = repo[ref_b]
        if obj.type_name == b"commit":
            return obj.id
        if obj.type_name == b"tag":
            return obj.object.id
    except Exception:
        pass
    # 2) 常见 ref 形态（本地分支 / 标签 / 远程跟踪分支）
    for prefix in (b"refs/heads/", b"refs/tags/", b"refs/remotes/origin/"):
        key = prefix + ref_b
        if key in repo.refs:
            sha = repo.refs[key]
            obj = repo[sha]
            return obj.object.id if obj.type_name == b"tag" else sha
    raise FetchError(f"无法解析 git ref：{ref}")


async def _fetch_http(dest: Path, url: str, sha256: str | None, fmt: str | None) -> None:
    """http 方式：下载文件 → 校验 sha256 → 按 ``fmt`` 决定解压还是直接落盘。

    ``fmt``（文件类型，与传输方式解耦）：
    - ``"zip"``（缺省）：下载压缩包 → 解压到 ``dest``（支持 zip / tar.gz，按文件头识别）；
    - ``"folder"``：当作原始文件落盘到 ``dest`` 目录内（保留 ``code_dir`` 为目录）。

    此函数既服务 http 也服务 git+zip——只要 ``format`` 选定 ``zip``，无论 ``kind``
    是 git 还是 http，url 都按「一个压缩包文件」下载并解压。
    """
    # 幂等：已经下载过（目录非空）就跳过，避免每次装载都重新下载大包
    if dest.is_dir() and any(dest.iterdir()):
        logger.info("http 资源已存在，跳过下载：%s", dest)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / (dest.name + ".part")
    await _download(url, part)
    try:
        _verify_sha256(part, sha256)
        if (fmt or "zip") == "folder":
            # 原始文件直接落盘（放在 dest 目录内，保留 code_dir 为目录）
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / Path(url).name
            part.replace(target)
        else:
            _extract(part, dest)
    finally:
        part.unlink(missing_ok=True)


async def _download(url: str, target: Path) -> None:
    """下载 ``url`` 到 ``target``（阻塞 IO 放到线程里跑）。"""
    def _do() -> None:
        urllib.request.urlretrieve(url, str(target))
    try:
        await asyncio.to_thread(_do)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"http 下载失败（{url}）：{e}") from e


def _verify_sha256(path: Path, sha256: str | None) -> None:
    """校验文件 sha256（不给则跳过）。"""
    if not sha256:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest().lower() != sha256.lower():
        raise FetchError(
            f"sha256 校验失败：期望 {sha256}，实际 {h.hexdigest()}"
        )


def _tar_filter() -> str | None:
    """Python 3.12+ 支持 ``data`` 过滤（防路径穿越）；老版本退回 None。"""
    return "data"


def _extract(archive: Path, dest: Path) -> None:
    """把压缩包解压到 ``dest``：按文件头自动识别 zip / tar.gz，避免依赖扩展名。"""
    dest.mkdir(parents=True, exist_ok=True)
    with open(archive, "rb") as f:
        magic = f.read(4)
    is_zip = magic[:2] == b"PK" or archive.suffix.lower() == ".zip"
    is_tar = (
        tarfile.is_tarfile(str(archive))
        or archive.suffix.lower() in (".tgz", ".tar")
        or magic[:2] == b"\x1f\x8b"  # gzip
    )
    if is_zip:
        with zipfile.ZipFile(archive) as z:
            _safe_extract_zip(z, dest)
    elif is_tar:
        with tarfile.open(str(archive)) as t:
            t.extractall(dest, filter=_tar_filter())  # 防路径穿越
    else:
        raise FetchError(
            f"无法识别的压缩格式（{archive.name}），只支持 zip / tar.gz"
        )


def _safe_extract_zip(z: zipfile.ZipFile, dest: Path) -> None:
    """解压 zip 并防御 zip-slip（成员路径不得逃出 ``dest``）。

    同时还原 zip 里记录的 Unix 权限位（含可执行位 +x）——Python 的 ``zipfile``
    默认**不**恢复权限，导致本该是可执行文件的成员被解压成普通文稿
    （macOS 尤其明显：系统解压工具双击能跑、程序解压出来却是文档）。这里按
    zip 的 ``external_attr`` 把原权限位还回去，与系统解压工具行为一致。
    """
    dest = dest.resolve()
    for name in z.namelist():
        target = (dest / name).resolve()
        if target != dest and not str(target).startswith(str(dest) + os.sep):
            raise FetchError(f"zip 内含危险路径，拒绝解压：{name}")
        info = z.getinfo(name)
        z.extract(name, str(dest))
        # 还原 Unix 权限位（external_attr 高 16 位即 st_mode）；不覆盖则解压出来
        # 默认 0644，丢失可执行位。
        mode = info.external_attr >> 16
        if mode:
            try:
                os.chmod(target, mode & 0o7777)
            except OSError:
                pass


def _locate_inner_zip(repo_dir: Path, archive: str | None) -> Path:
    """在 ``git clone`` 下来的仓库目录里找到要解压的那个 zip。

    - 指定了 ``archive`` → 按相对路径（相对仓库根 ``dest``）定位；
    - 没指定 → 探测仓库顶层（**非递归**）的第一个 ``.zip``：
      恰好一个就用它；没有 / 多个都明确报错（多个时提示用 ``archive`` 字段指定）。
    """
    if archive:
        p = repo_dir / archive
        if not p.is_file():
            raise FetchError(f"source.archive 指定的压缩包不存在：{p}")
        if not (p.suffix.lower() == ".zip" or zipfile.is_zipfile(str(p))):
            raise FetchError(f"source.archive 指定的文件不是 zip：{p}")
        return p

    zips = [p for p in repo_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    if len(zips) == 1:
        return zips[0]
    if not zips:
        raise FetchError(
            f"git 仓库内未找到 zip 文件（目录={repo_dir}），"
            f"若压缩包在子目录请用 source.archive 指定"
        )
    raise FetchError(
        f"git 仓库内找到多个 zip，请用 source.archive 指定要解压的那个："
        f"{[p.name for p in zips]}"
    )


async def fetch_resource(source: dict, dest: str | Path) -> Path:
    """按 ``source`` 把资源代码拉到 ``dest``，返回实际代码目录（考虑 ``subpath``）。

    ``source`` 来自平台 ``SourceSpec``（dict 形式）：
    ``{kind, url, ref, subpath, sha256, format}``。任何失败都抛 ``FetchError``。

    下载方式由 ``kind`` 与 ``format`` 共同决定，**以 ``format`` 优先**——

    - ``format=="zip"``：
        - ``kind==git``：先把整个仓库 clone 下来（一个文件夹），再从仓库目录里
          找到那个 zip（``archive`` 指定或自动探测）解压出来；
        - ``kind==http``：url 指向一个 zip / tar.gz 文件，下载后解压（例如
          GitHub archive 链接）；
    - ``format=="folder"``：git → clone；http → 把单文件直接落盘到目录内；
    - ``format`` 留空 → 按 ``kind`` 推断（向后兼容）：git → clone，http → 解压。
    """
    if not isinstance(source, dict):
        raise FetchError("source 必须是对象")

    kind = (source.get("kind") or "git").lower()
    url = source.get("url")
    if not url:
        raise FetchError("source.url 不能为空")

    dest = Path(dest)
    ref = source.get("ref")
    subpath = source.get("subpath")
    fmt = (source.get("format") or "").lower() or None

    if kind == "local":
        # 本地包：不下载，url 直接就是包目录路径（本机开发 / 预置插件用）。
        # 后面的 code_dir 计算照常生效，所以 subpath 依然可用。
        dest = Path(url).expanduser()
        if not dest.is_dir():
            raise FetchError(f"本地资源目录不存在：{dest}")
    elif fmt == "zip":
        if kind == "git":
            # git 仓库里夹带一个 zip：先把整个仓库 clone 下来（文件夹），
            # 再从仓库目录里找到那个 zip 解压出来（archive 指定或自动探测）。
            await _fetch_git(dest, url, ref, None)
            zip_path = _locate_inner_zip(dest, source.get("archive"))
            _extract(zip_path, dest)  # 解压进仓库目录内
        else:
            # http 直接下载压缩包再解压
            await _fetch_http(dest, url, source.get("sha256"), "zip")
    elif fmt == "folder":
        if kind == "git":
            await _fetch_git(dest, url, ref, subpath)
        else:
            await _fetch_http(dest, url, source.get("sha256"), "folder")
    elif kind == "git":
        await _fetch_git(dest, url, ref, subpath)
    elif kind == "http":
        await _fetch_http(dest, url, source.get("sha256"), None)  # 留空 → 解压
    else:
        raise FetchError(f"不支持的下载方式：{kind}")

    code_dir = dest / subpath if subpath else dest
    if not code_dir.exists():
        raise FetchError(f"下载完成但代码目录不存在：{code_dir}")
    return code_dir
