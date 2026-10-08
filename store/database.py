"""数据库引擎与初始化（SQLite + aiosqlite，异步、不阻塞事件循环）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import logging

from .models import Base, UserSpace, new_id, USER_SPACE_DEFAULT_NAME

logger = logging.getLogger(__name__)

# 默认库文件位置：keeper/data/keeper.db
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "keeper.db"

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def db_url(path: str | Path | None = None) -> str:
    """拼连接串；必要时创建目录。"""
    p = Path(path) if path else DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{p}"


def get_engine(url: str | None = None) -> AsyncEngine:
    """取全局引擎（首次调用时创建）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            url or db_url(),
            echo=False,
            # SQLite 的 busy_timeout 默认为 0：并发写撞上就立刻抛
            # 「database is locked」，不等待。keeper 是单进程多 agent，另有 A2A
            # 入站、任务执行、记忆与用量记账各自开 session 写库，写竞争是常态，
            # 这里给一个等待窗口，让短暂冲突退化为排队而不是失败。
            connect_args={"check_same_thread": False, "timeout": _BUSY_TIMEOUT_SEC},
        )
        _install_sqlite_pragmas(_engine)
    return _engine


# SQLite 写锁等待窗口（秒）。默认 0 = 撞锁立刻失败。
_BUSY_TIMEOUT_SEC = 15.0


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """给每个新连接设 WAL 之类的 PRAGMA。

    WAL 的关键收益是**读写不再互斥**：回滚日志模式下只要有一个长事务在写，
    所有读也会被一起阻塞；而 keeper 天然是「长事务 + 大量读」的组合——产出
    计划时要跑一整轮 LLM（几十秒起，期间握着写锁），同一会话的 ReAct、
    记忆、llm_calls 记账又要读写。默认模式下这种组合很容易互相拖死。

    ``journal_mode=WAL`` 由 SQLite 自己写进库文件头，设一次长期生效；
    ``synchronous=NORMAL`` 是 WAL 下的常规取舍（崩溃最多丢最后几个事务，
    换来的是数量级的写入性能差距）。

    两条 PRAGMA 都**失败不致命**：库可能放在只读挂载、或不支持共享内存的
    网络文件系统上，那里 WAL 根本设不上。此时只降级性能、不阻断启动，
    写竞争交给 ``busy_timeout`` 顶——自托管场景下库路径是用户说了算的。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
                try:
                    cur.execute(pragma)
                except Exception:  # noqa: BLE001
                    logger.warning("设置 %s 失败，继续沿用默认", pragma, exc_info=True)
        finally:
            cur.close()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """取全局会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


# 早期为多进程部署 + 按需装配回收计划预留的「部署 / 生命周期」列，单进程 keeper
# 从未读写，已在 models.Agent 中删除其映射；下方统一清理存量库里的这些列。
_AGENT_LEGACY_COLUMNS = [
    # 只读标志搬到了 user_space.read_only（工作空间自己的属性）。这列留在 agent
    # 上时两边各判各的，且每次平台同步都会被覆盖 —— 见 models.Agent 的注释。
    "workspace_read_only",
    "deploy_mode",
    "idle_timeout",
    "last_accessed_at",
    "host",
    "port",
    "entry_url",
    "deploy_status",
    "deploy_error",
    "last_deployed_at",
    "ui_kind",
]


async def _drop_agent_legacy_columns(conn) -> None:
    """清理 agent 表上已废弃的列（历史遗留，模型已不映射）。

    - 列不存在则跳过（幂等）；
    - SQLite < 3.35 不支持 DROP COLUMN 时只告警不操作，避免启动失败；
    - 这些列从未被业务写入，DROP 不会丢失任何真实数据。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent)")).fetchall()
    existing = {r[1] for r in rows}
    for col in _AGENT_LEGACY_COLUMNS:
        if col not in existing:
            continue
        try:
            await conn.exec_driver_sql(f"ALTER TABLE agent DROP COLUMN {col}")
            logger.info("agent 表已删除废弃列：%s", col)
        except Exception as e:
            logger.warning("agent 表删除废弃列 %s 失败（已忽略）：%s", col, e)


async def _migrate_agent_loaded(conn) -> None:
    """agent 补 ``loaded`` 列：本地装载态（卸载后重启不再自动装配）。

    存量行一律回填 1（True）——升级前本来在跑的 agent，不能因为加了列就消失。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent)")).fetchall()
    existing = {r[1] for r in rows}
    if "loaded" not in existing:
        await conn.exec_driver_sql("ALTER TABLE agent ADD COLUMN loaded BOOLEAN")
    await conn.exec_driver_sql("UPDATE agent SET loaded = 1 WHERE loaded IS NULL")


