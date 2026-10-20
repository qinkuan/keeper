"""PTC 沙箱：隔离的 Python 运行环境 + 工具桥。

代码在独立 venv（依赖与工作区隔离）里跑子进程；代码里 ``kt.call(name, **kw)``
通过 unix-socket 调回主系统的真实工具（由调用方提供的 ``call_tool`` 桥实现）。
危险操作先静态扫描，命中由上层按策略拦/放；缺包时非交互地 pip install（仅沙箱内）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, List, Tuple

_KEEPER_TOOLS_SHIM = '''\
import json, os, socket

# 代码侧工具桥：``kt.call(name, **kw)`` -> 工具返回的字符串。
# 用模块级函数而非实例，这样 ``import keeper_tools as kt`` 后 ``kt.call`` 直接可用。
_sock = os.environ.get("KEEPER_TOOLS_SOCK")

def call(name, **kw):
    if not _sock:
        return "[工具桥未就绪]"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(_sock)
            s.sendall((json.dumps({"name": name, "args": kw}) + "\\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode()).get("result", "")
    except Exception as e:
        return f"[工具调用失败: {e}]"
'''

# 危险操作静态扫描：速度护栏（不是墙）。命中后由调用方按策略拦/放。
_DANGER_PATTERNS: List[Tuple[str, str]] = [
    (r"\brm\s+-rf\b", "rm -rf 递归强制删除"),
    (r"\bshutil\.rmtree\b", "shutil.rmtree 删目录树"),
    (r":\(\)\s*\{.*\}\s*;:", "fork 炸弹"),
    (r"\bsudo\b", "sudo 提权"),
    (r"\bdd\s+if=", "dd 磁盘写入"),
    (r"\bmkfs\b", "mkfs 格式化"),
    (r"\bos\.system\b", "os.system 执行 shell"),
    (r"\bsubprocess\.", "subprocess 起子进程"),
    (r"shell\s*=\s*True", "shell=True"),
    (r"\beval\s*\(", "eval 动态执行"),
    (r"\bexec\s*\(", "exec 动态执行"),
    (r"__import__\s*\(", "__import__ 动态导入"),
    (r"\{\s*[\"']?curl[\"']?\s*.*\|\s*sh\b", "curl|sh 下载即执行"),
    (r"\.ssh", "触碰 .ssh"),
    (r"id_rsa", "触碰私钥"),
    (r"/etc/shadow", "触碰 /etc/shadow"),
    (r"chmod\s+-R", "chmod -R 递归改权限"),
]


def sandbox_root() -> Path:
    base = os.environ.get("KEEPER_PTC_SANDBOX")
    return Path(base) if base else (Path.home() / ".keeper" / "ptc-sandbox")


def ensure_venv() -> Path:
    """确保隔离 venv 存在并返回其 python 路径。

    测试时可被 monkeypatch（例如 ``ptc_sandbox.ensure_venv = lambda: sys.executable``）
    以避免真正建 venv。
    """
    root = sandbox_root()
    venv = root / "venv"
    if venv.exists():
        return venv / "bin" / "python"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / "bin" / "python"
    # 预装常用库（按需扩展）；失败不致命（某些环境无网络）。
    subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "numpy", "pandas", "requests"],
        check=False,
    )
    return py


def make_run_dir() -> Path:
    root = sandbox_root() / "runs"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"r_{uuid.uuid4().hex[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scan_code(code: str) -> List[str]:
    """返回命中的危险操作标签列表（空 = 未命中）。"""
    hits: List[str] = []
    for pat, label in _DANGER_PATTERNS:
        if re.search(pat, code):
            hits.append(label)
    return hits


async def run_code(
    code: str,
    call_tool: Callable[[str, dict], Any],
    timeout: float = 60.0,
) -> Tuple[str, List[str]]:
    """在沙箱里跑一段代码，返回 ``(输出, 产物路径列表)``。

    ``call_tool`` 是内核工具桥（异步）：代码侧 ``kt.call`` 经 socket 调它。
    """
    py = ensure_venv()
    rd = make_run_dir()
    main_py = rd / "main.py"
    shim = rd / "keeper_tools.py"
    shim.write_text(_KEEPER_TOOLS_SHIM)
    # 自动注入 kt，模型无需手动 import
    main_py.write_text("import keeper_tools as kt  # injected\n" + code)
    sock = str(rd / "kt.sock")

    async def handle(reader, writer):
        name = "?"
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode())
                name = msg.get("name", "?")
                args = msg.get("args") or {}
                res = await call_tool(name, args)
            except Exception as err:  # noqa: BLE001
                res = f"[工具 {name} 出错: {err}]"
            writer.write((json.dumps({"result": res}) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=sock)
    text = ""
    artifacts: List[str] = []
    try:
        env = dict(os.environ)
        env["KEEPER_TOOLS_SOCK"] = sock
        env["PYTHONPATH"] = str(rd) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(py), str(main_py),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(rd), env=env,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                text = stdout.decode(errors="replace")
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                text = f"[超时] 代码运行超过 {timeout}s 被中断"
        except Exception as e:  # noqa: BLE001
            text = f"[运行失败: {e}]"
        # 收集产物（沙箱目录里除源码/桥/套接字外的文件）
        for p in rd.iterdir():
            if p.name not in ("main.py", "keeper_tools.py", "kt.sock") and p.is_file():
                artifacts.append(str(p))
    finally:
        server.close()
        try:
            os.unlink(sock)
        except OSError:
            pass
    return text, artifacts
