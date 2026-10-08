"""agent 的装载 / 卸载 + **本地** agent 的增删改。

两套并存，各管一段：

1. **本地创建**（``POST /manage/agents`` 等）：画像与能力绑定都落本地库，
   插件来自本地插件库目录。这条路**不碰平台**——本机自用的 agent 不需要
   联网、不需要注册到任何中心。
2. **装载 / 卸载**（``load`` / ``unload``）：从平台拉配置 → 落本地库 → 装配。
   平台 agent 走这条。

两者的配置都存在同一张 ``agent`` 表里，**不会互相覆盖**：``sync_agent`` 只在
``load`` 接口里被调用（且只接受平台上的 agent），本地创建的 agent 永远不经过
它，所以它的绑定不会被整体替换掉。

插件绑定落到 ``~/.keeper/agents/<id>/plugins/<name>`` 的**符号链接**，指向插件库
里的真实目录（见 ``keeper/plugin/lib.py``）。
"""
from __future__ import annotations

import logging
import shutil
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from keeper.store import AgentLLMBinding, LLMProfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manage/agents", tags=["agents"])

_AGENT_STATUS = ("active", "disabled", "archived")


async def _set_loaded(agent_id: str, loaded: bool) -> None:
    """持久化本 agent 的本地装载态（决定重启后要不要自动装配）。"""
    from sqlalchemy import select

    from keeper.store import Agent as AgentRow, get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(select(AgentRow).where(AgentRow.id == agent_id))
        ).scalars().first()
        if row is None:
            return
        row.loaded = loaded
        await db.commit()


def _purge_agent_resources(agent_id: str) -> bool:
    """删掉该 agent 的下载目录 ``~/.keeper/agents/<agent_id>``。

    里面只是从平台拉下来的资源代码（mcp / skill / tool），删掉不影响配置——
    再次装载时 ``_fetch_agent_resources`` 会重新下载，所以可以安全清理。
    工作区目录（``~/.keeper/workspace/user/<name>``）**不动**：那里可能存着用户自己的文件。
    """
    from keeper.agent.config import default_agent_resource_root

    root = default_agent_resource_root(agent_id)
    if not root.is_dir():
        return False
    shutil.rmtree(root, ignore_errors=True)
    logger.info("已删除 agent 资源目录: %s", root)
    return True


@router.post("/{agent_id}/load")
async def load_agent(agent_id: str) -> dict:
    """装载：从平台拉该 agent 的配置 → 落本地库 → 装配成实例并注册。

    平台改完配置再调一次，会覆盖本地缓存并重建实例（旧的先释放，避免资源泄漏）。
    装载成功后置 ``loaded=True``，之后重启会自动装配。

    **装载前查重名**：`agent.name`` 上有唯一约束，而平台分配的 id 和本地生成的
    id 是两套体系——本地先建了个叫 ``assistant`` 的 agent，平台上又同步下来一个
    同名的，两边 id 不同、约束就会在 INSERT 时炸成 500。这里提前拦住并说清楚
    撞的是谁，因为那种 500 从报错里完全看不出是重名。
    """
    from sqlalchemy import select

    from keeper.agent.keeper import (
        build_agent,
        get_keeper,
        register_keeper,
        unregister_keeper,
    )
    from keeper.plat import fetch_my_agents
    from keeper.plat import sync_agent

    agents = await fetch_my_agents()
    target = next((a for a in agents if a.get("id") == agent_id), None)
    if target is None:
        raise HTTPException(
            status_code=404, detail="平台上没有这个 agent，或它不在「我已添加」里"
        )

    logger.info(
        "装载开始 agent=%s 名称=%s 平台声明的插件=%s",
        agent_id,
        target.get("name"),
        [p.get("name") for p in (target.get("plugin") or []) if p],
    )

    await _assert_name_free(agent_id, target.get("name") or "")

    await sync_agent(target)

    await _fetch_platform_plugins(agent_id, target)

    from ..store import AgentCapabilityOverride, get_session_factory

    # 同步只写 agent.llm（inline），不会碰绑定表。所以"没指定模型"的 agent
    # 装过来仍然是没模型的 → build 失败、连概览都进不去。顺手绑第一个模型，
    # 一个预设都没有时保持为空（让 build 失败并给出可读原因）。
    async with get_session_factory()() as _db:
        await _ensure_default_llm(_db, agent_id, await _load_inline_llm(agent_id))
        await _db.commit()

    # 本地已停用的话，只把配置拉下来，不进运行时——否则「停用」会被这次装载
    # 悄悄撤销，用户会以为开关坏了
    async with get_session_factory()() as db:
        off = (
            await db.execute(
                select(AgentCapabilityOverride.ref_name).where(
                    AgentCapabilityOverride.kind == "agent",
                    AgentCapabilityOverride.ref_name == agent_id,
                    AgentCapabilityOverride.enabled.is_(False),
                )
            )
        ).scalars().first()
    if off is not None:
        return {
            "ok": True,
            "id": agent_id,
            "name": target.get("name"),
            "running": False,
            "reason": "配置已下载，但该 agent 在本地处于停用状态",
        }

    old = get_keeper(agent_id)
    if old is not None:
        await old.shutdown()
        unregister_keeper(agent_id)

    try:
        agent = await build_agent(agent_id=agent_id)
    except Exception as e:  # noqa: BLE001 缺模型 / 插件起不来都是常态
        # 配置已经落到本地了，只是没跑起来。返回 200 + 可读原因，而不是 500：
        # 前端要能显示"已装载但起不来"，用户补完模型/插件还能再点一次。
        logger.warning("agent %s 装载后无法启动: %s", agent_id, e)
        await _set_loaded(agent_id, True)
        return {
            "ok": True,
            "id": agent_id,
            "name": target.get("name"),
            "running": False,
            "reason": str(e),
        }
    register_keeper(agent_id, agent)
    # 标记已装载：下次重启自动装配（之前卸载过的话这里会重新打开）
    await _set_loaded(agent_id, True)
    logger.info("装载完成 agent=%s 名称=%s（已注册运行时实例）", agent_id, agent.name)
    return {"ok": True, "id": agent_id, "name": agent.name, "running": True}