async def _migrate_agent_origin(conn) -> None:
    """agent 补 ``origin`` 列：区分「平台同步来的」和「本机创建的」（存量库升级）。

    **为什么必须有这个字段**
    ----------------------
    本地表里的 agent 一直混着两种来源：平台同步下来的**副本**，和本机创建的。
    两者 id 都由平台/ULID 生成，**没有字段能区分它们**，于是出了两个问题：

    1. UI 上同一个 agent 显示两遍（本地列表 + 平台列表），因为无法判断某行
       是不是平台的镜像；
    2. 更糟的是：在本地改了平台 agent 的绑定（比如勾插件），下次点「装载」时
       ``sync_agent`` 的 ``_replace_bindings`` 是**先删后插**，本地改动直接被冲
       掉——而且没有任何提示。

    存量行一律回填 ``platform``：加这一列之前，本地表里的每一行都是
    ``sync_agent`` 写进来的（本地创建 agent 是这之后才有的能力），所以这个
    回填值对存量数据是准确的。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent)")).fetchall()
    if not rows:
        return  # 表还没建（create_all 会按 ORM 建全的）
    existing = {r[1] for r in rows}
    if "origin" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE agent ADD COLUMN origin VARCHAR(16) DEFAULT 'platform'"
        )
    await conn.exec_driver_sql("UPDATE agent SET origin = 'platform' WHERE origin IS NULL")


async def _migrate_drop_plugin_tables(conn) -> None:
    """删掉 ``plugin`` / ``agent_binding`` 两张废表。

    为什么能删
    ---------
    两张表的职责都已经被**文件系统**取代了：

    - ``plugin``（插件清单快照）：来源是平台下发。插件改成"库里一个目录"之后，
      真源是 ``keeper-plugin.json`` 本身，没有远程可同步，表就只剩历史垃圾。
    - ``agent_binding``（哪个 agent 绑了哪个插件）：等价于
      ``~/.keeper/agents/<id>/plugins/<name>`` 这个符号链接。列目录就是读绑定，
      链接有效就等于插件还在。

    两张表里**带不走的字段实际都没在用**：查过真实数据，binding 的 kind 全是
    ``plugin``（skill / mcp 根本没走这张表），``settings`` 非空 0 条，``enabled=0``
    0 条。``settings`` 将来真需要（比如给 MCP server 传 env）再单独引一张极小的
    ``plugin_settings`` 表，比现在为一个空字段养着整张表划算。

    DROP TABLE 在 SQLite 里是事务性的，且这里本来就在迁移流程里，失败会回滚。
    """
    for table in ("agent_binding", "plugin"):
        rows = (
            await conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
        ).fetchall()
        if not rows:
            continue
        await conn.exec_driver_sql(f"DROP TABLE {table}")


async def _migrate_chat_session_agent_id(conn) -> None:
    """会话表补 agent_id 列：多 agent 下按它隔离会话（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "agent_id" not in existing:
        await conn.exec_driver_sql("ALTER TABLE chat_sessions ADD COLUMN agent_id TEXT")


async def _migrate_agent_message_task_id(conn) -> None:
    """agent_messages 补 task_id 列：跨端消息归属到 A2A taskId（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent_messages)")).fetchall()
    existing = {r[1] for r in rows}
    if "task_id" not in existing:
        await conn.exec_driver_sql("ALTER TABLE agent_messages ADD COLUMN task_id TEXT")


async def _migrate_thread_last_task_id(conn) -> None:
    """threads 补 last_task_id 列：记录对端最近一次返回的 A2A taskId（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(threads)")).fetchall()
    existing = {r[1] for r in rows}
    if "last_task_id" not in existing:
        await conn.exec_driver_sql("ALTER TABLE threads ADD COLUMN last_task_id TEXT")


