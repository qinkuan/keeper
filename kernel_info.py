"""内核运行时信息：端口在哪、进程是谁。

为什么需要它：**端口不能写死**。用户机器上 8080 可能早被别的程序占着，写死
就起不来——或者更糟：起了个服务却连到了别人的服务上。

所以内核启动时自己挑一个空闲端口（``port: 0`` 或配置的端口被占用时），
并把**实际端口**写进 ``~/.keeper/runtime/kernel.json``，让别的东西能**问出来**
而不是猜：

- ``start_keeper.sh`` —— 打印「实际跑在哪个端口」；
- ``web/start_web.sh`` / ``web/vite.config.ts`` —— 前端 dev server 的接口代理
  目标（原先硬编码 8080）；
- 人 —— 直接 ``cat ~/.keeper/runtime/kernel.json``。

桌面壳（Electron）不依赖本文件：它在 ``sidecar.js`` 里自己抢端口
（``findFreePort``），再把端口当命令行参数传给内核——那条路径天然不会撞。

文件格式（JSON）::

    {"pid": 12345, "host": "127.0.0.1", "port": 54321,
     "url": "http://127.0.0.1:54321", "started_at": "...", "config": "..."}
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 运行时信息文件：与其它本机数据同处 ~/.keeper，但独立成目录——
# 它是「当前进程的状态」，不是用户数据，重装/清数据时不必保留。
KERNEL_INFO_PATH = Path.home() / ".keeper" / "runtime" / "kernel.json"


def _socket_family(host: str) -> int:
    """按地址选地址族：``::1`` 走 IPv6，其余走 IPv4。"""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def is_port_free(host: str, port: int) -> bool:
    """这个端口能不能 bind（只探测，不占用）。"""
    try:
        with socket.socket(_socket_family(host), socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def pick_port(host: str, preferred: int = 0) -> Tuple[int, str]:
    """挑一个能用的端口，返回 ``(端口, 说明)``。

    ``preferred`` > 0 且能绑定 → 就用它（用户显式指定的端口优先）。
    否则 → 让系统分配一个空闲端口（``bind(port=0)``）。

    为什么不能"先探测再启动"就完事：探测与真正 bind 之间有**竞态**——这中间
    端口可能被别人抢走。所以这里只做**决策**，真正的绑定交给 uvicorn；万一
    启动时真撞了，:func:`keeper.main.main` 会退回到系统分配再试一次。
    """
    if preferred and preferred > 0:
        if is_port_free(host, preferred):
            return preferred, "配置的端口"
        return free_port(host), f"端口 {preferred} 已被占用，改用系统分配的"
    return free_port(host), "系统分配的空闲端口"


def free_port(host: str = "127.0.0.1") -> int:
    """让系统分配一个空闲端口（bind 到 0 再读回来，随即关闭）。

    给**启动脚本**用：脚本要先知道端口才能打印地址（``start_keeper.sh``），
    于是它先抢一个空闲端口再传给内核，而不是等内核起来后才知道。
    """
    with socket.socket(_socket_family(host), socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def write_kernel_info(
    host: str,
    port: int,
    *,
    pid: Optional[int] = None,
    config_path: Optional[str] = None,
) -> Path:
    """把当前内核的地址写进 ``kernel.json``，返回文件路径。"""
    info: Dict[str, Any] = {
        "pid": pid if pid is not None else os.getpid(),
        "host": host,
        "port": int(port),
        "url": f"http://{host}:{port}",
        "config": config_path or "",
    }
    try:
        KERNEL_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
        KERNEL_INFO_PATH.write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        # 写不了不该拦住启动：端口只是"给人看的"，服务本身不依赖它
        pass
    return KERNEL_INFO_PATH


def read_kernel_info() -> Optional[Dict[str, Any]]:
    """读 ``kernel.json``；文件不存在或坏了就返回 None。"""
    try:
        if not KERNEL_INFO_PATH.exists():
            return None
        data = json.loads(KERNEL_INFO_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_kernel_info() -> None:
    """删掉 ``kernel.json``（内核退出时调，免得留下过期端口误导人）。"""
    try:
        KERNEL_INFO_PATH.unlink(missing_ok=True)
    except OSError:
        pass