@router.post("/{agent_id}/unload")
async def unload_agent(agent_id: str) -> dict:
    """卸载：释放实例 + 标记已卸载（重启不再装配）+ 清掉下载目录。

    只清「下载下来的资源代码」与运行时实例，**保留本地配置缓存**，
    所以再次点「装载」就能恢复（代码会重新下载）。
    """
    from keeper.agent.keeper import get_keeper, unregister_keeper

    agent = get_keeper(agent_id)
    if agent is not None:
        await agent.shutdown()
        unregister_keeper(agent_id)

    # 即使本进程里没这个实例（比如上次装配失败），也要把「已卸载」落库——
    # 否则重启时 build_all_agents 还会把它装回来。
    await _set_loaded(agent_id, False)
    removed_dir = _purge_agent_resources(agent_id)
    logger.info(
        "卸载完成 agent=%s 运行时实例=%s 删除目录=%s",
        agent_id,
        "已释放" if agent is not None else "本进程没有（仅清标记）",
        removed_dir or "（无）",
    )
    return {
        "ok": True,
        "id": agent_id,
        "unloaded": agent is not None,
        "removed_dir": removed_dir,
    }


@router.get("/{agent_id}/model")
async def get_agent_model(agent_id: str) -> Dict[str, Any]:
    """读取本 agent 当前绑定的模型预设（本地归属，独立于平台缓存）。"""
    from keeper.store import get_session_factory

    async with get_session_factory()() as db:
        binding = await db.get(AgentLLMBinding, agent_id)
        if not binding or not binding.llm_profile_id:
            return {"llm_profile_id": None, "profile_name": None}
        profile = await db.get(LLMProfile, binding.llm_profile_id)
        return {
            "llm_profile_id": binding.llm_profile_id,
            "profile_name": profile.name if profile else None,
        }


async def _load_inline_llm(agent_id: str) -> dict:
    """读该 agent 的 inline llm 段（config.yaml 的 llm 解析结果）。

    只用来判断"inline 里有没有 provider" —— 有的话优先级高于绑定表（见
    ``_resolve_llm``），没必要再绑一个。读不到 / 存坏了都当没有。
    """
    from sqlalchemy import select

    from keeper.store import Agent as AgentRow
    from keeper.store import get_session_factory

    async with get_session_factory()() as db:
        row = (
            await db.execute(select(AgentRow).where(AgentRow.id == agent_id))
        ).scalars().first()
    if row is None:
        return {}
    try:
        return json.loads(row.llm or "{}") or {}
    except Exception:  # noqa: BLE001
        return {}


