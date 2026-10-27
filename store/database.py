"""数据库引擎与初始化（SQLite + aiosqlite，异步、不阻塞事件循环）。"""
from __future__ import annotations

import json
import os
import re
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

from .models import Base, ChatSession

logger = logging.getLogger(__name__)

# 默认库文件位置：``~/.keeper/data/keeper.db``（用户主目录下的 keeper 数据目录）。
#
# 为什么不再放在项目目录里：打包后的桌面端里 ``__file__`` 指向**解包出来的临时
# 目录**，把库写在项目目录（``keeper/data/keeper.db``）意味着每次启动都是一个
# 全新空库——用户装好的应用"一点数据都没有"就是这么来的（会话、模型、用量全
# 丢在一份没人读的空库里）。数据应该跟着用户走，和 settings.yaml / agents /
# search / logs 放在同一个目录下。
#
# 想指到别处仍然可以：设 ``KEEPER_DB`` 环境变量（测试、临时实例都靠它）。
DEFAULT_DB_PATH = Path.home() / ".keeper" / "data" / "keeper.db"


def db_path() -> Path:
    """实际的库文件位置：``KEEPER_DB`` 优先，否则用默认位置。

    为什么给这个口子：默认位置在**项目目录里**，跑测试、起临时实例、验证新功能
    时都会碰到用户的真实库（里面是 agent、会话、用量记录）。有了它就能把实例
    指到别处去，验证完删掉即可，不碰用户数据。
    （与 ``setting/user_settings.py`` 的 ``KEEPER_SETTINGS`` 同一套约定。）
    """
    env = (os.getenv("KEEPER_DB") or "").strip()
    return Path(env) if env else DEFAULT_DB_PATH

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def db_url(path: str | Path | None = None) -> str:
    """拼连接串；必要时创建目录。"""
    p = Path(path) if path else db_path()
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
    # 模型配置的 inline 列：唯一来源已改为 agent_llm_binding → llm_profiles，
    # 而这列优先级最高、内容又来自 config.yaml 同步，导致绑好的预设被无声压住。
    # 改成"先在模型管理页配好、再绑定"之后它彻底没人读了。
    "llm",
    # 三类资源的下载目录覆盖：从未有写入方（全库非空数为 0），运行时也不读，
    # 只在模型定义和几处注释里出现过。
    "mcp_cache_dir",
    "tool_cache_dir",
    "skill_cache_dir",
    # agent 侧工作区两列：工作空间是**会话级**属性，装配时永远用 default，不存在
    # "给 agent 绑工作区"。会话压根不看 agent 的绑定，那个分支零收益却制造了
    # "平台同步的 agent 永远没绑定"等状态。
    "workspace_kind",
    "workspace_path",
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
    """agent 补 ``origin`` 列，并把历史值统一归一成 ``local``。

    **这一列现在是历史字段**
    ----------------------
    曾经它区分「从平台同步下来的副本」（platform）与「本机创建的」（local）：
    两类 agent 混在同一张表里而 id 分别来自平台和本地 ULID，没有它就没法判断
    某行是不是平台的镜像，于是同一个 agent 在 UI 上显示两遍。

    平台对接已整体移除——keeper 现在是本机单用户应用，库里有的就是本机的。
    所以这里做两件事：给老库补上这一列（不让 ORM 读不到列直接炸），
    并把历史 ``platform`` 归一成 ``local``（语义正确，也让 UI 口径一致）。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent)")).fetchall()
    if not rows:
        return  # 表还没建（create_all 会按 ORM 建全的）
    existing = {r[1] for r in rows}
    if "origin" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE agent ADD COLUMN origin VARCHAR(16) DEFAULT 'local'"
        )
    await conn.exec_driver_sql("UPDATE agent SET origin = 'local' WHERE origin IS NULL")
    # 平台没了，「平台同步来的」这个来源就不存在了：那些行现在就是本机的 agent。
    await conn.exec_driver_sql("UPDATE agent SET origin = 'local' WHERE origin = 'platform'")


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


async def _drop_user_space_table(conn) -> None:
    """下线多用户空间：删掉 ``user_space`` 表（存量库升级，幂等）。

    多空间已经下线——「我的空间」是**唯一**的空间，路径存在 ``settings.yaml`` 的
    ``user_space.path``，不再走数据库。因此这张表连同它承载的"会话绑哪个空间"
    语义一起作废：``chat_sessions.user_space_id`` 这一列也不再被任何代码读或写
    （ORM 里已删掉映射），存量行里残留的值没有意义，直接留着即可——它是可空列，
    读它的代码路径已经不存在。

    为什么**不**顺手 ``ALTER TABLE chat_sessions DROP COLUMN``：那一列上挂着指向
    ``user_space.id`` 的外键约束，而 SQLite 不允许 DROP COLUMN 一个被外键引用的
    列；要真删就得整表重建（重建 ``chat_sessions`` 会牵动引用它的
    ``session_messages`` / ``react_step`` / ``task`` 等多张表的外键），风险远大于
    收益——一列没人读的可空列不占空间、不影响行为，留着是最稳的选择。

    删表时还要注意外键：``chat_sessions.user_space_id`` 仍指向被删的表。SQLite
    默认不开 ``PRAGMA foreign_keys``（keeper 也没开），所以删被引用的表不会报错；
    这里显式关掉再删，保证即便将来有人打开了外键也不会在这里炸。
    """
    await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    await conn.exec_driver_sql("DROP TABLE IF EXISTS user_space")


async def _migrate_session_message_artifacts(conn) -> None:
    """session_messages 补 artifacts 列：承载本轮 agent 产出的文件引用（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(session_messages)")).fetchall()
    existing = {r[1] for r in rows}
    if "artifacts" not in existing:
        await conn.exec_driver_sql("ALTER TABLE session_messages ADD COLUMN artifacts TEXT")