async def _migrate_thread_context_id(conn) -> None:
    """threads 补 context_id 列：协议 context 与 thread.id 解耦（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(threads)")).fetchall()
    existing = {r[1] for r in rows}
    if "context_id" not in existing:
        await conn.exec_driver_sql("ALTER TABLE threads ADD COLUMN context_id TEXT")


async def _migrate_thread_last_task_state(conn) -> None:
    """threads 补 last_task_state 列：标记对端上次任务是否仍在等输入（存量库升级）。

    存量行一律回填 NULL——升级前无法判断上次任务状态， safest 默认当成
    「已完成」，下一次提问走新问题分支（开新任务），避免误恢复旧任务导致
    返回陈旧答案。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(threads)")).fetchall()
    existing = {r[1] for r in rows}
    if "last_task_state" not in existing:
        await conn.exec_driver_sql("ALTER TABLE threads ADD COLUMN last_task_state TEXT")


async def _migrate_chat_session_context_id(conn) -> None:
    """chat_sessions 补 context_id 列：协议 context 与 session.id 解耦（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "context_id" not in existing:
        await conn.exec_driver_sql("ALTER TABLE chat_sessions ADD COLUMN context_id TEXT")


async def _migrate_chat_session_user_space(conn) -> None:
    """chat_sessions 补 user_space_id 列：会话绑定用户空间（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "user_space_id" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN user_space_id TEXT"
        )


async def _migrate_session_message_artifacts(conn) -> None:
    """session_messages 补 artifacts 列：承载本轮 agent 产出的文件引用（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(session_messages)")).fetchall()
    existing = {r[1] for r in rows}
    if "artifacts" not in existing:
        await conn.exec_driver_sql("ALTER TABLE session_messages ADD COLUMN artifacts TEXT")


async def _migrate_agent_peer_source(conn) -> None:
    """agent_peers 补 source / trust_env 列：区分本进程内 / 外部对端（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent_peers)")).fetchall()
    existing = {r[1] for r in rows}
    if "source" not in existing:
        await conn.exec_driver_sql("ALTER TABLE agent_peers ADD COLUMN source TEXT")
    if "trust_env" not in existing:
        await conn.exec_driver_sql("ALTER TABLE agent_peers ADD COLUMN trust_env BOOLEAN")


async def _migrate_session_message_artifact_paths(conn) -> None:
    """旧产物 ``path`` 规范化（存量库升级，幂等）。

    早期版本把 path 存成：
    - 相对路径（如 ``xian_travel.html``）→ 新版按 CWD 解析会 404；
    - 裸绝对路径（如 ``/abs/x.html``）→ 可用但缺 ``file://`` scheme，风格不统一。

    这里把相对路径**按会话绑定的工作空间根**还原为 ``file://`` 绝对路径
    （回落系统默认用户空间），裸绝对路径补 ``file://`` 前缀；已是 ``file://`` 的跳过。
    """
    default_rows = (
        await conn.exec_driver_sql(
            "SELECT path FROM user_space WHERE name = ?", (USER_SPACE_DEFAULT_NAME,)
        )
    ).fetchall()
    default_root = os.path.expanduser(default_rows[0][0]) if default_rows else None

    rows = (
        await conn.exec_driver_sql(
            "SELECT sm.id, sm.artifacts, us.path "
            "FROM session_messages sm "
            "LEFT JOIN chat_sessions cs ON cs.id = sm.chat_session_id "
            "LEFT JOIN user_space us ON us.id = cs.user_space_id "
            "WHERE sm.artifacts IS NOT NULL AND sm.artifacts != ''"
        )
    ).fetchall()

    for msg_id, artifacts_json, us_path in rows:
        try:
            arts = json.loads(artifacts_json)
        except Exception:
            continue
        if not isinstance(arts, list):
            continue
        changed = False
        root = os.path.expanduser(us_path) if us_path else default_root
        for a in arts:
            p = a.get("path")
            if not p or p.startswith("file://"):
                continue
            if os.path.isabs(p):
                a["path"] = "file://" + p
                changed = True
            elif root:
                # 相对路径：按会话工作空间根还原；realpath 归一化软链（如 default→真实目录）
                try:
                    abs_p = os.path.realpath(os.path.join(root, p))
                except Exception:
                    continue
                a["path"] = "file://" + abs_p
                changed = True
            # 既非绝对、又无可用 root：无法还原，保持原样（避免误写错误路径）
        if changed:
            await conn.exec_driver_sql(
                "UPDATE session_messages SET artifacts = ? WHERE id = ?",
                (json.dumps(arts, ensure_ascii=False), msg_id),
            )