async def _ensure_default_llm(db, agent_id: str, inline: Optional[dict]) -> Optional[str]:
    """给还没模型的 agent 绑**第一个**模型预设；一个都没有就保持为空。

    为什么要有兜底
    ------------
    没有模型时 ``build_agent`` 直接失败（NoLLMConfigError），agent 就进不了运行时、
    也不出现在概览里——用户看到的是"建了但用不了"，却不知道缺什么。而"配了 3 个
    模型、agent 一个都没绑"很常见（模型先配好、agent 后建）。所以建/装 agent 时
    顺手绑第一个，省掉一次往返。

    刻意**不覆盖**已有绑定，也不覆盖 inline（config.yaml 的 llm 段已解析出
    provider 的优先级更高，见 ``_resolve_llm``）。一个预设都没有时返回 None，
    保持"没模型"这个事实，不硬造一个错的。
    """
    from sqlalchemy import select

    from keeper.store import AgentLLMBinding as LLMBinding
    from keeper.store import LLMProfile as Profile

    if (inline or {}).get("provider"):
        return None
    binding = await db.get(LLMBinding, agent_id)
    if binding and binding.llm_profile_id:
        return binding.llm_profile_id
    first = (
        await db.execute(select(Profile.id).order_by(Profile.created_at, Profile.id))
    ).scalars().first()
    if first is None:
        return None
    if binding is None:
        db.add(LLMBinding(agent_id=agent_id, llm_profile_id=first))
    else:
        binding.llm_profile_id = first
    return first