async def _migrate_session_message_attachments(conn) -> None:
    """session_messages 补 attachments 列：承载用户上传的附件（存量库升级）。

    与 artifacts 分开两列而不是复用：语义不同（见 store/models.py 里
    SessionMessage.attachments 的注释），混在一列里前端与后端都分不清
    「这个文件是 agent 产出的还是用户传进来的」。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(session_messages)")).fetchall()
    existing = {r[1] for r in rows}
    if "attachments" not in existing:
        await conn.exec_driver_sql("ALTER TABLE session_messages ADD COLUMN attachments TEXT")


async def _migrate_agent_peer_source(conn) -> None:
    """agent_peers 补 source / trust_env 列：区分本进程内 / 外部对端（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(agent_peers)")).fetchall()
    existing = {r[1] for r in rows}
    if "source" not in existing:
        await conn.exec_driver_sql("ALTER TABLE agent_peers ADD COLUMN source TEXT")
    if "trust_env" not in existing:
        await conn.exec_driver_sql("ALTER TABLE agent_peers ADD COLUMN trust_env BOOLEAN")


def _my_space_root() -> Optional[str]:
    """「我的空间」的绝对路径（``settings.yaml`` 的 ``user_space.path``）。

    没配置 / 读不到时返回 None——**不兜底到某个内置目录**。多空间下线后
    「我的空间」由用户显式选择，没选就是没选；替用户猜一个目录只会让产物落到
    用户看不见的地方。口径与 :func:`agent.config._resolve_default_workspace` 一致。

    放在本模块而不是直接调 ``setting.get_user_space_path``：``store`` 是最底层，
    不想在 import 期就牵扯 yaml 解析；这里用**函数内延迟导入**。
    """
    raw = ""
    try:
        from ..setting import get_user_space_path

        raw = (get_user_space_path() or "").strip()
    except Exception as e:  # noqa: BLE001 读不到就按"没配置"处理
        logger.warning("读取「我的空间」路径失败（已忽略）: %s", e)
    if raw:
        return os.path.expanduser(raw)
    return None


