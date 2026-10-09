"""框架配置：keeper 这个**进程**怎么跑。

只放跟部署环境有关的东西，不放任何 agent 的能力：

- ``KeeperConfig``：**进程级**——服务怎么跑（端口、鉴权、**用哪个 agent 画像**）。
  部署时定，不进数据库，和具体 agent 无关。

agent 画像（``AgentConfig`` / ``WorkspaceConfig`` / ``default_workspace_root``
等）已移到 ``keeper.agent.config``——它们描述「这个 agent 是谁、有什么能力」，
由装配器从数据库的 agent 表读出并填充，框架不认识 MCP / skill / tool。这条分界
也是将来支持多 agent 的前提：一个进程一份框架配置，但可以装多个 agent 画像。

（旧版的 ``AgentConfig.from_dict`` 及 env 展开 / mcp.json 读取等 YAML 配置路径
已删除——agent 完全由数据库装配，不再有对应的 yaml 文件。）
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ServerAuth:
    """入站身份配置（可选开启，暂不鉴权）。"""

    enabled: bool = False
    default_initiator_id: Optional[int] = None


@dataclass
class WorkspaceSection:
    """本地工作区默认值。

    **平台不维护工作区**——工作区在哪、是否只读，是客户端自己的事。
    ``root`` 下按 agent 名字各自一个目录；``path`` 不指定时装配器会自动算。
    """

    kind: str = "local"
    root: str = "~/.keeper/workspace/user"
    read_only: bool = False


@dataclass
class PlatformSection:
    """平台（配置之源）的接入信息。

    本地应用凭 token 调 ``GET /api/me/agents`` 拉"我已添加"的 agent 配置就地装载。
    平台不参与运行时协作，这里只存"去哪拉、凭什么拉"。
    """

    url: str = "http://localhost:9095"
    token: str = ""


@dataclass
class FrameworkSection:
    """framework 运行时检测配置。

    默认检测系统 PATH 上的 python / node。若本机解释器不在 PATH，或想固定用
    某个版本，可在下面显式指定可执行文件路径，或给额外搜索目录 / 文件。
    命令行环境变量 ``KEEPER_PYTHON`` / ``KEEPER_NODE`` 可临时覆盖显式路径。
    """

    python: str = ""  # 显式指定 python 可执行文件（绝对路径）
    node: str = ""  # 显式指定 node 可执行文件（绝对路径）
    extra_paths: List[str] = field(default_factory=list)  # 额外候选路径 / 目录


@dataclass
class PromptDumpSection:
    """调试用：把发给大模型的**完整 prompt** 落盘到本地文件。

    默认关闭——完整 prompt 动辄几万字，全量写盘很快吃满磁盘，而且只有排查
    （上下文膨胀、模型答非所问、某步为什么这么慢）时才有价值，所以做成开关。

    落盘结构 ``<dir>/<session_id>/<message_id>/<时间戳>.json``，与 UI 的
    「会话 → 某一轮」一致：从某一轮点进去就能直接找到对应文件。

    min_prompt_tokens：只记输入 token 超过该值的调用（0 = 全记）。排查上下文
    膨胀时设阈值（如 50000），就只留真正要分析的那几次超大调用。
    """

    enabled: bool = False
    dir: str = "~/.keeper/prompt-dump"
    min_prompt_tokens: int = 0
    retain_days: int = 7


@dataclass
class ContextSection:
    """上下文治理：工具输出外部化 + 自动压缩（见 doc/context-design.md）。

    ReAct 每步把 THOUGHT/ACTION/OBSERVATION 全量累加进 messages，长任务下输入
    token 线性膨胀。这里的两件事都是为了**在中途把它压下来**：

    - externalize_*：工具输出超阈值就写盘，只把「首尾预览 + block_id」留在上下文；
    - compact_*   ：输入 token 到水位时，把最早的一批步骤压成「骨架 + block_id」。

    阈值都做成配置，因为最优值取决于模型窗口与单价：窗口大就晚点压，
    缓存贵就少压几次。
    """

    enabled: bool = True
    # ---- 外部化 ----
    # 外部化有两条触发路径，阈值各管一条：
    #   a) 单条超长：结果超过 observation_limit 就先落盘 + 给 block_id
    #      （用 observation_limit 当阈值，不看下面这个值）；
    #   b) 滚动外部化：「已退出保活窗口」且「超过这个长度」。
    # 默认 5000 而不是更小：外部化会**改写中段消息**，同样破坏前缀缓存。
    # 实测 2000 字符就换引用时，省下的 token 远抵不上后续缓存失效的代价。
    # 注意别把这个值调到 >= observation_limit：那条路径的判定用的是
    # observation_limit，调高只会让「刚返回就超长」的结果重新变成硬截断。
    externalize_min_chars: int = 5000  # 0 = 不做滚动外部化（单条超长仍会外部化）
    # 豁免名单：填了的工具不外部化（滚动与单条两条路径都不生效）；
    # 默认空 = 一视同仁
    never_externalize: str = ""
    # 外部化块的保留天数（过期自动清理，最多每小时扫一次；0 = 不清理）。
    # 必须回收：外部化会持续产块，且「先外部化、后又被压缩打包」会留下不再被
    # 引用的旧块，没有回收磁盘会一路涨。
    retain_days: int = 7
    # ---- 单条输出与步数（直接影响「一轮能涨到多大」）----
    # 单条输出的**内联上限**，同时是「单条超长就外部化」的触发线：
    # 超过它 → 原文落盘 + 上下文里换成「预览 + block_id」（模型可 read 追回）；
    # 外部化不可用时（总开关关 / never_externalize 命中 / 写盘失败）才退回硬截断，
    # 那时它砍掉的中段没有 id 可读，所以截断文案里写明了「怎么缩小范围重试」。
    #
    # 调小的代价不是「看不全」，而是**多一次 read**：内联上限以下的内容模型本来
    # 会一口气读完，调小等于让它先看预览再把整块读回来，净 token 反而上升。
    # 所以别按「省 token」调它，按「多大的结果模型不会读完」调。
    observation_limit: int = 3500
    max_steps: int = 30          # 一轮 ReAct 最多跑多少步
    # ---- 取回与估算 ----
    read_budget: int = 6000      # read 展开多块时的总字符预算
    max_ctx_recall: int = 5      # recall 返回的「本轮外部化块」上限
    chars_per_token: float = 2.5  # token 粗估系数（中文≈1.5，英文≈4），只用于算水位
    # 追回（read）回来的内容保活几条：0 = 不保（走出窗口就再外部化）。
    # 保太少会出现「读了 → 被压 → 又读」的抖动；保太多则上下文重新膨胀。
    pin_recent_reads: int = 2
    preview_head: int = 400            # 留在上下文里的开头字符数
    preview_tail: int = 200            # 结尾字符数（结论/报错常在最后）
    dir: str = "~/.keeper/context-store"
    # ---- 压缩 ----
    compact_enabled: bool = True
    model_context_limit: int = 64000   # 模型窗口（token），用于算水位
    # 单次 LLM 输出的 token 上限。设 0 = 不限制，交给模型 / provider 默认。
    # 与上面的输入窗口是**一件事的两侧**：窗口决定「能进来多少」，它决定
    # 「一次能出去多少」。
    #
    # 为什么要有（而不是"不设就够"）：ReAct 的 ACTION_INPUT 里常常塞着整份文件 /
    # 代码，一次输出上万字符很容易撞上 provider 的输出上限而被**静默截断**——
    # 表现是参数 JSON 不完整、工具莫名失败，而模型自己完全不知情（它那边只是
    # "输出结束了"）。实测就是这么把一个任务烧到步数上限的。
    #
    # 默认 16384 的由来：不少 provider（DeepSeek 的 deepseek-chat 就是）的
    # **默认 max_tokens 只有 4096**，也就是"什么都不配"其实已经在被截了，只是没人
    # 告诉你。16384 是它的 4 倍，够写完一个大文件的一半又不至于让一次响应失控；
    # 设了之后提示词会把这个数字告诉模型，让它按预算分段输出
    # （见 planner._output_budget_section）。
    max_output_tokens: int = 16384
    compact_ratio: float = 0.7         # 用到窗口的这个比例就压
    compact_min_steps: int = 20        # 步数触发的步数线（**且**要过下面的水位下限）
    # 步数触发的水位下限。取 0.7（= 真水位）是经过实测的：23k token（窗口 36%）
    # 时压缩，缓存命中率会从 97% 掉到 68%，**每步输入成本反而贵 13%**——
    # 省掉的多是命中价的便宜 token，却把后续一批变成未命中价。所以步数触发
    # 只在「确实很胖」时才生效，压缩定位是防爆窗，不是省钱。
    compact_min_ratio: float = 0.7     # 步数触发的水位下限：不到它，步数再多也不压
    compact_min_interval: int = 5      # 两次压缩之间至少隔几步（防抖动）
    keep_recent_steps: int = 6         # 最近这几步永远保留原文
    compact_max_chars: int = 1500      # 生成的骨架摘要上限
    # ---- 技能按需加载（见 doc/capability-loading-design.md）----
    # 每轮开头系统要不要替模型把技能正文取来、以及怎么挑：
    #   off     = 不预加载：只有概要常驻，正文等模型自己按名字读（最纯粹的按需）
    #   keyword = 拿技能 keywords 跟用户问题做包含匹配，命中才取（零成本）
    #   llm     = 让 LLM 看一眼清单挑名字（一次便宜判读）
    #   auto    = 技能少就直接全取（见 cap_small_limit），多了先 keyword、没命中再判读
    capability_preload: str = "auto"
    # 工具清单折叠：同一组（插件 / MCP server）里的工具达到几个就折成一行，
    # 只列工具名、不列参数说明（真正调用时自动把完整定义补进上下文）。
    # 1 = 从不折叠（等同改造前的全量清单）；调大更省 token，代价是模型可能多一次补载。
    tool_group_min: int = 3
    # 「技能少」的判定线：按需技能的正文 token 粗估合计低于它就**全部取来**，
    # 连判读都不做——这时省下的 token 抵不过多一次调用，也没必要让行为不可预测。
    cap_small_limit: int = 6000
    # ---- 工具并行（P1-1）----
    # 0 = 关闭（默认，行为与从前完全一致）；1 = 并行「只读」工具；
    # 2 = 只读 + 插件清单里显式标了 mutating: false 的工具。
    # 分级而不是一个开关：哪些工具能并发需要作者自己判断，代码替不了。
    tool_parallel: int = 0
    # 单轮最多并发几个：防止一次几十个把 MCP server 或连接池打爆
    tool_parallel_max: int = 5


@dataclass
class A2ASection:
    """A2A 出站调用的超时与熔断（本端 → 对端）。

    对端是**别人的进程**，快慢与可用性都不由我们控制：没有超时，一次调用能把
    整轮对话挂死（实测过「请求一直不返回」）；没有熔断，对端挂了之后每轮还会
    老老实实去拨，白白拖慢每一轮。

    - request_timeout：单次 HTTP/RPC 超时（发消息、查状态、拉 AgentCard 都用它）
    - task_timeout  ：等一个 task 走到终态的**总时长**；0 = 不限（不推荐）
    - poll_interval ：轮询间隔（等终态时查 GetTask 的频率）
    - cancel_on_timeout：超时后主动 CancelTask，别让对端继续白跑
    - breaker_*     ：连续失败达到阈值就短期熔断，冷却期内直接快速失败
    """

    request_timeout: float = 15
    task_timeout: float = 120
    poll_interval: float = 3
    cancel_on_timeout: bool = True
    breaker_enabled: bool = True
    breaker_threshold: int = 3
    breaker_cooldown: float = 60


@dataclass
class ObservabilitySection:
    """可观测相关的本地配置。"""

    prompt_dump: PromptDumpSection = field(default_factory=PromptDumpSection)


@dataclass
class PluginSection:
    """插件库配置。

    **插件库是一个目录，不是一种来源。** 手写一个插件包丢进去、用
    plugin-authoring 写完放进去、将来远程下载安装——都只是"往这个目录里放东西"
    的不同方式。装载逻辑只认"库里现在有什么"，不关心它从哪来，所以加远程
    安装时不需要动装配链路，只需要多一个往目录里写东西的实现。

    因此 ``root`` 是元配置（部署时定、跟代码走）而不是用户设置：换机器 / 多台
    机器各放不同位置时改这里，不要在代码里硬编码插件路径。
    """

    # 插件库根目录。每个子目录只要含 keeper-plugin.json 就算一个插件。
    # 放在 ~/.keeper 下而不是仓库里：插件包（含 283MB 的 MCP 二进制）不该进
    # 版本库，也不该跟着代码目录搬家——换个机器只改这一行（或设 PLUGIN_ROOT）。
    root: str = "~/.keeper/plugins"


@dataclass
class KeeperConfig:
    """框架配置：keeper 这个**进程**怎么跑。

    只放跟部署环境有关的东西，不放任何 agent 的能力。
    """

    host: str = "0.0.0.0"
    port: int = 8080
    auth: ServerAuth = field(default_factory=ServerAuth)
    # 用哪个 agent 画像：装配器据此去数据库的 agent 表读（name 与 id 二选一）
    agent_name: Optional[str] = None
    agent_id: Optional[str] = None
    # 上游配置平台（拉 agent 定义用）
    platform: PlatformSection = field(default_factory=PlatformSection)
    # framework 运行时下载源（镜像 / 自建）
    framework: FrameworkSection = field(default_factory=FrameworkSection)
    # 本地默认大模型配置：**平台不维护 model**，用哪个模型是客户端自己的事
    llm: Dict[str, Any] = field(default_factory=dict)
    # 本地默认工作区（平台同样不维护）
    workspace: WorkspaceSection = field(default_factory=WorkspaceSection)
    # 按 agent（id 或 name）的本地覆盖：{"旅游小助手": {"llm": {...}, "workspace": {...}}}
    agent_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 可观测（调试开关等）
    observability: ObservabilitySection = field(
        default_factory=ObservabilitySection
    )
    # A2A 出站（调对端）的超时与熔断
    a2a: A2ASection = field(default_factory=A2ASection)
    # 上下文治理（工具输出外部化 + 自动压缩）
    context: ContextSection = field(default_factory=ContextSection)
    # 插件库（插件目录从哪读）
    plugins: PluginSection = field(default_factory=PluginSection)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "KeeperConfig":
        d = dict(d)
        server_section = d.pop("server", {}) or {}
        auth_section = server_section.get("auth") or {}
        agent_section = d.pop("agent", {}) or {}
        platform_section = d.pop("platform", {}) or {}
        framework_section = d.pop("framework", {}) or {}
        llm_section = d.pop("llm", {}) or {}
        workspace_section = d.pop("workspace", {}) or {}
        overrides_section = d.pop("agent_overrides", {}) or {}
        observability_section = d.pop("observability", {}) or {}
        pd_section = observability_section.get("prompt_dump") or {}
        a2a_section = d.pop("a2a", {}) or {}
        ctx_section = d.pop("context", {}) or {}
        plugins_section = d.pop("plugins", {}) or {}
        return cls(
            host=server_section.get("host") or "0.0.0.0",
            port=int(server_section.get("port") or 8080),
            auth=ServerAuth(
                enabled=bool(auth_section.get("enabled", False)),
                default_initiator_id=auth_section.get("default_initiator_id"),
            ),
            agent_name=agent_section.get("name"),
            agent_id=agent_section.get("id"),
            platform=PlatformSection(
                url=platform_section.get("url") or "http://localhost:9095",
                token=platform_section.get("token") or "",
            ),
            framework=FrameworkSection(
                python=framework_section.get("python") or "",
                node=framework_section.get("node") or "",
                extra_paths=list(framework_section.get("extra_paths") or []),
            ),
            llm=llm_section,
            workspace=WorkspaceSection(
                kind=workspace_section.get("kind") or "local",
                root=workspace_section.get("root") or "~/.keeper/workspace/user",
                read_only=bool(workspace_section.get("read_only", False)),
            ),
            agent_overrides=overrides_section,
            observability=ObservabilitySection(
                prompt_dump=PromptDumpSection(
                    enabled=bool(pd_section.get("enabled", False)),
                    dir=pd_section.get("dir") or "~/.keeper/prompt-dump",
                    min_prompt_tokens=int(pd_section.get("min_prompt_tokens") or 0),
                    retain_days=int(pd_section.get("retain_days") or 7),
                ),
            ),
            a2a=A2ASection(
                request_timeout=float(a2a_section.get("request_timeout") or 15),
                # task_timeout 允许 0（不限），所以不能用 `or 默认`
                task_timeout=(
                    float(a2a_section["task_timeout"])
                    if a2a_section.get("task_timeout") is not None
                    else 120.0
                ),
                poll_interval=float(a2a_section.get("poll_interval") or 3),
                cancel_on_timeout=bool(a2a_section.get("cancel_on_timeout", True)),
                breaker_enabled=bool(a2a_section.get("breaker_enabled", True)),
                breaker_threshold=int(a2a_section.get("breaker_threshold") or 3),
                breaker_cooldown=float(a2a_section.get("breaker_cooldown") or 60),
            ),
            context=ContextSection(
                enabled=bool(ctx_section.get("enabled", True)),
                externalize_min_chars=int(ctx_section.get("externalize_min_chars") or 5000),
                never_externalize=ctx_section.get("never_externalize") or "",
                retain_days=int(ctx_section.get("retain_days") or 7),
                observation_limit=int(ctx_section.get("observation_limit") or 3500),
                max_steps=int(ctx_section.get("max_steps") or 30),
                read_budget=int(ctx_section.get("read_budget") or 6000),
                max_ctx_recall=int(ctx_section.get("max_ctx_recall") or 5),
                chars_per_token=float(ctx_section.get("chars_per_token") or 2.5),
                pin_recent_reads=int(ctx_section.get("pin_recent_reads") or 2),
                preview_head=int(ctx_section.get("preview_head") or 400),
                preview_tail=int(ctx_section.get("preview_tail") or 200),
                dir=ctx_section.get("dir") or "~/.keeper/context-store",
                compact_enabled=bool(ctx_section.get("compact_enabled", True)),
                model_context_limit=int(ctx_section.get("model_context_limit") or 64000),
                compact_ratio=float(ctx_section.get("compact_ratio") or 0.7),
                compact_min_steps=int(ctx_section.get("compact_min_steps") or 20),
                compact_min_ratio=float(ctx_section.get("compact_min_ratio") or 0.7),
                compact_min_interval=int(ctx_section.get("compact_min_interval") or 5),
                keep_recent_steps=int(ctx_section.get("keep_recent_steps") or 6),
                compact_max_chars=int(ctx_section.get("compact_max_chars") or 1500),
                # 也允许写在 config.yaml（元配置）；正常走 settings.yaml 那条路
                max_output_tokens=int(ctx_section.get("max_output_tokens") or 0),
            ),
            plugins=PluginSection(
                root=plugins_section.get("root") or "~/.keeper/plugins"
            ),
        )


def _resolve(path) -> Path:
    """把配置路径解析成绝对路径。

    相对路径先按**当前目录**找（命令行里写 `keeper/config.yaml` 是相对项目根的），
    找不到再退回按 keeper 包目录算（写 `config.yaml` 时）——两种写法都能用。
    """
    p = Path(path)
    if p.is_absolute():
        return p
    if p.exists():
        return p
    return Path(__file__).parent / p


def load_config(path: Optional[str | os.PathLike] = None) -> KeeperConfig:
    """加载**框架**配置（默认 config.yaml），再用**用户设置**覆盖可调段。

    两段的关系：``config.yaml`` 是出厂默认（运维手改），``settings.yaml`` 是
    用户在设置页改的（见 ``keeper/setting/user_settings.py``）。后者覆盖前者，所以
    ``prompt_dump`` / ``a2a`` 这类「用的时候调」的配置改完即生效，而元配置
    永远不用被程序回写（也就不会被 pyyaml 抹掉注释）。
    """
    p = _resolve(path) if path else default_config_path()
    if not p.exists():
        raise FileNotFoundError(f"框架配置文件不存在: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = KeeperConfig.from_dict(data)
    _apply_user_settings(cfg)
    return cfg


def _apply_user_settings(cfg: "KeeperConfig") -> None:
    """用 settings.yaml 覆盖用户可调段（只覆盖文件里**显式写了**的键）。"""
    try:
        from .setting import load_raw
    except Exception as e:  # noqa: BLE001
        logger.debug("加载用户设置失败，按元配置运行: %s", e)
        return
    try:
        raw = load_raw()
    except Exception as e:  # noqa: BLE001
        logger.debug("读取用户设置失败，按元配置运行: %s", e)
        return
    if not raw:
        return

    obs = raw.get("observability")
    if isinstance(obs, dict):
        pd = obs.get("prompt_dump")
        if isinstance(pd, dict):
            _overlay(cfg.observability.prompt_dump, pd)

    a2a = raw.get("a2a")
    if isinstance(a2a, dict):
        _overlay(cfg.a2a, a2a)

    ctx = raw.get("context")
    if isinstance(ctx, dict):
        _overlay(cfg.context, ctx)


def _overlay(target: Any, patch: Dict[str, Any]) -> None:
    """把 patch 里出现的键按目标字段的类型写进去（类型不对就跳过，别写坏）。"""
    for k, v in patch.items():
        if v is None or not hasattr(target, k):
            continue
        cur = getattr(target, k)
        try:
            if isinstance(cur, bool):
                setattr(target, k, bool(v))
            elif isinstance(cur, float):
                setattr(target, k, float(v))
            elif isinstance(cur, int):
                setattr(target, k, int(v))
            else:
                setattr(target, k, v)
        except Exception as e:  # noqa: BLE001
            logger.debug("用户设置字段 %s 无效（%s），跳过", k, e)


def default_config_path() -> Path:
    """框架配置默认位置：config.yaml。"""
    return Path(__file__).parent / "config.yaml"


def save_agent_env(agent_id: str, python: str, node: str) -> None:
    """把某个 agent 的 python / node 解释器路径写进 ``agent_overrides``。

    与 ``load_config`` 共用同一份 config.yaml，只动 ``agent_overrides`` 小节，
    其余配置原样保留。传空字符串表示清空（回退到 framework 全局默认值）。

    装载时 ``plat.sync._local_defaults`` 会按 ``agent_overrides`` → 全局默认
    的顺序解析，所以这里写下的路径就是该 agent 实际使用的解释器。
    """
    p = default_config_path()
    data: Dict[str, Any] = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    overrides = data.get("agent_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
        data["agent_overrides"] = overrides
    ov = overrides.get(agent_id)
    if not isinstance(ov, dict):
        ov = {}
        overrides[agent_id] = ov
    ov["python"] = python
    ov["node"] = node
    # 两项都为空则清掉空壳，避免写出无意义的嵌套
    if not python and not node:
        overrides.pop(agent_id, None)
    if not overrides:
        data.pop("agent_overrides", None)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def save_settings(
    *,
    prompt_dump: Optional[Dict[str, Any]] = None,
    a2a: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """保存用户设置（写 ``settings.yaml``，**不碰** config.yaml）。

    设置页的写入口在这里统一转发，调用方不必知道文件到底在哪——也让
    「元配置不被程序回写」这条约束只有一处需要维护。

    ``context`` 必须在这里列出来：设置页提交时会带上整段，而这一层只转发
    **显式列出**的段——漏一个就是运行时 ``TypeError``，而且是在用户点保存
    那一刻才炸，测试里点不到就发现不了。
    """
    from .setting import save_sections

    save_sections(prompt_dump=prompt_dump, a2a=a2a, context=context)