@router.post("/{agent_id}/model")
async def set_agent_model(agent_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """绑定 / 解绑本 agent 的模型预设。

    ``llm_profile_id`` 为空（None / 空串）表示解绑，回落到全局默认（config.yaml 的 llm 段）。
    放在独立表而非 ``agent`` 列，是为了不和平台同步重写 agent 行时冲突。
    """
    llm_profile_id: Optional[str] = body.get("llm_profile_id") or None
    from keeper.store import get_session_factory

    async with get_session_factory()() as db:
        if llm_profile_id is not None:
            profile = await db.get(LLMProfile, llm_profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="模型预设不存在")
        binding = await db.get(AgentLLMBinding, agent_id)
        if binding is None:
            binding = AgentLLMBinding(agent_id=agent_id, llm_profile_id=llm_profile_id)
            db.add(binding)
        else:
            binding.llm_profile_id = llm_profile_id
        await db.commit()

    # 模型是 build 期读的：改了绑定不会自动反映到已建好的实例上。而"缺模型"
    # 恰恰是最常见的启动失败原因——用户补完模型期望它立刻能跑，所以这里顺带
    # 重装配。停用状态的 agent 只改库，不去启动它。
    running, reason = await _rebuild_if_enabled(agent_id)
    return {
        "ok": True,
        "agent_id": agent_id,
        "llm_profile_id": llm_profile_id,
        "running": running,
        "reason": reason,
    }


async def _rebuild_if_enabled(agent_id: str) -> tuple:
    """重建 agent 实例，返回 ``(是否在跑, 没跑的原因)``。

    模型、插件都是**装配期**读进来的，改完配置不重建等于没改。停用中的 agent 不
    重建（用户明确让它别跑）。

    build 失败**不抛异常**：缺模型、插件目录没了都会走到这里，用户该看到的是
    "配置已保存，但 agent 起不来 + 为什么"，而不是一个 500。
    """
    from sqlalchemy import select

    from keeper.agent.keeper import (
        build_agent,
        get_keeper,
        register_keeper,
        unregister_keeper,
    )
    from keeper.store import Agent as AgentRow
    from keeper.store import AgentCapabilityOverride, get_session_factory

    async with get_session_factory()() as db:
        row = (
            await db.execute(select(AgentRow).where(AgentRow.id == agent_id))
        ).scalars().first()
        if row is None:
            return False, "agent 不存在"
        off = (
            await db.execute(
                select(AgentCapabilityOverride.ref_name).where(
                    AgentCapabilityOverride.kind == "agent",
                    AgentCapabilityOverride.ref_name == agent_id,
                    AgentCapabilityOverride.enabled.is_(False),
                )
            )
        ).scalars().first()

    if off is not None:
        return False, "该 agent 在本地处于停用状态"

    live = get_keeper(agent_id)
    if live is not None:
        await live.shutdown()
        unregister_keeper(agent_id)
    try:
        agent = await build_agent(agent_id=agent_id)
    except Exception as e:  # noqa: BLE001 起不来是常态（缺模型等），要给可读原因
        logger.warning("agent %s 重装配失败: %s", agent_id, e)
        return False, str(e)
    register_keeper(agent_id, agent)
    return True, ""


# ──────────────────────────────────────────────────────────────────────────
# 本地 agent 的增删改
#
# 「本地创建」这条路不经过平台：画像、能力绑定、插件链接都落本地。已有的
# ``load`` 接口只接受平台上的 agent，所以本地 agent 不会被 ``sync_agent`` 的
# 「先删后插」覆盖掉绑定（见 keeper/plat/sync.py 的 _replace_bindings）。
# ──────────────────────────────────────────────────────────────────────────


async def _fetch_platform_plugins(agent_id: str, target: Dict[str, Any]) -> None:
    """把平台下发的插件**真下载**到 ``~/.keeper/agents/<id>/plugins/<name>`` 并固定。

    为什么平台 agent 不用链接
    --------------------------
    两类 agent 的**可变性**本来就不一样，不该共用一套机制：

    - **平台 agent**：配置归平台，插件也归平台（平台下发的名单 + source）。
      它的插件是不可变的、自包含的一份——直接把文件下载进来，版本就固定住了，
      不受本机插件库变动的影响。也不支持在本地改它的插件（改了下次同步会没）。
    - **本地 agent**：插件从本机 ``~/.keeper/plugins`` 里挑，用**符号链接**引用。
      库里改了所有引用它的 agent 立刻生效，这正是本地维护想要的效果。

    「固定」的具体含义
    ------------------
    已经在 agent 目录里的插件**不重复下载**（不管平台那边 main 分支动没动）。
    重新装载只会补缺失的，不会悄悄覆盖——否则每次点「重新装载」都可能拿到
    一份不同的代码，那样"版本"就无从谈起。

    下载失败只跳过并告警，不阻断装载：网络抖一下不该让整个 agent 装不上。
    """
    from ..plugin import agent_plugins_dir
    from ..plat.fetcher import FetchError, fetch_resource

    got: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []

    for pl in target.get("plugin") or []:
        pl = pl or {}
        name = pl.get("name")
        if not name:
            continue
        dest = agent_plugins_dir(agent_id) / name
        src = pl.get("source") or {}
        logger.info(
            "处理插件资产 agent=%s 插件=%s 目标=%s kind=%s url=%s",
            agent_id, name, dest, (src.get("kind") or "-"), src.get("url") or "-",
        )
        if dest.is_dir() and not dest.is_symlink():
            logger.info(
                "  插件 %s 已是固定副本，跳过下载（要重新拉取需先删目录 %s）", name, dest
            )
            skipped.append(name)  # 已经固定过了，不动它
            continue

        kind = (src.get("kind") or "").lower()
        if kind == "local":
            # 平台指明的就是本机路径（比如体积大、不便走网络的二进制），
            # 建链接即可，不复制
            from ..plugin import link_plugin

            from pathlib import Path as _P

            p = _P(src.get("url") or "").expanduser()
            if not p.is_dir():
                failed.append(f"{name}(本机路径不存在)")
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_symlink() or dest.exists():
                    dest.unlink()
                dest.symlink_to(p, target_is_directory=True)
                logger.info("  插件 %s 建成本机链接 -> %s", name, p)
                got.append(name)
            except OSError as e:
                logger.warning("  插件 %s 建链接失败：%s", name, e)
                failed.append(f"{name}({e})")
            continue

        if not src.get("url"):
            logger.warning("  插件 %s 平台没给 source.url，跳过", name)
            failed.append(f"{name}(平台没给 source)")
            continue
        try:
            await fetch_resource(src, dest)
            logger.info("  插件 %s 下载完成 -> %s", name, dest)
            got.append(name)
        except FetchError as e:
            logger.warning("插件 %s 下载失败，跳过：%s", name, e)
            failed.append(f"{name}({e})")
        except Exception as e:  # noqa: BLE001 下载器本身的意外也不该拖垮装载
            logger.warning("插件 %s 下载异常，跳过：%s", name, e)
            failed.append(f"{name}({e})")

    logger.info(
        "插件资产处理完毕 agent=%s | 成功=%s | 跳过=%s | 失败=%s",
        agent_id, got or "无", skipped or "无", failed or "无",
    )
    if failed:
        logger.warning(
            "agent %s 有插件没拿到（不影响其余能力，装载继续）: %s",
            agent_id, "；".join(failed),
        )


async def _assert_name_free(agent_id: str, name: str) -> None:
    """装载平台 agent 前查重名。

    ``agent.name`` 上有唯一约束。平台分配的 id 与本地生成的 ULID 是**两套体系**，
    所以完全可能出现「本地建了个 assistant，平台上又同步下来一个同名的」——
    id 不同、约束成立，INSERT 会在最内层炸成 500，而那种报错完全看不出是重名。
    这里提前拦住，并把撞的是谁说清楚（本地建的 / 平台同步下来的）。
    """
    name = (name or "").strip()
    if not name:
        return
    from sqlalchemy import select

    from keeper.store import Agent as AgentRow
    from keeper.store import get_session_factory

    async with get_session_factory()() as db:
        dup = (
            await db.execute(select(AgentRow).where(AgentRow.name == name))
        ).scalars().first()
    if dup is None or dup.id == agent_id:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"平台上这个 agent 叫「{name}」，但本机已经有一个同名的"
            f"（{'本地创建' if dup.origin == 'local' else '平台同步'}，id={dup.id}）。"
            f"请先把那个改名或删除，再装载。"
        ),
    )