async def _migrate_session_message_artifact_paths(conn) -> None:
    """旧产物 ``path`` 规范化（存量库升级，幂等）。

    早期版本把 path 存成：
    - 相对路径（如 ``xian_travel.html``）→ 新版按 CWD 解析会 404；
    - 裸绝对路径（如 ``/abs/x.html``）→ 可用但缺 ``file://`` scheme，风格不统一。

    这里把相对路径**按「我的空间」根**还原为 ``file://`` 绝对路径，裸绝对路径补
    ``file://`` 前缀；已是 ``file://`` 的跳过。

    单一空间后不再按会话 join 空间表：所有会话的工作空间都来自
    ``settings.yaml`` 的 ``user_space.path``（没配置就没有根，相对路径原样保留）。
    """
    default_root = _my_space_root()

    rows = (
        await conn.exec_driver_sql(
            "SELECT sm.id, sm.artifacts "
            "FROM session_messages sm "
            "WHERE sm.artifacts IS NOT NULL AND sm.artifacts != ''"
        )
    ).fetchall()

    for msg_id, artifacts_json in rows:
        try:
            arts = json.loads(artifacts_json)
        except Exception:
            continue
        if not isinstance(arts, list):
            continue
        changed = False
        root = default_root
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

    # 只捞路径里带 `/@tmp/` 段的（真正的会话临时目录）；其余一律不动。
    # 注意：合法产物（mermaid/.mmd/图片/…）就存放在 workspace/session/<sid>/ 下，
    # 若在此按 session_root 整段剔除了，会把所有正常产物误删——这正是「重启后
    # artifacts 全空」的根因。临时文件只会出现在 `@tmp/` 子目录里。
    rows = (
        await conn.exec_driver_sql(
            "SELECT id, artifacts FROM session_messages "
            "WHERE artifacts IS NOT NULL AND artifacts != '' "
            "AND artifacts LIKE ?",
            ("%" + "/" + TMP_PREFIX + "/%",),
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
                # 只剔除以 `/@tmp/` 段出现的会话临时目录；合法产物（mermaid/.mmd/图片…）
                # 位于 workspace/session/<sid>/ 下，必须保留，否则重启后 artifacts 全空。
                # 用子串判断而非 startswith，兼容早期没带 file:// scheme 的裸绝对路径。
                if ("/" + TMP_PREFIX + "/") in p:
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


async def _migrate_chat_session_deleted_at(conn) -> None:
    """chat_sessions 补 deleted_at 列：软删除要记删除时间（存量库升级）。

    存量行一律留空——它们的 status 都是 active，没有删除时间可言。
    有了这列才能按「删了多久」算保留期；复用 updated_at 是不行的，
    它会被后续任何一次写顶掉。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "deleted_at" not in existing:
        await conn.exec_driver_sql("ALTER TABLE chat_sessions ADD COLUMN deleted_at DATETIME")


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


async def _drop_chat_sessions_legacy_columns(conn) -> None:
    """删掉 ``chat_sessions`` 上几个没人用的遗留列（存量库升级，幂等）。

    - ``user_space_id``：多用户空间下线后没人读也没人写；它上面还挂着一条指向
      已删除的 ``user_space`` 表的**死外键**。
    - ``ball_key``：「浮球」默认会话的归属键，浮球功能已下线。
    - ``summary``：会话摘要——从来没有代码写过它（ORM 里定义了，读取侧一次都没
      用过），存量行全为 NULL。

    为什么不能直接 ``ALTER TABLE chat_sessions DROP COLUMN``：实测 SQLite 会报
    ``error in table chat_sessions after drop column: unknown column "user_space_id"
    in foreign key definition``——DROP COLUMN 删不掉**带外键约束**的列（删掉列后
    表里会剩下一条指向不存在列的约束，SQLite 解析不了），而 SQLite 没有
    ``DROP CONSTRAINT``；``ball_key`` 那列则先被索引挡着（索引里有这列就删不了列）。

    所以只能**重建表**：按现有 DDL 改写出「去掉这几列与其外键」的版本 → 建新表 →
    拷数据 → 换名 → 重建索引。整个过程在 init_db 的**一个事务**里，中途失败会
    整体回滚，不会留下半张表。

    四个容易踩的细节：

    1. **改写 DDL 时按「顶层逗号」切片段**，不能按行——``ALTER TABLE ADD COLUMN``
       追加的列会挤在同一行里（``parent_id TEXT, activated_plugins TEXT,``），
       按行删会连带删掉别的列。
    2. **换名时开 ``PRAGMA legacy_alter_table=ON``**：SQLite 默认（legacy 关闭）会
       在 RENAME 时重写全库的引用，把 ``session_messages`` / ``threads`` 里
       ``REFERENCES chat_sessions`` 改成指向临时表名，最后那两张表的外键就指向了
       一张被删掉的表。开了 legacy 就只改表名本身、不动别人的引用文本。
    3. **先 DROP 旧表再 RENAME 新表**：DROP 会连带删掉旧表上的索引，正好腾出名字
       给新表重建用；反过来（旧表改名、新表顶上）会把索引名占着。
    4. **表名按模式替换**：重建过一次之后 sqlite_master 里的 DDL 会变成
       ``CREATE TABLE "chat_sessions"``（RENAME 会加引号），硬编码字符串就再也
       匹配不上了。
    """
    legacy = ("user_space_id", "ball_key", "summary")
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    if not rows:
        return
    existing = {r[1] for r in rows}
    if not any(c in existing for c in legacy):
        return  # 新库（create_all 建的）本来就没这几列

    old_cols = [r[1] for r in rows]
    old_ddl = (
        await conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_sessions'"
        )
    ).scalar()
    if not old_ddl:
        return

    # --- 1) 改写 DDL：去掉这几列的定义，以及引用它们的外键约束 ---
    head = old_ddl[: old_ddl.index("(") + 1]
    body = old_ddl[old_ddl.index("(") + 1 : old_ddl.rindex(")")]
    tail = old_ddl[old_ddl.rindex(")") :]
    kept: list[str] = []
    for frag in _split_sql_top_level(body):
        st = frag.strip()
        if not st:
            continue
        if st.upper().startswith("FOREIGN KEY"):
            # 形如 FOREIGN KEY(user_space_id) REFERENCES user_space (id)
            inner = st.split("(", 1)[1] if "(" in st else ""
            if any(inner.startswith(c) and (len(inner) == len(c) or not inner[len(c)].isalnum())
                   for c in legacy):
                continue
        elif st.split()[0] in legacy:
            continue
        kept.append(st)
    new_ddl = head + "\n\t" + ",\n\t".join(kept) + "\n" + tail

    # --- 2) 记下旧表上的索引（跳过 sqlite_autoindex 与涉及遗留列的）---
    keep_idx: dict[str, list[str]] = {}
    for r in await conn.exec_driver_sql("PRAGMA index_list(chat_sessions)"):
        name = r[1]
        if name.startswith("sqlite_autoindex"):
            continue
        cols = [
            c[2]
            for c in await conn.exec_driver_sql(f"PRAGMA index_info({name!r})")
        ]
        if any(c in legacy for c in cols):
            continue
        keep_idx[name] = cols
    # ORM 里声明的索引也补上（老库可能缺，比如后加的 parent_id 索引）——
    # 让重建后的库与新库（create_all 建的）结构一致。
    for idx in ChatSession.__table__.indexes:
        cols = [c.name for c in idx.columns]
        if not any(c in legacy for c in cols):
            keep_idx.setdefault(idx.name, cols)

    # --- 3) 建新表 → 拷数据 → 换名 → 重建索引 ---
    tmp = "chat_sessions__rebuild"
    await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {tmp}")
    # 表名要按**模式**替换，不能按字面量：重建过一次之后 sqlite_master 里的
    # DDL 变成了 CREATE TABLE "chat_sessions"（RENAME 会加上引号），硬编码
    # "CREATE TABLE chat_sessions" 就再也匹配不上了。
    await conn.exec_driver_sql(
        re.sub(
            r"^CREATE TABLE (IF NOT EXISTS )?\"?chat_sessions\"?",
            lambda m: f'CREATE TABLE {m.group(1) or ""}"{tmp}"',
            new_ddl,
            count=1,
        )
    )
    new_cols = [
        r[1] for r in await conn.exec_driver_sql(f"PRAGMA table_info({tmp})")
    ]
    copy_cols = [c for c in new_cols if c in old_cols]
    collist = ", ".join(copy_cols)
    await conn.exec_driver_sql(
        f"INSERT INTO {tmp} ({collist}) SELECT {collist} FROM chat_sessions"
    )
    await conn.exec_driver_sql("DROP TABLE chat_sessions")
    await conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        await conn.exec_driver_sql(f"ALTER TABLE {tmp} RENAME TO chat_sessions")
    finally:
        await conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    for name, cols in keep_idx.items():
        await conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {name} ON chat_sessions ({', '.join(cols)})"
        )
    logger.info("chat_sessions 已重建：删除遗留列 %s", ", ".join(legacy))


def _split_sql_top_level(s: str) -> list[str]:
    """按**顶层**逗号切分 SQL 片段（括号内的逗号不算）。

    用来把建表语句的 body 拆成「一个列定义 / 一条表约束」一段，好逐段判断要不要
    保留。``DECIMAL(10,2)`` 这种括号内带逗号的类型也不会被切错。
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


async def _migrate_chat_session_activated_plugins(conn) -> None:
    """chat_sessions 补 activated_plugins 列：本会话动态激活的插件名（JSON 数组）。

    存量行一律留空（NULL）——读取侧把 NULL 当「未激活」，无需回填。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "activated_plugins" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN activated_plugins TEXT"
        )


async def _migrate_chat_session_thinking(conn) -> None:
    """chat_sessions 补 thinking / reasoning_effort 列：会话级思考模式与强度。

    存量行回填 thinking=0（关）/ reasoning_effort='low'——与"从没配过思考"的行为一致。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "thinking" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN thinking BOOLEAN DEFAULT 0"
        )
    if "reasoning_effort" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN reasoning_effort TEXT DEFAULT 'low'"
        )
    await conn.exec_driver_sql(
        "UPDATE chat_sessions SET reasoning_effort = 'low' WHERE reasoning_effort IS NULL"
    )