async def _migrate_strip_tmp_artifacts(conn) -> None:
    """剔除历史上误登记为产物的 ``@tmp/`` 临时文件（存量库升级，幂等）。

    ``@tmp/`` 是会话私有临时目录：临时测试脚本、补丁脚本、同一文件的中间版本。这些
    是**过程**不是交付，不该出现在会话里（用户点开多半已 404——收尾时会被清理）。
    ``_capture_artifacts`` 现在在源头就不登记它们，这里只清理存量。

    两种历史形态都要清：
    - ``file://<用户工作空间>/@tmp/x``——早期只按工作空间根解析，路径根本不存在；
    - ``file://<...>/session/<sid>/<aid>/x``——修好后能定位到真实临时目录的。

    只删 ``artifacts`` 里的**引用**，不删磁盘文件；写入动作本身仍完整留在
    ``react_steps``（要追溯过程查那里），所以这一步是可逆的。

    ``agent.config`` / ``tools.builtin.sandbox`` 反向依赖 ``store``，这里惰性 import。
    """
    from ..agent.config import WORKSPACE_ROOT
    from ..tools.builtin.sandbox import TMP_PREFIX

    session_root = str(Path(WORKSPACE_ROOT) / "session")
    # 两种形态都要捞出来：路径里带 `@tmp/` 段的，以及已被修成真实 agent_space
    # 绝对路径（`.../workspace/session/<sid>/<aid>/`）的——后者字面量里已经没有
    # `@tmp` 了，只按 `@tmp` 过滤会漏掉它们。
    rows = (
        await conn.exec_driver_sql(
            "SELECT id, artifacts FROM session_messages "
            "WHERE artifacts IS NOT NULL AND artifacts != '' "
            "AND (artifacts LIKE ? OR artifacts LIKE ?)",
            ("%" + "/" + TMP_PREFIX + "/%", "%" + session_root + "/%"),
        )
    ).fetchall()

    for msg_id, artifacts_json in rows:
        try:
            arts = json.loads(artifacts_json)
        except Exception:
            continue
        if not isinstance(arts, list):
            continue
        kept = []
        for a in arts:
            if isinstance(a, dict):
                p = a.get("path") or ""
                # 形态一：路径里带 @tmp/ 段；形态二：落在 workspace/session/ 下（即 agent_space）
                # 用子串判断而非 startswith，兼容早期没带 file:// scheme 的裸绝对路径
                is_tmp = (
                    ("/" + TMP_PREFIX + "/") in p
                    or (session_root + "/") in p
                )
                if is_tmp:
                    continue
            kept.append(a)
        if len(kept) == len(arts):
            continue
        await conn.exec_driver_sql(
            "UPDATE session_messages SET artifacts = ? WHERE id = ?",
            (json.dumps(kept, ensure_ascii=False) if kept else None, msg_id),
        )


async def _migrate_task_item_rounds(conn) -> None:
    """task_items 补 rounds 列：计划点已执行的对话轮数（存量库升级）。

    task_items 是后加的表，create_all 建出来时本就带这一列；这里只为
    「先建过旧版表」的库兜底，保证单点轮数上限能生效。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(task_items)")).fetchall()
    existing = {r[1] for r in rows}
    if "rounds" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE task_items ADD COLUMN rounds INTEGER DEFAULT 0"
        )


async def _migrate_task_paused(conn) -> None:
    """tasks 补 paused 列：按 step 暂停的执行开关（存量库升级）。

    存量任务一律回填 0（未暂停），避免升级后任务被误判为暂停中。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(tasks)")).fetchall()
    existing = {r[1] for r in rows}
    if "paused" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN paused BOOLEAN DEFAULT 0"
        )
    await conn.exec_driver_sql("UPDATE tasks SET paused = 0 WHERE paused IS NULL")


async def _migrate_task_review_fields(conn) -> None:
    """tasks 补 review_feedback / review_reject_count 列：验收闭环（存量库升级）。

    - ``review_feedback``：最新一条验收意见（打回反馈 / 通过备注）；
    - ``review_reject_count``：验收打回次数，超出上限转 failed（doc §七）。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(tasks)")).fetchall()
    existing = {r[1] for r in rows}
    if "review_feedback" not in existing:
        await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN review_feedback TEXT")
    if "review_reject_count" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN review_reject_count INTEGER DEFAULT 0"
        )


async def _migrate_chat_session_kind(conn) -> None:
    """chat_sessions 补 kind 列：区分普通会话与任务绑定会话（存量库升级）。

    存量行一律回填 chat（普通），任务会话在建任务时显式置 task。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "kind" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN kind TEXT DEFAULT 'chat'"
        )
    await conn.exec_driver_sql(
        "UPDATE chat_sessions SET kind = 'chat' WHERE kind IS NULL"
    )