class AgentCreateIn(BaseModel):
    """新建本地 agent。

    工作区给 ``workspace_space_id``（引用 ``user_space`` 一条记录）——**不要**让
    调用方自己传路径和只读：只读是**工作空间**的属性（``user_space.read_only``），
    让前端再手填一遍会出现两个来源打架（历史上就是这样：agent 画像上的
    ``workspace_read_only`` 默认 true，而用户空间是可写的，装配期读前者，
    把写类工具全丢了）。传 id，后端按同一条记录取路径与只读，只有一个真源。
    """

    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = None
    persona: str = ""
    status: str = "active"
    # 「无工作区」传空；否则给 user_space.id
    workspace_space_id: Optional[str] = None
    # 仅保留兼容（老调用方直接给路径）；新代码走 space_id。
    # **没有** workspace_read_only：只读是工作空间的属性，不该由调用方再填一份。
    workspace_kind: str = "none"
    workspace_path: Optional[str] = None
    # 插件的**库内目录名**或清单 name 都接受（用户在文件夹里看到的是目录名）
    plugins: List[str] = Field(default_factory=list)


class PluginsSetIn(BaseModel):
    """整体替换某个 agent 的插件勾选。"""

    plugins: List[str] = Field(default_factory=list)


def _resolve_plugins(names: List[str]) -> List[Any]:
    """把用户给的一串名字解析成插件库里的插件，任何一个找不到就报错。

    早失败比晚失败好：等到装载时才发现名字打错，症状是"agent 起来了但少一个
    工具"，排查成本高得多。
    """
    from keeper.plugin import find_plugin

    out: List[Any] = []
    missing: List[str] = []
    seen = set()
    for n in names:
        p = find_plugin((n or "").strip())
        if p is None:
            missing.append(n)
            continue
        key = p.dirname or p.name
        if key in seen:  # 同名重复勾选只算一次
            continue
        seen.add(key)
        out.append(p)
    if missing:
        raise HTTPException(
            status_code=400, detail=f"插件库里没有：{'、'.join(missing)}"
        )
    return out


def _env_gate(plugins: List[Any]) -> None:
    """按所有选中插件的环境要求做准入检查，不满足直接拒绝创建。

    刻意**拒绝**而不是警告：环境不满足的表现是子进程起不来、命令 not found，
    跟"插件坏了"长得很像，等真去调的时候根本想不起来是创建时就没验过。
    拒绝的成本是用户当场就知道缺什么。
    """
    from keeper.plugin import check_env_requirements

    deps = []
    for p in plugins:
        deps.extend(p.env_dependencies)
    if not deps:
        return
    ok, results = check_env_requirements(deps)
    if not ok:
        bad = "；".join(
            f"{r['kind']} 需要 {r['minVersion'] or '不限'}"
            f"~{r['maxVersion'] or '不限'}（本机：{'、'.join(r['installed']) or '未探测到'}）"
            for r in results
            if not r["satisfied"]
        )
        raise HTTPException(
            status_code=409, detail=f"本机环境不满足所选插件：{bad}"
        )


@router.get("")
async def list_agents(origin: Optional[str] = None) -> Dict[str, Any]:
    """列出 agent，可按来源过滤。

    **默认只返回本机创建的（``origin=local``）**：平台同步下来的行是平台的镜像，
    它们的配置所有权在平台，放进「本地 Agent」列表里只会和下面的平台列表重复
    显示同一个 agent，而且用户在那里改绑定会被下次 ``sync_agent`` 静默冲掉。
    要看全部传 ``?origin=all``（排查时有用）。

    每个 agent 带 ``missing_plugins``：勾了但链接不可用（库里目录被删了），
    装起来会少这些工具——不显式告诉 UI 的话，症状是「勾了却没这个工具」。
    """
    from sqlalchemy import and_, or_, select

    from keeper.plugin import agent_plugins_dir, linked_plugins
    from keeper.store import Agent as AgentRow
    from keeper.store import AgentCapabilityOverride, get_session_factory

    q = select(AgentRow).order_by(AgentRow.name)
    if origin in (None, "", "local"):
        # 默认只给本机创建的。平台镜像也塞进来会有两个后果：UI 上和平台列表
        # 显示同一个 agent 两遍；用户在这里改它的绑定会被下次 sync_agent 冲掉。
        q = q.where(AgentRow.origin == "local")
    elif origin == "platform":
        q = q.where(AgentRow.origin == "platform")
    elif origin == "all":
        # 「库存」= 本地创建的 ∪ **已装载的**平台 agent。
        # 卸载过的平台 agent 不该出现在这里：它的配置已经从本机清掉了，留一行
        # 只会让人以为还在。要再装回去去「智能体市场」——那边按这个字段决定
        # 显示「装载」还是「重新装载」。
        q = q.where(
            or_(
                AgentRow.origin == "local",
                and_(AgentRow.origin == "platform", AgentRow.loaded.is_not(False)),
            )
        )
    else:
        raise HTTPException(
            status_code=400, detail="origin 只能是 local / platform / all"
        )

    factory = get_session_factory()
    async with factory() as db:
        rows = (await db.execute(q)).scalars().all()
        # 本地停用清单（kind='agent'，ref_name 就是 agent_id）——缺省启用
        disabled = set(
            (
                await db.execute(
                    select(AgentCapabilityOverride.ref_name).where(
                        AgentCapabilityOverride.kind == "agent",
                        AgentCapabilityOverride.enabled.is_(False),
                    )
                )
            ).scalars().all()
        )
    out = []
    for r in rows:
        # 绑定 = 链接目录（含悬空：勾过但库里的目录没了，要让用户看见）
        linked_dir = agent_plugins_dir(r.id)
        want = (
            sorted(p.name for p in linked_dir.iterdir())
            if linked_dir.is_dir()
            else []
        )
        have = set(linked_plugins(r.id))
        out.append(
            {
                "id": r.id,
                "name": r.name,
                "origin": r.origin or "platform",
                "description": r.description,
                "status": r.status,
                # 本地启用开关（唯一开关，缺省开）。平台同步不碰这张 override
                # 表，所以停用不会在下次装载时被悄悄撤销。
                "enabled": r.id not in disabled,
                "loaded": bool(r.loaded),
                "persona": r.persona,
                "workspace_kind": r.workspace_kind,
                "workspace_path": r.workspace_path,
                # 只读不在这里给：它属于工作空间（user_space.read_only）。
                # 想知道某个 agent 能不能写，问它绑定的那个空间，或看装配日志。
                "plugins": want,
                "missing_plugins": sorted(set(want) - have),
            }
        )
    return {"agents": out}