async def _migrate_chat_session_mode(conn) -> None:
    """chat_sessions 补 mode 列：运行模式（普通/快捷/程序化）。存量行回填 normal。

    回填 normal = 与改造前完全一致（普通模式就是原逻辑），老会话行为不变。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "mode" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN mode TEXT DEFAULT 'normal'"
        )
    await conn.exec_driver_sql(
        "UPDATE chat_sessions SET mode = 'normal' WHERE mode IS NULL"
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


async def _migrate_llm_call_cost(conn) -> None:
    """llm_calls 补成本列（存量库升级）。

    成本改成**调用返回时就按当时的单价算好、随 token 一起落库**（见
    ``observability/recording.py``）。存量行一律 NULL —— 没有当时的单价快照，
    就不去反推一个可能错误的金额（用户已确认历史数据不追溯）。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(llm_calls)")).fetchall()
    existing = {r[1] for r in rows}
    for col, decl in (
        ("cost", "FLOAT"),
        ("currency", "VARCHAR(8)"),
        ("price_snapshot", "TEXT"),
    ):
        if col not in existing:
            await conn.exec_driver_sql(
                f"ALTER TABLE llm_calls ADD COLUMN {col} {decl}"
            )


async def _migrate_llm_profile_preset(conn) -> None:
    """移除早期版本给模型预设加的「出处」几列（存量库升级）。

    那套设计是：预设导入时记 ``preset_key`` + 预设值快照，将来官方改预设时
    只覆盖用户没改过的字段。**现在不用了**——预设只在前端帮忙填表，落库的一切
    以创建时传入的数据为准，服务端不记账、不做合并。

    所以这里把 ``llm_profiles.source / preset_key / preset_snapshot`` 与
    ``model_prices.source / preset_key`` 删掉，让表结构回到"只有模型配置本身"。
    """
    # 有索引的列要先删索引，否则 SQLite 不允许 DROP COLUMN
    for idx in (
        "ix_llm_profiles_preset_key",
        "ix_model_prices_preset_key",
    ):
        await conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")

    for table, cols in (
        ("llm_profiles", ("preset_snapshot", "preset_key", "source")),
        ("model_prices", ("preset_key", "source")),
    ):
        rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).fetchall()
        existing = {r[1] for r in rows}
        for col in cols:
            if col not in existing:
                continue
            try:
                await conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {col}")
            except Exception as e:  # noqa: BLE001 —— 老 SQLite 不支持 DROP COLUMN
                logger.warning("移除废弃列失败（忽略）：%s.%s：%s", table, col, e)


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