async def _seed_default_user_space(conn) -> None:
    """播种系统默认用户空间 ``~/.keeper/workspace/user/default``（幂等）。

    用户不手动新建 / 切换时，这就是 agent 干活的主目录。名称被保留，
    管理页与 API 都禁止删除（除非手动改库）。由于 agent.config 反向依赖 store，
    这里直接拼路径，不与 ``WORKSPACE_ROOT`` 耦合，避免循环导入。
    """
    from datetime import datetime, timezone
    from sqlalchemy import text

    row = (
        await conn.execute(
            text("SELECT id FROM user_space WHERE name = :n"),
            {"n": USER_SPACE_DEFAULT_NAME},
        )
    ).first()
    if row is not None:
        return
    default_path = Path.home() / ".keeper" / "workspace" / "user" / USER_SPACE_DEFAULT_NAME
    default_path.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    await conn.execute(
        UserSpace.__table__.insert().values(
            id=new_id(),
            name=USER_SPACE_DEFAULT_NAME,
            path=str(default_path),
            read_only=False,
            description="系统默认工作空间；不可删除或编辑，除非手动改库。",
            created_at=now,
            updated_at=now,
        )
    )


async def _drop_legacy_resource_tables(conn) -> None:
    """下线旧三类资源表：mcp / skill / tool 已统一进 ``plugin``。

    用户已确认不迁移历史数据，所以这里是真删表。

    原来这里还会清掉指向旧类型的绑定（``DELETE FROM agent_binding WHERE
    kind != 'plugin'``）。**那段已经删了**：``agent_binding`` 整张表现在也下线了
    （绑定关系由 ``~/.keeper/agents/<id>/plugins/<name>`` 这个符号链接表达，
    见 ``_migrate_drop_plugin_tables``），留着会在这张表被 drop 之后报
    "no such table"。
    """
    for table in ("mcp", "skill", "tool"):
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")


async def _migrate_llm_profile_prices(conn) -> None:
    """llm_profiles 补价格列：成本换算单价（存量库升级）。

    价格挂在**模型预设**上（同一 model_name 可能有多个预设、价格亦不同）。
    存量行一律 NULL——未配价格的预设算不出成本，展示为「—」，不瞎算。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(llm_profiles)")).fetchall()
    existing = {r[1] for r in rows}
    for col, decl in (
        ("input_price_per_1m", "FLOAT"),
        ("cached_price_per_1m", "FLOAT"),
        ("output_price_per_1m", "FLOAT"),
        ("currency", "VARCHAR(8) NOT NULL DEFAULT 'CNY'"),
        ("context_limit", "INTEGER"),
    ):
        if col not in existing:
            await conn.exec_driver_sql(
                f"ALTER TABLE llm_profiles ADD COLUMN {col} {decl}"
            )


async def _migrate_capability_loads(conn) -> None:
    """capability_loads 补列（存量库升级）。

    这张表是后加的：``create_all`` 只在**表不存在**时才建，碰上更早版本已经建过
    （哪怕是缺列的半成品）就不会补字段，于是 INSERT 报「no column named ...」。
    这里把缺的列逐个补齐，键与 ORM 定义保持一致。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(capability_loads)")).fetchall()
    if not rows:
        return  # 表还没建（create_all 会按 ORM 建全的），不用补
    existing = {r[1] for r in rows}
    for name, ddl in (
        ("trace_id", "VARCHAR(26)"),
        ("agent_id", "VARCHAR(64)"),
        ("session_id", "VARCHAR(26)"),
        ("message_id", "VARCHAR(26)"),
        ("task_id", "VARCHAR(26)"),
        ("key", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("kind", "VARCHAR(16) NOT NULL DEFAULT 'skill'"),
        ("source", "VARCHAR(16) NOT NULL DEFAULT 'model_load'"),
        ("chars", "INTEGER"),
        ("created_at", "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ):
        if name not in existing:
            await conn.exec_driver_sql(
                f"ALTER TABLE capability_loads ADD COLUMN {name} {ddl}"
            )
            logger.info("capability_loads 补列: %s", name)


async def _migrate_react_step_duration(conn) -> None:
    """react_steps 补 duration_ms：一步的总耗时（存量库升级）。

    不能拿 updated_at - created_at 顶替：updated_at 带 onupdate，会被后续写刷新。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(react_steps)")).fetchall()
    existing = {r[1] for r in rows}
    if "duration_ms" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE react_steps ADD COLUMN duration_ms INTEGER"
        )


async def _migrate_session_message_duration(conn) -> None:
    """session_messages 补 duration_ms：一轮端到端耗时（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(session_messages)")).fetchall()
    existing = {r[1] for r in rows}
    if "duration_ms" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE session_messages ADD COLUMN duration_ms INTEGER"
        )


async def _migrate_task_time_range(conn) -> None:
    """tasks 补 started_at / finished_at：任务耗时（存量库升级）。

    存量行一律 NULL——升级前无从得知历史任务的起止时间，宁缺毋滥。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(tasks)")).fetchall()
    existing = {r[1] for r in rows}
    if "started_at" not in existing:
        await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN started_at DATETIME")
    if "finished_at" not in existing:
        await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN finished_at DATETIME")


async def _migrate_llm_call_reasoning(conn) -> None:
    """llm_calls 补 reasoning_tokens：思维链 token（存量库升级）。

    存量行一律 NULL——升级前没有这个指标。新调用会写入；非 reasoning 模型恒为 0。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(llm_calls)")).fetchall()
    existing = {r[1] for r in rows}
    if "reasoning_tokens" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE llm_calls ADD COLUMN reasoning_tokens INTEGER"
        )


async def _migrate_model_price_time_range(conn) -> None:
    """model_prices 补 time_from_minute / time_to_minute：错峰计费（存量库升级）。

    存量行一律 NULL——升级前配的都是「全天通用价」，语义不受影响。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(model_prices)")).fetchall()
    existing = {r[1] for r in rows}
    if "time_from_minute" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE model_prices ADD COLUMN time_from_minute INTEGER"
        )
    if "time_to_minute" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE model_prices ADD COLUMN time_to_minute INTEGER"
        )


async def _migrate_model_price_weekdays(conn) -> None:
    """model_prices 补 weekdays：按星期几区分价格（存量库升级）。

    存量行一律 NULL——升级前的价格都是「每天适用」，语义不受影响。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(model_prices)")).fetchall()
    existing = {r[1] for r in rows}
    if "weekdays" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE model_prices ADD COLUMN weekdays VARCHAR(32)"
        )