@router.post("")
async def create_agent(body: AgentCreateIn) -> Dict[str, Any]:
    """创建本地 agent：落库 + 落插件绑定 + 建插件链接 + **立刻装配成实例**。

    **创建即装载。** 「装载」是从平台拉配置（`load_agent`）的叫法，只对平台 agent
    有意义；本地 agent 根本没有平台配置可拉，所以创建完就直接进运行时——
    否则它不会出现在概览里，用户还得再点一次装载，而那一层对它是多余的。

    顺序是「先建链接 → 落库 → 装配」，任一步失败都**回滚干净**：
    链接是纯文件操作、可以撤；行删掉；已经起来的实例 shutdown + 注销。
    不回滚的话库里会留下一个指向半成品链接的 agent，比直接报错难查得多。

    停用 / 启用走 ``PATCH /manage/agents/{id}``（改 ``status``），不用卸载。
    """
    from sqlalchemy import select

    from keeper.plugin import link_plugin
    from keeper.store import Agent as AgentRow
    from keeper.store import get_session_factory
    from keeper.store.models import new_id

    name = body.name.strip()
    if body.status not in _AGENT_STATUS:
        raise HTTPException(
            status_code=400, detail=f"status 只能是 {'/'.join(_AGENT_STATUS)}"
        )
    plugins = _resolve_plugins(body.plugins)
    _env_gate(plugins)  # 环境不满足 → 拒绝，不留半成品

    factory = get_session_factory()
    async with factory() as db:
        # 工作区：给了 space_id 就按用户空间那条记录取路径与只读，别让调用方
        # 自己传——两个来源必须是一个，否则会出现「用户空间是可写的、agent
        # 画像写着只读，装配期按后者把写类工具丢了」这种静默失效。
        ws_kind, ws_path = "none", None
        if body.workspace_space_id:
            from keeper.store import UserSpace as UserSpaceRow

            space = await db.get(UserSpaceRow, body.workspace_space_id)
            if space is None:
                names = (
                    await db.execute(
                        select(UserSpaceRow.name).where(
                            UserSpaceRow.id == body.workspace_space_id
                        )
                    )
                ).scalars().first()
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"用户空间不存在：{body.workspace_space_id}"
                        + (f"（{names} 已被删除）" if names else "")
                    ),
                )
            ws_kind, ws_path = "local", space.path
            logger.info(
                "创建 agent %s：工作区=%s（user_space「%s」read_only=%s，"
                "只读标志存在那条记录上，agent 侧不存）",
                name, ws_path, space.name, bool(space.read_only),
            )
        elif body.workspace_path:
            # 兼容旧调用：直接给了路径。**要求它在「用户空间」里登记过**——
            # 只读的判断依据是 user_space.read_only，没登记就没有依据，而静默
            # 按可写处理等于凭空给写权限，按只读处理又会让人以为「这目录不能写」。
            # 两个都不行，所以明确报错。
            from keeper.store import UserSpace as UserSpaceRow

            space = (
                await db.execute(
                    select(UserSpaceRow).where(UserSpaceRow.path == body.workspace_path)
                )
            ).scalars().first()
            if space is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"工作区目录未在「用户空间」登记：{body.workspace_path}。"
                        "工作区必须先登记才能绑定（只读与否由那条记录决定）。"
                        "请到「用户空间」补一条，或改用创建表单里的下拉框选择。"
                    ),
                )
            ws_kind, ws_path = "local", space.path

        dup = (
            await db.execute(select(AgentRow).where(AgentRow.name == name))
        ).scalars().first()
        if dup is not None:
            raise HTTPException(status_code=409, detail=f"同名 agent 已存在：{name}")

        agent_id = new_id()
        created: List[str] = []
        try:
            for p in plugins:
                link = link_plugin(agent_id, p)
                created.append(link.name)
        except Exception as e:  # noqa: BLE001 链接失败 → 回滚已建的，不留残链
            from keeper.plugin import unlink_plugin

            for n in created:
                unlink_plugin(agent_id, n)
            raise HTTPException(status_code=500, detail=f"建立插件链接失败：{e}") from e

        row = AgentRow(
            id=agent_id,
            name=name,
            origin="local",  # 本机创建；平台同步来的行恒为 platform
            description=body.description,
            persona=body.persona,
            status=body.status,
            workspace_kind=ws_kind,
            workspace_path=ws_path,
            loaded=True,  # 创建即进运行时（见 docstring）；status 才是启停开关
        )
        db.add(row)
        # 插件绑定 = 链接目录里有什么，不落表。链接在 _ensure_plugin_links /
        # 创建前就已经建好了（见上面的 link_plugin 循环）。
        # 没指定模型就绑第一个：否则 build 必然失败（NoLLMConfigError），
        # 用户要再手动配一次才能用。一个模型都没有时保持为空。
        await _ensure_default_llm(db, agent_id, await _load_inline_llm(agent_id))
        await db.commit()

    # 装配成实例（创建即装载，且默认启用——本地开关是唯一开关，缺省开）。
    # 失败要连库带链接一起撤：留一个 build 不起来的 agent 在库里，用户看到的
    # 会是"建好了但用不了"，比创建失败难查。
    from keeper.agent.keeper import build_agent, register_keeper

    try:
        agent = await build_agent(agent_id=agent_id)
    except Exception as e:  # noqa: BLE001 缺模型 / 插件起不来都是常态
        # **不回滚**：配置已经落库、插件链接也建好了，把它删掉等于让用户重填一遍。
        # 缺模型是最常见的失败原因（一个模型预设都没有），而补完模型就能跑起来
        # —— 保留下来并说清为什么没跑，前端也能提示"去配个模型"。
        logger.warning("agent %s 创建后无法启动: %s", agent_id, e)
        return {
            "ok": True,
            "id": agent_id,
            "name": name,
            "enabled": True,
            "running": False,
            "reason": str(e),
            "plugins": created,
        }
    register_keeper(agent_id, agent)

    return {
        "ok": True,
        "id": agent_id,
        "name": name,
        "enabled": True,
        "running": True,
        "plugins": created,
    }