async def _migrate_session_message_mentions(conn) -> None:
    """session_messages 补 mention_raw / mentions 列：气泡里 @标签 的展示元数据（存量库升级）。"""
    rows = (await conn.exec_driver_sql("PRAGMA table_info(session_messages)")).fetchall()
    existing = {r[1] for r in rows}
    if "mention_raw" not in existing:
        await conn.exec_driver_sql("ALTER TABLE session_messages ADD COLUMN mention_raw TEXT")
    if "mentions" not in existing:
        await conn.exec_driver_sql("ALTER TABLE session_messages ADD COLUMN mentions TEXT")


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


async def _migrate_react_parse_stat_model(conn) -> None:
    """react_parse_stats 补 model 列：幻觉率要按模型看（存量库升级）。

    为什么必须按模型：模型偶尔会自己伪造工具回执（自己写 OBSERVATION /
    连写多个 THOUGHT），解析器检出后整轮作废。但「哪个模型跑飞得多」才是
    能指导决策的数据——小模型明显更高，换模型就可能根治。聚合在一起看
    只会得到一个被平均掉、看不出差别的数字。

    存量行一律留 NULL：升级前没有记录模型，无法回填。统计时按「未知」归类，
    不参与幻觉率计算（见 observability.stats.react_parse_stats）。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(react_parse_stats)")).fetchall()
    if not rows:
        return  # 表还没建（create_all 会按 ORM 建全的）
    existing = {r[1] for r in rows}
    if "model" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE react_parse_stats ADD COLUMN model VARCHAR(64)"
        )


async def _migrate_chat_session_parent_id(conn) -> None:
    """chat_sessions 补 parent_id 列：子 agent（delegatee）子会话挂到主会话下（存量库升级）。

    存量行一律留空——升级前没有子会话概念，普通会话本就没有父。
    """
    rows = (await conn.exec_driver_sql("PRAGMA table_info(chat_sessions)")).fetchall()
    existing = {r[1] for r in rows}
    if "parent_id" not in existing:
        await conn.exec_driver_sql(
            "ALTER TABLE chat_sessions ADD COLUMN parent_id TEXT"
        )


async def _seed_schema_docs(conn) -> None:
    """播种表说明（``schema_doc`` 表），内容变了才写。

    为什么不是无条件 upsert：这张表每次启动都全量比对 + 写一遍的话，
    WAL 会一直有写、``updated_at`` 每次都变（那列本来是"说明何时改过"的语义），
    还会无谓地惊动文件系统监控。所以只写**内容确实不同**的行。

    说明的真源是 ``store/schema_doc.py`` 里的种子数据——改说明改那里，
    再跑 ``python -m keeper.store.schema_doc --write`` 重新生成
    ``doc/database.md``。
    """
    from .models import SchemaDoc
    from .schema_doc import all_docs

    rows = (await conn.exec_driver_sql("SELECT table_name, db, purpose FROM schema_doc")).all()
    current = {(r[0], r[1]): r[2] for r in rows}
    for doc in all_docs():
        key = (doc["table_name"], doc["db"])
        payload = {
            "purpose": doc.get("purpose") or "",
            "owner": doc.get("owner"),
            "readers": doc.get("readers"),
            "lifecycle": doc.get("lifecycle"),
            "notes": doc.get("notes"),
        }
        if key not in current:
            await conn.execute(
                SchemaDoc.__table__.insert().values(table_name=key[0], db=key[1], **payload)
            )
        elif current[key] != payload["purpose"]:
            # 说明改过了：整行更新（其余字段可能也变了，一起覆盖）
            await conn.execute(
                SchemaDoc.__table__.update()
                .where(SchemaDoc.table_name == key[0])
                .values(**payload)
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
        await _drop_user_space_table(conn)
        await _migrate_session_message_artifacts(conn)
        await _migrate_session_message_attachments(conn)
        await _migrate_agent_peer_source(conn)
        await _migrate_task_item_rounds(conn)
        await _migrate_task_paused(conn)
        await _migrate_task_review_fields(conn)
        await _migrate_chat_session_kind(conn)
        await _drop_chat_sessions_legacy_columns(conn)
        await _migrate_chat_session_deleted_at(conn)
        await _migrate_chat_session_thinking(conn)
        await _migrate_chat_session_mode(conn)
        await _migrate_chat_session_activated_plugins(conn)
        await _migrate_session_message_artifact_paths(conn)
        await _migrate_strip_tmp_artifacts(conn)
        await _drop_legacy_resource_tables(conn)
        # 可观测：llm_calls 表由上面的 create_all 自动建（ORM 模型），
        # 这里只需给存量表补列。
        await _migrate_llm_profile_prices(conn)
        await _migrate_llm_profile_preset(conn)
        await _migrate_react_step_duration(conn)
        await _migrate_capability_loads(conn)
        await _migrate_session_message_duration(conn)
        await _migrate_session_message_mentions(conn)
        await _migrate_task_time_range(conn)
        await _migrate_llm_call_reasoning(conn)
        await _migrate_llm_call_cost(conn)
        await _migrate_model_price_time_range(conn)
        await _migrate_model_price_weekdays(conn)
        await _migrate_tool_call_args_hash(conn)
        await _migrate_react_parse_stat_model(conn)
        await _migrate_chat_session_parent_id(conn)
        # 表说明元数据：放最后，确保前面的建表/迁移都已完成
        await _seed_schema_docs(conn)


async def dispose_engine() -> None:
    """释放引擎（进程退出时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