async def _migrate_tool_call_args_hash(conn) -> None:
    """tool_calls 补 args_hash：识别重复调用 / 空转（存量库升级）。

    存量行一律 NULL——升级前的记录无从判断参数是否相同，按「不重复」处理。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(tool_calls)")).fetchall()
    existing = {r[1] for r in rows}
    if "args_hash" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE tool_calls ADD COLUMN args_hash VARCHAR(16)"
        )


async def init_db(url: str | None = None) -> None:
    """建表（幂等）+ 补列迁移。顺带开 WAL，提升读写并发。"""
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
        await _drop_agent_legacy_columns(conn)
        await _migrate_agent_loaded(conn)
        await _migrate_agent_origin(conn)
        await _migrate_drop_plugin_tables(conn)
        await _migrate_chat_session_agent_id(conn)
        await _migrate_agent_message_task_id(conn)
        await _migrate_thread_last_task_id(conn)
        await _migrate_thread_context_id(conn)
        await _migrate_thread_last_task_state(conn)
        await _migrate_chat_session_context_id(conn)
        await _migrate_chat_session_user_space(conn)
        await _migrate_session_message_artifacts(conn)
        await _migrate_agent_peer_source(conn)
        await _migrate_task_item_rounds(conn)
        await _migrate_task_paused(conn)
        await _migrate_task_review_fields(conn)
        await _migrate_chat_session_kind(conn)
        await _seed_default_user_space(conn)
        await _migrate_session_message_artifact_paths(conn)
        await _migrate_strip_tmp_artifacts(conn)
        await _drop_legacy_resource_tables(conn)
        # 可观测：llm_calls 表由上面的 create_all 自动建（ORM 模型），
        # 这里只需给存量表补列。
        await _migrate_llm_profile_prices(conn)
        await _migrate_react_step_duration(conn)
        await _migrate_capability_loads(conn)
        await _migrate_session_message_duration(conn)
        await _migrate_task_time_range(conn)
        await _migrate_llm_call_reasoning(conn)
        await _migrate_model_price_time_range(conn)
        await _migrate_model_price_weekdays(conn)
        await _migrate_tool_call_args_hash(conn)


async def dispose_engine() -> None:
    """释放引擎（进程退出时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