class AgentUpdateIn(BaseModel):
    """本地启用 / 停用。对**平台 agent 和本地 agent 都适用**。"""

    enabled: bool


@router.patch("/{agent_id}/enable")
async def set_agent_enabled(agent_id: str, body: AgentUpdateIn) -> Dict[str, Any]:
    """本地启用 / 停用某个 agent —— 唯一决定"它跑不跑"的开关。

    **为什么落在 AgentCapabilityOverride 而不是改 agent.status**
    ---------------------------------------------------------
    status 是**配置**：平台 agent 的 status 归平台管（_upsert_agent_row 每次同步
    都会写它），本地 agent 的由创建表单定。而"我现在不想跑它"是**运行时意图**
    —— 写进 status 的话，下一次点「装载」同步回来就没了，用户会发现停用悄悄
    失效。那张 override 表平台同步从来不碰，所以本地开关能活下来。

    停用 = 释放实例 + 注销（立刻从概览消失，重启也不会被 build_all_agents 装
    回来）；启用 = 撤掉停用标记，按 status 决定要不要 build。
    """
    from sqlalchemy import select

    from keeper.agent.keeper import (
        build_agent,
        get_keeper,
        register_keeper,
        unregister_keeper,
    )
    from keeper.store import Agent as AgentRow
    from keeper.store import AgentCapabilityOverride, get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(select(AgentRow).where(AgentRow.id == agent_id))
        ).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="agent 不存在")
        ov = (
            await db.execute(
                select(AgentCapabilityOverride).where(
                    AgentCapabilityOverride.agent_id == agent_id,
                    AgentCapabilityOverride.kind == "agent",
                    AgentCapabilityOverride.ref_name == agent_id,
                )
            )
        ).scalars().first()

        if not body.enabled:
            if ov is None:
                db.add(
                    AgentCapabilityOverride(
                        agent_id=agent_id,
                        kind="agent",
                        ref_name=agent_id,
                        enabled=False,
                    )
                )
            else:
                ov.enabled = False
            await db.commit()
        else:
            # 启用 = 撤掉停用标记，回到 status 的安排
            if ov is not None:
                await db.delete(ov)
                await db.commit()
            # 不看 status：本地启用开关是唯一开关，缺省启用。
            # 平台把 agent 标成 disabled 也不阻止它在本地跑——否则会出现
            #「装载了却跑不起来」，而 status 在界面上已经不再展示。

    live = get_keeper(agent_id)
    if not body.enabled:
        if live is not None:
            await live.shutdown()
            unregister_keeper(agent_id)
        return {"ok": True, "id": agent_id, "enabled": False, "running": False}

    if live is not None:
        await live.shutdown()
        unregister_keeper(agent_id)
    agent = await build_agent(agent_id=agent_id)
    register_keeper(agent_id, agent)
    return {"ok": True, "id": agent_id, "enabled": True, "running": True}


