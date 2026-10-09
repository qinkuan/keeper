"""存储层的表定义，分四组：

- 本地会话：chat_sessions / session_messages / react_steps
- 跨端链路：threads / agent_messages
- 能力市场（资源）：mcp（连接器）/ skill / tool —— 装配 agent 的原料
- 任务模式：tasks / task_items —— 一个任务绑定一个会话，计划点即任务步骤

字段与设计取舍见 keeper/doc/session-design.md 第四节；
任务模式单独成文，见 keeper/doc/task-design.md。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from ulid import ULID


def new_id() -> str:
    """生成 ULID：时间有序、全局唯一（26 字符）。"""
    return str(ULID())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 系统默认用户空间：启动建库时自动播种、始终存在。其路径固定为
# ``~/.keeper/workspace/user/default``，用户空间管理页不可删除（除非手动改库）。
USER_SPACE_DEFAULT_NAME = "default"


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    """对话框。

    initiator_id 记发起人 id：userId 或 agentId（长整型），
    据此区分该会话是人发起还是外部 agent 发起。

    agent_id 记**这个会话属于哪个 agent**：一个进程里装多个 agent 时，
    同一个人（initiator_id 相同）在不同 agent 下的会话必须分开，
    否则切换 agent 会看到别的 agent 的历史。
    """

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    initiator_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # 所属 agent：单进程多 agent 下**按它隔离会话**。
    # 没有这一列时，同一个人切换 agent 会看到别的 agent 的会话（串台）。
    agent_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    # 协议层对话上下文锚（A2A contextId 等）：与内部 session 主键**解耦**，
    # 换协议时只换映射，不动 session 主键。入站按它找/建内部 session。
    context_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    # 本会话绑定的用户空间；可空 → 回落 agent 默认工作目录
    # （工作空间与 agent 解耦，只按 会话+agent 解析）
    user_space_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("user_space.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 会话种类：chat=普通会话，task=任务绑定会话（D1：一个任务一个 session）。
    # 用于在同一个会话列表里区分两种来源，而不必分两套 UI。
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="chat")
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SessionMessage(Base):
    """入站对话。

    一个对话框固定两个角色，故只留 role 即可，不设 peer / peer_type / tool：
    - 对端由 chat_session_id 关联到 chat_sessions.initiator
    - 工具结果记在 react_steps.output
    """

    __tablename__ = "session_messages"
    __table_args__ = (UniqueConstraint("chat_session_id", "seq", name="uq_session_seq"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    chat_session_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chat_sessions.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # chat_session 内递增
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    # 产物引用（JSON）：本轮 agent 通过 fs.write_file 产出的文件，供前端展示 / 下载。
    # 形如 [{"id","name","path"(相对工作空间根),"mime","size"}]；无产物则为 NULL。
    artifacts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 本条消息（一轮）的端到端耗时：从提问到答完的墙钟毫秒。
    # 与 llm_calls 的 SUM(duration_ms) 是两个口径——后者只是「算力花了多久」，
    # 不含工具执行、等待与挂起恢复，别混用。
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ReactStep(Base):
    """一次 ReAct 的步骤。

    挂起与恢复的唯一事实来源在 status + wait_kind + wait_ref，
    threads 与 agent_messages 都不再重复记录等待状态。
    """

    __tablename__ = "react_steps"
    __table_args__ = (UniqueConstraint("session_message_id", "step", name="uq_message_step"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    session_message_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("session_messages.id"), nullable=False, index=True
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)  # 轮内序号，从 1 开始
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    input: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    wait_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    wait_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 一步的总耗时（含 LLM 思考 + 工具执行）墙钟毫秒。
    # 不能拿 updated_at - created_at 顶替：updated_at 带 onupdate，会被后续写操作刷新。
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Thread(Base):
    """对端链：本地会话 ↔ 某个对端之间的那条链。

    thread_id 由发起方生成，**对端拿它当自己那侧的 session_id**——两边用同一个值
    标识同一条链，不需要映射表。

    定位方式：``(chat_session_id, peer)``。LLM 不需要记 thread_id。

    > **不设 status**：等待状态在 react_steps，这里只回答"这条链是谁"，
    > 避免同一事实两处写。
    """

    __tablename__ = "threads"
    __table_args__ = (
        UniqueConstraint("chat_session_id", "peer", name="uq_thread_peer"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    chat_session_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chat_sessions.id"), nullable=False, index=True
    )
    peer: Mapped[str] = mapped_column(String(64), nullable=False)  # 对端标识（MCP server 名）
    # 遗留列（已停写，勿再依赖）：静默续聊回退已删除——不带 peer_step_id 即新任务，
    # 回答追问由上层显式带（工具参数 / resume 注入的 peer_reply_to）。保留仅为兼容旧库。
    last_task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    last_task_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 协议层 context（A2A contextId）：与 thread.id **解耦**的独立标识。
    # 注：出站发出的 contextId 已改 per-task（见 PeerService._ask_a2a），
    # 此列仅为兼容保留，出站不再使用。
    context_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class AgentMessage(Base):
    """跨端消息：我与某个对端之间的往来。

    同一条 message_id 在两边各存一份，归属不同：
      - 我发出的：我这边 ``direction=out``，对端存进它自己的 ``session_messages``
      - 对端发来的：对端存它自己的出站记录，我这边 ``direction=in``

    ``in_reply_to`` 串联多轮：存在指向它的记录 = 已被回复，故不设 status。
    """

    __tablename__ = "agent_messages"
    __table_args__ = (UniqueConstraint("thread_id", "seq", name="uq_thread_seq"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("threads.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # thread 内递增
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # out / in
    in_reply_to: Mapped[Optional[str]] = mapped_column(
        String(26), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Task(Base):
    """任务（任务模式）。

    一个任务**绑定一个 session**（D1）：产出计划、逐点执行、打回修补全程都在那一个
    会话里对话，上下文天然连续，因此不存在「跨 run 失忆」的问题——这也是不建
    ``task_runs`` 表的原因（D2）：执行过程本身就是该 session 里的消息，天然留痕。

    ``task_items`` 是计划的**事实来源**；``plan_md`` 只是由它渲染出的 markdown 视图
    （人直接编辑文本时再反向 upsert items），两者不双写，取舍见
    ``doc/task-design.md`` 3.3。

    产物分两层：本表 ``artifacts`` 存**结果产物**，过程产物挂在 ``task_items.artifacts``；
    二者与 ``session_messages.artifacts`` 同构（JSON 数组，path 带 ``file://`` scheme），
    取数统一走 ``chat/artifacts.py``。
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 绑定的唯一会话：一个任务一个 session（D1）
    session_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    # draft / planning / plan_review / executing / waiting_input / waiting_review
    # / done / failed / cancelled（状态机见 doc/task-design.md 第四节）
    status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="draft"
    )
    # 执行暂停开关（按 step 暂停）：前端点「暂停」置 True；ReAct 每跑完一步就检查它，
    # 命中则停在该步之后——该步已落库，未开始的下一步丢弃；下次「执行」从断点续跑。
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 计划 markdown：由 task_items 渲染（视图，非事实来源）
    plan_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 最新一条审批意见（计划打回 / 重新规划的补充说明都复用这一字段）
    plan_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plan_reject_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 计划版本：每次「重新规划」+1，执行只推进当前版本的点
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    replan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 验收打回次数（超出上限转 failed，见 doc 风险 §七）；验收通过时记为 0 不累计
    review_reject_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最新一条验收意见：验收打回时人给的反馈；验收通过时的备注也存这里（单条，非留痕）
    review_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 结果产物（JSON 数组，可多个）：[{"id","name","path","mime","size"}]
    artifacts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 任务耗时：进入 executing 记 started_at，到 done/failed/cancelled 记 finished_at。
    # 单独放在本表（而不是按 session 聚合）——任务与会话虽一对一，但任务有自己的
    # 生命周期（审批、打回、暂停），用任务自身的时间戳更贴合语义。
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class TaskItem(Base):
    """任务的计划点（计划的**事实来源**）。

    勾选态、过程产物、结论都落在这一行上；``Task.plan_md`` 由本表按 ``seq`` 渲染
    （done 渲染为 ``- [x]``），避免 markdown 与结构化状态双写不一致。

    ``plan_version`` 服务于「重新规划」：升版本后**已完成的点原样保留**（不丢已完成
    的工作），未完成的置 ``skipped``，新点写入新版本；执行只推进当前版本的点，
    历史版本仅作留痕（见 ``doc/task-design.md`` 5.7）。
    """

    __tablename__ = "task_items"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("tasks.id"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # pending / doing / done / skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 已执行的对话轮数（一次对话 = 一轮）：配合单点上限防死循环（见 doc 风险 R2）
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conclusion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 过程产物（JSON 数组，可多个），与 session_messages.artifacts 同构
    artifacts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class A2ATask(Base):
    """A2A 任务（协议门面，持久化）。

    只存协议态：state / artifacts / 挂起问题 / history 摘要 / 指回本地 session 的
    metadata。ReAct 执行仍在 ``chat_sessions`` / ``react_steps``，本表不承载执行。

    ``context_id`` 入站时 = ``chat_sessions.id``（A2A contextId），出站时 = ``threads.id``。
    """

    __tablename__ = "a2a_tasks"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # taskId
    context_id: Mapped[str] = mapped_column(String(26), index=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    status_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON(Message)
    artifacts: Mapped[str] = mapped_column(Text, default="[]")  # JSON(list[Artifact])
    history: Mapped[str] = mapped_column(Text, default="[]")  # JSON(list[Message])
    meta: Mapped[str] = mapped_column("metadata", Text, default="{}")  # JSON(dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AgentCapabilityOverride(Base):
    """某 agent 对某一件能力的**本地开关**（覆盖平台 binding）。

    为什么单独一张表而不是改 ``agent_binding.enabled``：平台同步是
    **先删光再按平台数据重建**（``_replace_bindings``），写在那里的开关会被冲掉。

    分工：
    - ``kind=builtin``：内置工具**只归 keeper**，平台没有这个概念，
      因此开关**只看本表**（无记录 = 默认启用，见 registry.BUILTIN_TOOLS）；
    - ``kind=tool/mcp/skill``：平台决定「有哪些能力」（binding），
      本表决定「本地开不开」——有记录就以本表为准，覆盖 binding.enabled。
    """

    __tablename__ = "agent_capability_overrides"
    __table_args__ = (
        UniqueConstraint("agent_id", "kind", "ref_name", name="uq_agent_cap"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    # builtin / tool / mcp / skill
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 能力名：内置=工具名(fs.list_dir)，其余=资源表 name
    ref_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class A2AOutboundTask(Base):
    """出站 A2A 任务（本地索引）：我发起、运行在**对端**的 task。

    与入站 ``a2a_tasks`` 分开建表：入站 task 由我拥有（完整协议态），
    出站 task 的状态由对端掌握，本表只做**本地索引**——记录「我发起了哪些
    task、在哪个对端、最后已知状态」，让本地能查「有什么 task 在对端跑」，
    不必每次去对端拉。详情可凭 ``id``（对端 taskId）调对端 ``GetTask`` 刷新。

    ``id`` = 对端返回的 taskId；``context_id`` = 我方的协议 context（= threads.context_id）。
    """

    __tablename__ = "a2a_outbound_tasks"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # 对端 taskId
    context_id: Mapped[str] = mapped_column(String(26), index=True, nullable=False)
    peer: Mapped[str] = mapped_column(String(64), nullable=False)  # 对端标识
    remote_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 对端 A2A 端点
    thread_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("threads.id"), nullable=False, index=True
    )
    local_session_id: Mapped[Optional[str]] = mapped_column(
        String(26), nullable=True, index=True
    )
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    snapshot: Mapped[str] = mapped_column(Text, default="{}")  # 对端返回的完整 task（JSON）
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AgentPeer(Base):
    """某 agent 要经 A2A 调用的对端（外部 agent，或本进程内另一个 agent）。

    运行时生效层在 ``KeeperAgent.peers``（内存 dict），本表是**持久化层**：
    启动时 load 进前者，运行时增删写回本表，重启不丢。这样「运行时添加 peer
    端点」不会因进程重启而丢失。

    ``name`` = 对端标识，同时是工具名前缀：``{name}__{skill}``。
    """

    __tablename__ = "agent_peers"
    __table_args__ = (UniqueConstraint("agent_id", "name", name="uq_agent_peer"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # 对端标识（工具名前缀）
    a2a_url: Mapped[str] = mapped_column(Text, nullable=False)
    headers: Mapped[str] = mapped_column(Text, default="{}")  # JSON(dict)
    # 来源：``local``=本进程内 agent；``external``=外部 agent。
    # 为空表示「按 a2a_url 自动判定」——添加时就能确定，不必让用户手填。
    source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # 是否走系统代理。None=按来源自动推导：local 直连(False，否则 localhost
    # 会被送去代理→502)，external 走环境代理(True)。
    trust_env: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UserSpace(Base):
    """用户空间：用户自管的命名文件路径（agent 在此目录下干活的主目录）。

    一个用户可配很多个；每个会话通过 ``ChatSession.user_space_id`` 绑定其中之一。
    agent 自身不再绑定工作空间（见 ChatSession 注释）。
    """

    __tablename__ = "user_space"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 绝对路径。``default`` 是唯一真实目录（workspace/user/default）；其余空间在
    # workspace/user/<name> 建软链接指向用户真实文件夹，path 存的就是这个软链接路径。
    path: Mapped[str] = mapped_column(Text, nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)  # 默认只读（安全）
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<UserSpace {self.name} {self.path}>"



class Agent(Base):
    """用户创建的 agent：身份 + 工作区 + 模型。

    能力不写在这张表里，靠 ``agent_binding`` 挂——这样加一件能力只是多一行，
    不用改 agent 本身。

    ``persona`` 就是创建时写的那段**预设提示词**（"你是谁、怎么回答"），
    装配时注入 system。它与 skill 的区别：persona 始终生效、且只有一个；
    skill 可随时增减。

    ``llm`` 为空时回退到全局默认（config.yaml 的 llm 段）。
    """

    __tablename__ = "agent"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    # 来源：``platform``=从平台同步下来的副本（默认），``local``=本机创建。
    #
    # 必须显式记：两类 agent 混在同一张表里，而 id 分别来自平台和本地 ULID，
    # **没有别的字段能区分它们**。不记的后果有两个：同一个 agent 在 UI 上显示
    # 两遍（本地列表 + 平台列表），以及更糟的——本地改了平台 agent 的绑定后，
    # 下一次 ``sync_agent`` 的「先删后插」会把改动静默冲掉。
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="platform", server_default="platform"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 预设提示词（人设）：始终生效，注入 system 的身份部分
    persona: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # 留空 → 自动用 ~/.keeper/workspace/user/<name>（每个 agent 都有自己的默认空间）
    workspace_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 这里**没有** workspace_read_only：只读是**工作空间**的属性，真源在
    # ``user_space.read_only``（装配期与运行期都只读那一列）。
    # 曾经在这存一份，结果两处各判各的：user_space 明明可写，装配期却按这里的
    # 默认 true 把写类工具全丢了，且工具在界面上凭空消失、无任何报错。
    # 该列已由 ``_drop_agent_legacy_columns`` 从存量库里删掉。
    # 大模型配置（JSON dict）；当前 api_key 明文存储，与 mcp 的凭证一致
    llm: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ---- 生命周期状态 ----
    # active  ：启用中——可被装配、可服务
    # disabled：已停用——用户主动关掉，配置保留，随时能再启用
    # archived：已废弃——不再使用，不参与装配；留记录只为追溯
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    # ---- 本地装载态（与 status 分离，本地专属）----
    # status 归平台（同步时按平台数据覆盖，见 plat.sync._upsert_agent_row）；
    # 本列只记「本地有没有装载」——卸载置 False，重启就不再自动装配；
    # 点「装载」重新置 True。平台同步不写这一列，所以不会被冲掉。
    loaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ---- 各类资源的「下载目录」覆盖（仅覆盖用，默认留空） ----
    # 默认每个 agent 独立目录（决策 #4）：~/.keeper/agents/<agent_id>/<kind>；
    # 显式配置才用这里的值（需要版本 / 权限隔离、或与其它 agent 共用时）。
    # 注意：这跟 keeper 自带的连接器缓存 ~/.keeper/mcp 是两码事——后者是
    # keeper 自己的能力，前者是「agent 从平台拉下来的代码」，互不干扰。
    mcp_cache_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_cache_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skill_cache_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )



class LLMProfile(Base):
    """可复用的模型预设（模型管理页配置）。

    api_key 明文存储（本地运行时，模型归属客户端、平台不维护；与 mcp 凭证一致）。
    """

    __tablename__ = "llm_profiles"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    # 展示名（下拉里选「用哪个模型」就用它）；唯一，避免重名
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    temperature: Mapped[float] = mapped_column(default=0.2)
    timeout: Mapped[int] = mapped_column(default=120)
    max_retries: Mapped[int] = mapped_column(default=3)
    # 成本换算单价（每百万 token）。
    # 挂 profile 而非按 model_name 建表：同一 model_name 可能有多个预设（不同
    # provider / base_url / 密钥），价格也可能不同，挂在 profile 上天然一对一。
    # 以官方公布为准、随时可改；为 NULL 时该预设算不出成本（展示为「—」）。
    input_price_per_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cached_price_per_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    output_price_per_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    # 模型上下文上限（token）。用于**上下文水位告警**：ReAct 多轮很容易把
    # prompt_tokens 顶到上限，逼近时提前告警，别等真爆了才发现。
    context_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def to_llm_dict(self) -> dict:
        """转成 get_llm 接受的配置 dict（带字段默认值兜底）。"""
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "temperature": self.temperature if self.temperature is not None else 0.2,
            "timeout": self.timeout if self.timeout is not None else 120,
            "max_retries": self.max_retries if self.max_retries is not None else 3,
        }


class AgentLLMBinding(Base):
    """agent → 模型预设 的绑定（本地归属，独立于平台同步的 agent 缓存）。

    放在独立表而非 ``agent`` 列，是为了不和 ``plat.sync`` 重写 agent 行时冲突——
    模型归属是本地决定，平台同步不应覆盖它。
    """

    __tablename__ = "agent_llm_binding"

    agent_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("agent.id"), primary_key=True
    )
    llm_profile_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("llm_profiles.id"), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class LLMCall(Base):
    """一次 LLM 调用的用量记录（可观测**事实表**）。

    每次 LLM 调用落一行，带归属（agent / session / message / step / task）；
    step / 消息 / 任务 / agent 各级用量全部由本表**聚合**得出，不在各表冗余
    token 列——避免多份数据不一致。

    为什么是「每次调用一行」而不是「累计值做差」：``LLM`` 是 agent 级单例，其
    ``usage`` 跨会话累加，靠快照做差在并发下会把别的会话的消耗算进来，且无法
    归属到具体 step / 消息。

    ``cached_tokens`` 可空是有意为之：``None`` = provider **未上报**，``0`` =
    上报了但**确实没命中**。两者必须区分，否则「没统计到」会被误读成「没命中」。

    DeepSeek 语义：``prompt_tokens = cache_hit + cache_miss``，命中部分已包含在
    prompt_tokens 内，算 miss 时用 ``prompt - cached``，不要重复相加。
    """

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    # 预留：跨端 / 跨服务链路串联（本期不消费，为将来接外部 APM 留口子）
    trace_id: Mapped[Optional[str]] = mapped_column(
        String(26), nullable=True, index=True
    )
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    # step 落库后回填：打点时 step 还没写库（先执行后落库），只能先记 step_seq
    step_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    step_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    task_item_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    # react_step / final_summary / turn_summary / peer / other
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="other")
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    profile_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("llm_profiles.id"), nullable=True
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 思维链 token（reasoning 模型才有）：是 completion_tokens 的**子集**，已包含在
    # 输出 token 内，不另计入 total；None=provider 未上报，0=非 reasoning 模型。
    reasoning_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 首字延迟
    is_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class ModelPrice(Base):
    """模型单价，**按生效时间段**存储（可有多段，覆盖调价历史）。

    为什么必须有时间段：模型会调价。若只存"当前价"，三个月前那次调用就会被
    按今天的价重算——历史成本全错。成本换算必须按**调用发生时**生效的那段价。

    三个价格维度（对应 DeepSeek 的计费语义）：

    - ``input_price_per_1m``  ：**未命中缓存**的输入单价
    - ``cached_price_per_1m`` ：**命中缓存**的输入单价（显著更低）
    - ``output_price_per_1m`` ：输出单价

    ``effective_to`` 为 NULL 表示"至今仍有效"（即当前价）。
    匹配优先级：``profile_id`` > ``model_name``+``provider`` > 仅 ``model_name``
    （agent 用内联 llm 配置、没绑预设时走后两级）。
    """

    __tablename__ = "model_prices"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    profile_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("llm_profiles.id"), nullable=True, index=True
    )
    model_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 一天内的**时段**（本地时间 00:00 起的分钟数，0–1439），用于错峰计费：
    # 如 DeepSeek 低峰时段单独一段更便宜的价。两者都为 NULL = 全天通用价（兜底）。
    # 跨午夜（如 22:00–02:00）用 from > to 表示。
    time_from_minute: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_minute: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 适用**星期**：逗号分隔的 ISO 星期号（1=周一 … 7=周日），如 "1,2,3,4,5"。
    # 空 / NULL = 不限星期（每天都适用）。与时段是 AND 关系。
    weekdays: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    input_price_per_1m: Mapped[float] = mapped_column(Float, nullable=False)
    cached_price_per_1m: Mapped[float] = mapped_column(Float, nullable=False)
    output_price_per_1m: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class ToolCall(Base):
    """一次**工具调用**的执行记录（可观测：工具维度统计）。

    与 ``llm_calls`` 平行：那张表记 LLM，这张记工具。工具维度的价值在于定位
    「哪个工具最慢 / 最常失败 / **返回最大**」——返回大的工具往往就是把上下文
    撑爆的元凶（它的输出会原样进下一轮 prompt）。

    ``output_size`` 记的是**真正进入上下文**的长度（影响 token 的是它）；
    ``raw_output_size`` 是工具原本返回的长度。两者不等即说明给模型的文本被缩减过
    （``truncated=True``）——超内联上限（``observation_limit``）时会先外部化成
    「预览 + block_id」，外部化不可用时才硬截断。常被缩减说明该工具返回需要精简。
    """

    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    trace_id: Mapped[Optional[str]] = mapped_column(
        String(26), nullable=True, index=True
    )
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    step_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    step_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    task_item_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    args_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 入参哈希（sha1 前 12 位）：用于识别「同样的参数又调了一遍」= 空转 / 绕圈子。
    # 只存哈希不存原文——原文可能是整份代码，入库太重。
    args_hash: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    output_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_output_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class CapabilityLoad(Base):
    """一次「能力加载」事件：某份正文 / 某组工具定义进入了上下文。

    为什么不复用 ``tool_calls``：三条加载路径里**只有一条**是模型主动发起的工具
    调用，另外两条是

    - ``preload``       —— 轮开始时系统替模型把技能正文取来；
    - ``auto_disclose`` —— 模型直接调用了只见过名字的工具，执行前自动补定义。

    但三条消耗的是同一份上下文，**必须一起统计**才能回答「这个能力到底被取进来
    几次」——只统计模型主动 load 会漏掉另一半，结论必然是错的。

    这张表用来回答三个问题：

    - ``preload`` 占比高 → L1 摘要写得不准，系统总在猜；
    - 同轮重复加载 → **抖动**：模型不确定自己读过没有，摘要 / 提示没给够；
    - ``auto_disclose`` 占比高 → 工具被折叠得太狠（或没写组摘要），模型只猜名字。
    """

    __tablename__ = "capability_loads"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    trace_id: Mapped[Optional[str]] = mapped_column(
        String(26), nullable=True, index=True
    )
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    # 技能名 / 工具全名 / 工具组名
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # skill / tool / tool_group
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="skill")
    # preload / model_load / auto_disclose
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="model_load")
    # 注入上下文的字符数（比 token 更直观：它就是实际占掉的 prompt 体积）
    chars: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )


class ReactParseStat(Base):
    """一次 LLM 响应的**结构**统计：ReAct 解析出了什么、有没有多余动作被丢。

    只为回答一个问题：**模型到底想并行吗？** prompt 明确要求「每次响应只写一个
    ACTION」，但模型经常连写两个（见 planner 里那段注释）。现状是第二个被解析器
    丢弃——按 prompt 办没错，但这份「想并行」的需求白白浪费了。

    没有这张表之前，只能靠感觉决定要不要做并行化；有了它就是数字：
    ``multi_rate`` 高说明并行化有真实收益；``dropped_total`` 高说明已经在白丢。
    """

    __tablename__ = "react_parse_stats"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    trace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True, index=True)
    # final / ask / act / think：这一步的解析结果
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="act")
    # 响应里出现了几个 ACTION（不含 ACTION_INPUT）
    action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 其中被丢弃的个数（现状 = action_count - 1）
    dropped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )


@event.listens_for(Task, "before_update")
def _sync_task_time_on_status(mapper, connection, target) -> None:
    """任务状态流转时自动维护 ``started_at`` / ``finished_at``（可观测：任务耗时）。

    用事件监听而不是在每个接口里手写：状态流转点有十余处（计划审批、执行推进、
    验收通过、验收打回、补充输入、取消……），逐个写必漏，也会侵入业务代码。

    - 首次进入 ``executing`` → 记 ``started_at``（不覆盖：打回后重跑沿用首次）；
    - 进入终态 ``done`` / ``failed`` / ``cancelled`` → 记 ``finished_at``。
    """
    try:
        hist = inspect(target).attrs.status.history
        if not hist.has_changes():
            return
        new = target.status
        if new == "executing" and target.started_at is None:
            target.started_at = _utcnow()
        elif new in ("done", "failed", "cancelled") and target.finished_at is None:
            target.finished_at = _utcnow()
    except Exception:  # 时间戳维护失败绝不能阻断状态流转
        pass
