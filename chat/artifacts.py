"""产物文件解析：按 URI scheme 分派到不同存储后端（与 API 路由解耦）。

产物在 ``session_messages.artifacts`` 里的 ``path`` 是一个**带 scheme 的 URI**：

- ``file:///abs/path``（或裸绝对路径，默认按 ``file`` 处理）→ 本机文件系统；
- ``http(s)://host/...`` → 远程 / 云存储，由本服务**受控代取**。

前端只凭 ``artifact_id`` 拼 URL，真正的取数逻辑全在本模块，便于：

- 扩展新的存储后端（只需注册一个 scheme handler）；
- 统一安全策略（本机越界校验、远程 SSRF 防护）。

安全：
- 本机：``resolve()`` 后仍要求文件真实存在，软链被替换跳出工作空间会 404（S2）。
- 远程：**默认拒绝任何远程代取**；仅 ``KEEPER_ARTIFACT_HTTP_ALLOW_HOSTS`` 白名单内的
  host 才允许，且解析出的 IP 不得为私有 / 回环 / 链路本地 / 保留段，并限制大小与超时，
  且**不跟随重定向**——避免 SSRF（云元数据、内网探测）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# --- 远程代取的安全参数（默认即「拒绝一切远程」） ---
_HTTP_TIMEOUT = 10.0
_HTTP_MAX_BYTES = 50 * 1024 * 1024  # 50MB
_HTTP_ALLOW_HOSTS = {
    h.strip().lower()
    for h in (os.environ.get("KEEPER_ARTIFACT_HTTP_ALLOW_HOSTS") or "").split(",")
    if h.strip()
}


class ArtifactResolutionError(Exception):
    """产物解析失败：文件不存在 / scheme 不支持 / 远程被拒等。"""


@dataclass
class ArtifactSource:
    """解析后的产物取数来源。"""

    kind: str  # "file" | "bytes"
    path: Path | None = None  # 本机文件
    data: bytes | None = None  # 远程字节
    size: int | None = None

    @property
    def is_local(self) -> bool:
        return self.kind == "file"


async def resolve_artifact_uri(uri: str) -> ArtifactSource:
    """按 scheme 分派解析 ``uri``，返回可取数的 ``ArtifactSource``。"""
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "file").lower()
    if scheme in ("", "file"):
        return _resolve_file(uri)
    if scheme in ("http", "https"):
        return await _resolve_http(parsed)
    raise ArtifactResolutionError(f"不支持的 scheme: {scheme}")


def _resolve_file(uri: str) -> ArtifactSource:
    """本机文件：``file:///abs`` 或裸绝对路径。"""
    parsed = urlparse(uri)
    raw = parsed.path or uri
    if not raw:
        raise ArtifactResolutionError("路径为空")
    try:
        # expanduser 处理 ~；resolve 解析软链并归一化
        p = Path(raw).expanduser().resolve()
    except Exception:
        raise ArtifactResolutionError("路径无效")
    if not p.is_file():
        raise ArtifactResolutionError("文件不存在")
    return ArtifactSource(kind="file", path=p, size=p.stat().st_size)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向（防 30x 跳转到内网 / 元数据地址）。"""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _reject_private_host(host: str) -> None:
    """解析 host 的所有 IP，拒绝私有 / 回环 / 链路本地 / 保留段。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ArtifactResolutionError(f"无法解析 host: {host}") from e
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            raise ArtifactResolutionError("拒绝访问内网 / 保留地址")


async def _resolve_http(parsed) -> ArtifactSource:
    """远程文件：受控代取（白名单 + 禁内网 + 限大小 + 不跟随重定向）。"""
    if not _HTTP_ALLOW_HOSTS:
        raise ArtifactResolutionError("远程代取未启用")
    host = (parsed.hostname or "").lower()
    if host not in _HTTP_ALLOW_HOSTS:
        raise ArtifactResolutionError("远程 host 不在白名单")
    _reject_private_host(host)
    try:
        data = await asyncio.to_thread(_fetch_http, parsed.geturl())
    except ArtifactResolutionError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("远程产物代取失败: %s", e)
        raise ArtifactResolutionError("远程获取失败") from e
    return ArtifactSource(kind="bytes", data=data, size=len(data))


def _fetch_http(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "keeper-artifact/1.0"}
    )
    with _opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
        data = resp.read(_HTTP_MAX_BYTES + 1)
    if len(data) > _HTTP_MAX_BYTES:
        raise ArtifactResolutionError("远程文件过大")
    return data
