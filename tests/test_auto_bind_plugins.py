"""核心 agent 首装自动绑定插件：`python keeper/tests/test_auto_bind_plugins.py`。

背景
----
内置插件是**应用带走的**（打包在 app 的 resources 里），启动时复制到
``~/.keeper/plugins``。但"装进插件库"和"绑给 agent"是两件事——绑定关系是
``~/.keeper/agents/<id>/plugins/<name>`` 这个符号链接，而以前只有 UI 能创建它。

于是**全新安装**的机器上：用户一个 agent 都没建、也没勾过任何插件，核心 agent
就一条工具都没有（bash 是唯一的执行入口）。这不是"配置缺失"，是"开箱即用"断了。

覆盖
----
1. 核心 agent 从没绑过 → 自动绑上内置插件（bash）；
2. 插件目录存在但为空 → **不**自动绑（用户显式解绑过，别添乱）；
3. 非核心 agent → 不自动绑（用户自建的 agent 由 UI 显式选插件）；
4. 库里没有那个插件 → 跳过，不报错；
5. 重复调用幂等。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须在导入 keeper 之前设好 HOME：Path.home() 读它
_TMP = Path(tempfile.mkdtemp(prefix="keeper-autobind-"))
os.environ["HOME"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import keeper.config as kconfig  # noqa: E402
from keeper.agent import config as agent_config  # noqa: E402

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


class Stub:
    """agent 画像替身：插件链路只读 id / name 两个字段"""

    def __init__(self, id: str, name: str = "x"):
        self.id = id
        self.name = name


# ── 造一个只有 bash 的插件库 ────────────────────────────────────────────────
_lib = _TMP / "plugins"
(_lib / "bash").mkdir(parents=True)
(_lib / "bash" / "keeper-plugin.json").write_text(
    json.dumps({"name": "bash", "version": "1.0.0", "bin": [{"command": "x", "tools": []}]}),
    encoding="utf-8",
)

_cfg = kconfig.KeeperConfig.from_dict(
    {"plugins": {"root": str(_lib)}, "core_agent": {"id": "core"}}
)
kconfig.load_config = lambda: _cfg  # type: ignore[assignment]


def links_of(agent_id: str):
    return agent_config.agent_plugins_dir(agent_id)


async def main() -> int:
    # 1) 核心 agent 从没绑过 → 自动绑
    await agent_config._ensure_plugin_links(Stub("core", "主脑"))  # type: ignore[arg-type]
    link = links_of("core") / "bash"
    check(link.is_symlink(), "核心 agent 首次装配自动绑上了 bash", str(link))
    check(link.is_dir(), "链接指向插件库里的真实目录")
    check(
        link.resolve() == (_lib / "bash").resolve(),
        "链接指向库里的 bash",
        str(link.resolve()),
    )

    # 5) 幂等：再装配一次不会出事
    await agent_config._ensure_plugin_links(Stub("core", "主脑"))  # type: ignore[arg-type]
    check(links_of("core").is_dir(), "重复调用后目录仍在")

    # 3) 非核心 agent 不自动绑
    await agent_config._ensure_plugin_links(Stub("01OTHER", "用户建的"))  # type: ignore[arg-type]
    check(
        not links_of("01OTHER").exists(),
        "非核心 agent 不自动绑（用户自建 agent 由 UI 显式选插件）",
        str(links_of("01OTHER")),
    )

    # 2) 目录存在但为空 → 用户显式解绑过 → 不干预
    empty_dir = links_of("01EMPTY")
    empty_dir.mkdir(parents=True, exist_ok=True)
    await agent_config._ensure_plugin_links(Stub("01EMPTY", "解绑过的"))  # type: ignore[arg-type]
    check(
        list(empty_dir.iterdir()) == [],
        "插件目录存在但为空 → 不自动绑回去",
        [p.name for p in empty_dir.iterdir()],
    )

    # 4) 库里没有那个插件 → 跳过，不报错
    shutil.move(str(_lib / "bash"), str(_TMP / "bash-away"))
    await agent_config._ensure_plugin_links(Stub("01NOBASH", "没插件的"))  # type: ignore[arg-type]
    check(
        not links_of("01NOBASH").exists() or not any(links_of("01NOBASH").iterdir()),
        "库里没有 bash 时跳过，不报错",
    )
    shutil.move(str(_TMP / "bash-away"), str(_lib / "bash"))
    return 0


try:
    asyncio.run(main())
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过")
