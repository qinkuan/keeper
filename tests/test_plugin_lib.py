"""插件库回归脚本：`python keeper/tests/test_plugin_lib.py`。

跟 ``keeper/tests/test_guard.py`` / ``keeper/tests/test_planner_parse.py`` 一个路子——
本仓的"测试"就是能直接 python 跑完就出结论的脚本，不引测试框架。

**隔离**：`agent_plugins_dir()` 落在 ``Path.home()`` 下，所以脚本开头把 ``HOME``
指到临时目录再导入模块——绝不碰真实的 ``~/.keeper``。之前一版测试没这么做，
在真实家目录里留下了残链，导致"列出已链接"这类断言被上一轮的残留物干扰。

覆盖：插件库配置 / 清单解析 / 目录扫描 / 每-agent 的符号链接。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须在导入 keeper 之前设好 HOME：Path.home() 读它
_TMP = Path(tempfile.mkdtemp(prefix="keeper-pluginlib-"))
os.environ["HOME"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.config import KeeperConfig, PluginSection  # noqa: E402
from keeper.plugin import (  # noqa: E402
    EnvDependency,
    agent_plugins_dir,
    find_plugin,
    link_plugin,
    linked_plugins,
    parse_env_dependencies,
    read_manifest,
    scan_library,
    unlink_plugin,
)

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


# ── 配置 ────────────────────────────────────────────────────────────────────
cfg = KeeperConfig.from_dict({"plugins": {"root": "/tmp/x-plugins"}})
check(cfg.plugins.root == "/tmp/x-plugins", "config.yaml 的 plugins.root 生效", cfg.plugins.root)
check(
    PluginSection().root == "~/.keeper/plugins", "默认插件库路径", PluginSection().root
)


# ── 清单：envDependencies ──────────────────────────────────────────────────
deps = parse_env_dependencies(
    {
        "envDependencies": [
            {"kind": "node", "minVersion": "18.0.0", "maxVersion": None},
            {"kind": "python", "min_version": "3.10"},  # 蛇形也认
            {"kind": "ruby", "minVersion": "3"},  # 不认的 kind
            "不是 dict",  # 跳过
        ]
    }
)
check(
    [(d.kind, d.min_version, d.max_version) for d in deps]
    == [("node", "18.0.0", ""), ("python", "3.10", "")],
    "envDependencies 解析（兼容蛇形、跳过非法项）",
    [(d.kind, d.min_version) for d in deps],
)
check(parse_env_dependencies({}) == [], "没声明时为空")
check(
    EnvDependency("node", "18").to_dict()
    == {"kind": "node", "minVersion": "18", "maxVersion": ""},
    "EnvDependency.to_dict",
)


# ── 扫描 ────────────────────────────────────────────────────────────────────
lib = _TMP / "mylib"
(lib / "bash").mkdir(parents=True)
(lib / "bash" / "keeper-plugin.json").write_text(
    json.dumps(
        {
            "name": "bash",
            "version": "1.0.0",
            "description": "跑命令",
            "bin": [{"command": "x", "tools": []}],
            "envDependencies": [{"kind": "python", "minVersion": "3.10"}],
        }
    ),
    encoding="utf-8",
)
(lib / "mcp-only").mkdir()
(lib / "mcp-only" / "keeper-plugin.json").write_text(
    json.dumps({"name": "mcp-only", "mcpServers": {"s": {"command": "x"}}}), encoding="utf-8"
)
(lib / "bad").mkdir()
(lib / "bad" / "keeper-plugin.json").write_text("{ 不是 json", encoding="utf-8")
(lib / "no-manifest").mkdir()  # 没有清单 → 不算插件
(lib / "__MACOSX").mkdir()  # 打包噪声 → 跳过
(lib / ".hidden").mkdir()  # 隐藏 → 跳过

plugins = scan_library(lib)
names = [p.name for p in plugins]
check(names == ["bad", "bash", "mcp-only"], "扫描结果（排序、跳过无清单/隐藏/噪声）", names)
check(
    all(p.dirname for p in plugins),
    "每个插件都带库内目录名（链接名要用它）",
    [p.dirname for p in plugins],
)

bash = find_plugin("bash", lib)
assert bash is not None, "find_plugin 应找到 bash"
check(bash.kinds == ["bin"], "识别 bin 能力", bash.kinds)
check(len(bash.env_dependencies) == 1, "扫描时带出环境要求")
check(bash.size_bytes > 0, "目录体积", bash.size_bytes)
check(find_plugin("mcp-only", lib).kinds == ["mcp"], "识别 mcp 能力")  # type: ignore[union-attr]
check(find_plugin("nope", lib) is None, "找不到时返回 None")
check(any(p.name == "bad" and p.error for p in plugins), "坏清单带 error（能看见并去修）")
check(read_manifest(lib / "no-manifest") == {}, "没有清单时按空清单处理")


# ── 每 agent 的符号链接 ─────────────────────────────────────────────────────
agent_id = "01TEST"
link = link_plugin(agent_id, bash)
check(link.is_symlink(), "建链接", str(link))
check(link.name == "bash", "链接名 = 库内目录名", link.name)
check(linked_plugins(agent_id) == ["bash"], "列出已链接", linked_plugins(agent_id))

raw = os.readlink(link)
check(not os.path.isabs(raw), "用相对链接（~/.keeper 搬家不会断）", raw)
check(link.resolve() == bash.path.resolve(), "相对链接能解析回真实目录")

link_plugin(agent_id, bash)  # 幂等：重复勾选同一个插件
check(linked_plugins(agent_id) == ["bash"], "重复建链接幂等", linked_plugins(agent_id))

# 库里的插件挪了位置 → 重建后能跟上
moved = lib / "mcp-only"
bash.path = moved
link_plugin(agent_id, bash)
check(link.resolve() == moved.resolve(), "目标变了会自动重建")

check(unlink_plugin(agent_id, "bash") is True, "删链接")
check(linked_plugins(agent_id) == [], "删完为空", linked_plugins(agent_id))
check(unlink_plugin(agent_id, "bash") is False, "删不存在的链接返回 False")

link_plugin(agent_id, bash)
unlink_plugin(agent_id, "bash")
check(moved.is_dir(), "删链接不会删掉库里的插件目录")
check(
    str(agent_plugins_dir(agent_id)).endswith("agents/01TEST/plugins"),
    "链接目录位置",
    str(agent_plugins_dir(agent_id)),
)


shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过")