@router.patch("/{agent_id}/plugins")
async def set_agent_plugins(agent_id: str, body: PluginsSetIn) -> Dict[str, Any]:
    """整体替换该 agent 的插件勾选（勾上→建链接，取消→删链接）。

    "整体替换"而不是增删：UI 上是一个多选框，提交的就是最终集合；
    做成增量反而要处理"到底该加还是该减"的歧义。
    """
    from sqlalchemy import select

    from keeper.plugin import link_plugin, linked_plugins, unlink_plugin
    from keeper.store import Agent as AgentRow
    from keeper.store import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(select(AgentRow).where(AgentRow.id == agent_id))
        ).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="agent 不存在")
        wanted = _resolve_plugins(body.plugins)
        _env_gate(wanted)
        target = {p.dirname or p.name: p for p in wanted}
        current = set(linked_plugins(agent_id))
        logger.info(
            "设置插件绑定 agent=%s 原来=%s 目标=%s",
            agent_id, sorted(current) or "无", sorted(target) or "无",
        )

        for name in sorted(current - set(target)):
            unlink_plugin(agent_id, name)
        for name, p in target.items():
            link_plugin(agent_id, p)  # 幂等：已存在的会被重建以跟上库里的变化

        # 绑定就是链接：上面已经 unlink / link 过了，不需要再写表

    # 工具是在**装配阶段**读进来的，改绑定不会反映到已建好的实例上，所以重建。
    # 只在该 agent 真的在跑时重建（停用状态的没必要动它）。
    restarted = False
    from keeper.agent.keeper import get_keeper

    if get_keeper(agent_id) is not None:
        from keeper.agent.keeper import (
            build_agent,
            register_keeper,
            unregister_keeper,
        )
        from keeper.agent.keeper import (
            build_agent,
            get_keeper,
            register_keeper,
            unregister_keeper,
        )

        live = get_keeper(agent_id)
        if live is not None:
            await live.shutdown()
            unregister_keeper(agent_id)
            restarted = True
        agent = await build_agent(agent_id=agent_id)
        register_keeper(agent_id, agent)
        logger.info(
            "插件绑定变更后已重建实例 agent=%s 生效工具=%s",
            agent_id,
            sorted(
                getattr(t, "name", "?")
                for t in (await agent._collect_tools()).all()
            ),
        )
    return {
        "ok": True,
        "agent_id": agent_id,
        "plugins": sorted(target),
        "restarted": restarted,
    }


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str) -> Dict[str, Any]:
    """删除本地 agent：卸实例 → 删行 / 绑定 → 清资源目录（含插件链接）。

    先卸实例再删行：反过来的话实例还活着、它的插件链接已经被 rmtree 掉了，
    正在跑的工具会在半路找不到文件。工作区目录不动——那里可能有用户自己的
    文件，跟 agent 配置无关。
    """
    from sqlalchemy import select

    from keeper.agent.keeper import get_keeper, unregister_keeper
    from keeper.store import Agent as AgentRow
    from keeper.store import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(select(AgentRow).where(AgentRow.id == agent_id))
        ).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="agent 不存在")

        live = get_keeper(agent_id)
        if live is not None:
            await live.shutdown()
            unregister_keeper(agent_id)

        # 插件链接随整个 agent 资源目录一起删（_purge_agent_resources），
        # 不需要逐条删绑定
        await db.delete(row)
        await db.commit()

    removed_dir = _purge_agent_resources(agent_id)
    return {"ok": True, "id": agent_id, "removed_dir": removed_dir}
