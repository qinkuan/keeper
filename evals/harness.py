"""评测的隔离环境。

评测最怕两件事：**污染真实数据**和**跑不起来**。这里的隔离是硬约束：

1. **独立数据库**：把真实库用 SQLite 的 backup API 复制一份到运行目录。
   必须是 backup 而不是 ``shutil.copy``——真实库开着 WAL，直接拷文件会漏掉
   还在 WAL 里的数据（表现为「agent 配置忽然少了几条」）。
   复制而不是新建空库，是因为 ``load_agent_config`` 要从库里读 agent 画像 /
   插件绑定 / 模型预设，配置本身是被测对象的一部分。
2. **独立工作空间**：每条用例一个独立目录，并临时登记成一条 user_space 绑给
   会话。于是即使 agent 写了文件，也只写在运行目录里。
3. **只读优先**：默认 ``read_only=True``，需要写文件的用例显式
   ``allow_writes: true``。
4. **离线装载**：默认**跳过插件资源下载**（``--offline``）。评测不该因为网络
   抖动而失败，也不该每次跑都重下一遍插件。

进程隔离是前提：本模块假定自己跑在**独立进程**里（``keeper.evals.run``）。
服务进程里那个全局 SQLAlchemy 引擎已经绑在真实库上，评测绝不能借用它。
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..store import (
    UserSpace,
    db_url,
    dispose_engine,
    get_session_factory,
    init_db,
    new_id,
)
from .spec import EvalCase

logger = logging.getLogger(__name__)

# ---- 产物落盘位置：与项目其它运行时数据同源，一律放 ~/.keeper/ 下 ----
#
# 不放代码目录（keeper/evals/）有两个实际原因：
# 1. **代码与产物混在一起会让 git 变危险**。产物目录里躺着 17MB 的临时库，
#    而 ``keeper/evals/`` 本身又是该进版本库的代码目录——两者挨着时，
#    将来加 gitignore 规则极容易把代码一起干掉（首跑时就是这个状态：
#    整个 evals 目录在 git 里是未跟踪的）。
# 2. **多份 checkout 互不污染**。切分支、试不同版本时各自的基线不会串。
#
# 至于「基线要不要进版本库」：不进。它是模型输出的记录，每次跑都产生新的，
# 而模型有随机性 → 基线天然会漂；且报告里含答案原文，进历史等于长期留档。
# 要分享结果用 ``--show`` 打印 Markdown 贴进 PR，不需要文件进 git。
#
# 环境变量可覆盖：CI / 测试时指到临时目录，避免污染真实基线。
EVALS_ROOT = Path(
    os.getenv("KEEPER_EVALS_DIR") or (Path.home() / ".keeper" / "evals")
)
# 单次运行的现场（克隆的库、临时工作空间、子进程日志）——纯临时，可随时删
RUNS_ROOT = EVALS_ROOT / "runs"
# 历次基线报告（唯一需要长期保留的东西）
BASELINES_DIR = EVALS_ROOT / "baselines"


def _snapshot(root: Path) -> Dict[str, float]:
    """目录内容快照：``相对路径 -> (mtime, size)``。

    用 mtime+size 而不是纯文件列表，是为了能发现**改写**已有文件——只对比
    文件名的话，「原地改掉了 notes.md」会被当成没变化。
    """
    out: Dict[str, float] = {}
    try:
        for p in root.rglob("*"):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(root))] = round(st.st_mtime, 3) + st.st_size
    except Exception as e:  # noqa: BLE001
        logger.debug("快照目录失败 %s: %s", root, e)
    return out


@dataclass
class CaseOutcome:
    """一条用例跑完的原始产出（还没做断言判定）。"""

    case_id: str
    title: str
    tags: List[str]
    input: str
    answer: str = ""
    # 本轮真实发生的工具调用（按发生顺序，只含工具步）
    tool_calls: List[str] = field(default_factory=list)
    step_count: int = 0
    # 运行级异常：LLM 报错、agent 未就绪等。断言判定前先看它。
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    # 存档用：答案与工具序列的截断副本（报告里给人看）
    answer_preview: str = ""
    workspace: str = ""


class EvalHarness:
    """一次评测运行的全部环境管理。"""

    def __init__(
        self,
        *,
        run_id: str,
        offline: bool = True,
        source_db: Optional[Path] = None,
    ) -> None:
        self.agent_id: str = ""   # load_agent() 之后才有值
        self.run_id = run_id
        self.offline = offline
        self.source_db = Path(source_db) if source_db else None
        self.run_dir = RUNS_ROOT / run_id
        self.db_path = self.run_dir / "eval.db"
        self.agent: Any = None
        self._original_fetch: Any = None

    # ---- 生命周期 ----

    async def setup(self) -> None:
        """准备独立库（建目录 + 克隆 + 建表）。

        **不**在这里装载 agent：被测 agent 的 id 要从库里查，而库正是这一步
        才建好的。先建环境、再定 agent、最后 ``load_agent()``——顺序反了就会
        「用还没建好的库去查 agent」。
        """
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self._clone_db()
        # 注意：get_engine 是**全局单例**，这里传的 url 只在首次创建时生效。
        # 这正是评测必须跑在独立进程的原因。
        await init_db(db_url(self.db_path))

        if self.offline:
            self._disable_fetch()
        logger.info("评测库就绪：%s（离线装载=%s）", self.db_path, self.offline)

    async def load_agent(self, agent_id: str) -> None:
        """装配并注册被测 agent。"""
        from ..agent.keeper import build_agent, register_keeper

        self.agent_id = agent_id
        self.agent = await build_agent(agent_id=agent_id)
        register_keeper(agent_id, self.agent)
        logger.info("被测 agent 已装载：%s", agent_id)

    async def teardown(self) -> None:
        """释放 agent（MCP 子进程）与数据库连接池。异常一律吞掉。"""
        self._restore_fetch()
        try:
            from ..agent.keeper import shutdown_all, unregister_keeper

            await shutdown_all()
            unregister_keeper(self.agent_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("释放评测 agent 失败（忽略）: %s", e)
        try:
            await dispose_engine()
        except Exception as e:  # noqa: BLE001
            logger.debug("释放评测连接池失败（忽略）: %s", e)

    # ---- 被测对象指纹 ----

    async def fingerprint(self) -> Dict[str, Any]:
        """记录「这次测的是什么东西」。

        没有指纹，跨时间的数字没有可比性：换了模型 token 变多不能算「变差」，
        改了提示词用例失败也不能算「代码退化」。基线最怕的就是口径会漂，
        所以配置变了必须在报告里看得见。
        """
        out: Dict[str, Any] = {"agent_id": self.agent_id}
        agent = self.agent
        if agent is None:
            return out
        llm = getattr(agent, "llm", None)
        out["model"] = getattr(llm, "model_name", None)
        out["provider"] = getattr(llm, "provider", None)
        out["profile_id"] = getattr(llm, "profile_id", None)
        try:
            # prepare() 顺带把工具表装好（幂等），正好拿来当「能力清单」的真相源
            proc = await agent.prepare()
            out["tools"] = sorted(t.name for t in proc.registry.all())
        except Exception as e:  # noqa: BLE001  指纹拿不到不该让评测失败
            logger.debug("采集工具清单失败（指纹将不完整）: %s", e)
            out["tools"] = []
        try:
            out["skills"] = sorted(
                getattr(s, "name", str(s)) for s in (agent.skills or [])
            )
        except Exception:  # noqa: BLE001
            out["skills"] = []
        out["skill_count"] = len(out["skills"])
        out["tool_count"] = len(out["tools"])
        out["persona_chars"] = len(getattr(agent, "persona", "") or "")
        return out

    # ---- 单条用例 ----

    async def run_case(self, case: EvalCase) -> CaseOutcome:
        """跑一条用例并采集原始产出（**不**做断言判定）。

        异常一律转成 ``error`` 字段返回而不是抛出：一条用例挂掉不该让整轮
        评测中断——恰恰相反，「它挂了」本身就是评测结论的一部分。
        """
        out = CaseOutcome(
            case_id=case.id,
            title=case.display_name,
            tags=list(case.tags),
            input=case.input,
        )

        ws_dir = self._prepare_workspace(case)
        out.workspace = str(ws_dir)
        before = _snapshot(ws_dir)

        try:
            space_id = await self._ensure_user_space(case, ws_dir)
            from ..chat.service import ChatService

            svc = ChatService(agent_id=self.agent_id)
            t0 = time.perf_counter()
            resp = await svc.ask(case.input, user_space_id=space_id)
            wall_ms = int((time.perf_counter() - t0) * 1000)

            steps = resp.get("steps") or []
            out.answer = str(resp.get("answer") or "")
            out.step_count = len(steps)
            # 只记真正的工具调用：think / ask 之类不是「调了工具」，
            # 混进来会让 must_not_call 之类的断言失真。
            out.tool_calls = [
                str(s.get("tool"))
                for s in steps
                if s.get("tool") and s.get("kind") != "ask_human"
            ]
            usage = resp.get("usage") or {}
            out.metrics = {
                "steps": out.step_count,
                "tool_calls": len(out.tool_calls),
                "llm_calls": int(usage.get("calls") or 0),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "cost": usage.get("cost"),
                # 两个耗时口径：resp 里的是端到端墙钟，llm 是纯算力耗时
                "duration_ms": int(resp.get("duration_ms") or wall_ms),
                "llm_duration_ms": int(usage.get("duration_ms") or 0),
                "asked": bool(resp.get("waiting_human")),
                "budget_stopped": bool(resp.get("budget_stopped")),
                "used_tools": bool(resp.get("used_tools")),
                "artifacts": len(resp.get("artifacts") or []),
                "llm_available": bool(resp.get("llm")),
            }
            # 补充：能力加载次数与工具失败数（只存在于观测表，查库取）
            await self._fill_observability(out, resp.get("user_message_id"))
        except Exception as e:  # noqa: BLE001
            out.error = f"{type(e).__name__}: {e}"
            logger.warning("用例 %s 执行异常: %s", case.id, out.error)

        # 隔离校验：只读用例的工作空间**不应该**被改动。把「只读」从一句配置
        # 变成可验证的事实——否则 agent 真写了文件，评测照样报绿，等于没隔离。
        try:
            after = _snapshot(ws_dir)
            changed = sorted(
                p for p in (set(before) | set(after)) if before.get(p) != after.get(p)
            )
            out.metrics["workspace_mutated"] = bool(changed)
            out.metrics["changed_files"] = changed[:10]
        except Exception as e:  # noqa: BLE001
            logger.debug("采集工作空间变更失败（忽略）: %s", e)

        out.answer_preview = out.answer[:500]
        return out

    # ---- 内部实现 ----

    def _clone_db(self) -> None:
        """用 SQLite backup API 复制真实库到运行目录。

        不用 ``shutil.copy``：真实库开着 WAL，文件拷贝拿不到还留在 WAL 里的
        数据，表现为「随机某几条 agent 配置不见了」，且难复现。
        """
        from ..store import DEFAULT_DB_PATH

        src = self.source_db or DEFAULT_DB_PATH
        if not Path(src).exists():
            raise FileNotFoundError(
                f"源数据库不存在：{src}。评测需要一个已初始化过的 keeper 库"
            )
        dst = self.db_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        s = sqlite3.connect(str(src))
        try:
            d = sqlite3.connect(str(dst))
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        logger.info("已复制数据库到评测运行目录：%s", dst)

    def _disable_fetch(self) -> None:
        """把「装载前下载插件资源」替换成空操作（离线模式）。"""
        from ..agent import config as agent_config

        if self._original_fetch is not None:
            return

        async def _noop(_row) -> None:  # noqa: ANN001
            logger.debug("离线模式：跳过插件资源下载")

        self._original_fetch = agent_config._fetch_agent_resources
        agent_config._fetch_agent_resources = _noop  # type: ignore[assignment]

    def _restore_fetch(self) -> None:
        if self._original_fetch is None:
            return
        from ..agent import config as agent_config

        agent_config._fetch_agent_resources = self._original_fetch  # type: ignore[assignment]
        self._original_fetch = None

    def _prepare_workspace(self, case: EvalCase) -> Path:
        """建本用例的工作空间并写入 setup 里的初始文件。"""
        ws = self.run_dir / "cases" / case.id
        ws.mkdir(parents=True, exist_ok=True)
        for rel, content in case.files.items():
            p = (ws / rel).resolve()
            # 挡住 ``../`` 越界写入：用例文件是纯文本，路径越界等于评测
            # 成了往任意目录写文件的入口。
            if not str(p).startswith(str(ws.resolve())):
                raise ValueError(f"用例 {case.id} 的 setup 文件路径越界：{rel}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return ws

    async def _ensure_user_space(self, case: EvalCase, ws: Path) -> str:
        """把本用例的工作空间登记成一条 user_space，返回它的 id。

        每条用例一条独立记录而不是复用同一条：复用会让用例之间通过
        「上一条留下的文件」互相影响，跑出来的差异就不能归因给被测的改动了。
        """
        factory = get_session_factory()
        now = datetime.now(timezone.utc)
        name = f"eval-{self.run_id}-{case.id}"
        async with factory() as db:
            row = UserSpace(
                id=new_id(),
                name=name,
                path=str(ws),
                read_only=case.read_only,
                description=f"评测用例 {case.id} 的临时工作空间",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            await db.commit()
            return row.id

    async def _fill_observability(
        self, out: CaseOutcome, user_message_id: Optional[str]
    ) -> None:
        """补两个只在观测表里有的指标：能力加载次数、工具失败次数。

        刻意只查这两项——其余指标 ``ask()`` 已经返回了。查得越多，评测对
        观测表结构的耦合越深，改表就容易把评测跑挂。
        """
        if not user_message_id:
            return
        try:
            from sqlalchemy import func, select

            from ..store import CapabilityLoad, ToolCall, get_session_factory as _f

            factory = _f()
            async with factory() as db:
                loads = (
                    await db.execute(
                        select(func.count(CapabilityLoad.id)).where(
                            CapabilityLoad.message_id == user_message_id
                        )
                    )
                ).scalar() or 0
                tool_total, tool_failed = (
                    (
                        await db.execute(
                            select(
                                func.count(ToolCall.id),
                                func.sum(
                                    func.iif(ToolCall.ok.is_(False), 1, 0)
                                ),
                            ).where(ToolCall.message_id == user_message_id)
                        )
                    )
                    .one()
                )
            out.metrics["capability_loads"] = int(loads or 0)
            out.metrics["tool_errors"] = int(tool_failed or 0)
            out.metrics["recorded_tool_calls"] = int(tool_total or 0)
            # 失败原因单独取出来：断言要能区分「工具坏了」和「被安全策略拒绝」
            # ——后者是期望行为（越界被拦），不该算失败。见 asserts._check_tools。
            reasons = (
                (
                    await db.execute(
                        select(ToolCall.error).where(
                            ToolCall.message_id == user_message_id,
                            ToolCall.ok.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            out.metrics["tool_failures"] = [str(r) for r in reasons if r]
        except Exception as e:  # noqa: BLE001
            logger.debug("补采观测指标失败（忽略）: %s", e)