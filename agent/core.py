"""
核心 agent：应用内生的**主脑**，从本地目录装配，不经过数据库。

为什么它和其他 agent 不一样
--------------------------
其它 agent 是**运行期产物**：可能从平台同步下来、可能被停用、可能这个项目用到
下一个项目不用，所以它们住在 ``agent`` 表里，用 ``origin`` / ``status`` / ``loaded``
记录各自的来路与状态。

核心 agent 是**部署时就该存在**的东西：它没有"未装载"这个状态，不参与平台分发，
也不该被用户在智能体页随手卸载掉（点一下就可能连它的能力代码一起删掉）。把它塞进
同一张表，就得靠这些字段去表达"它其实不一样"——上一版就是这么设计的，代价是核心
agent 依赖数据库里存在一条记录，而那条记录通常来自平台同步。于是产生一个自相矛盾
的局面：**平台成了非运行时必需**，与 ``doc/positioning.md`` 定的原则相反。

所以核心 agent 走一条独立的装配路径：从 ``~/.keeper/core-agent/`` 读定义。
数据库里那条记录（如果有）仍然是普通 agent，与核心角色无关。

目录长什么样
------------
::

    ~/.keeper/core-agent/
    ├── agent.yaml     定义：叫什么、用哪个模型、能力开关
    ├── soul.md        灵魂：人格、语气、价值观（每次对话都带上）
    └── memory/        记忆：见下

``agent.yaml`` 故意**不列能力清单**。核心 agent 只带内置工具（对话、发消息、
记忆 recall/read、A2A 调用）；插件和执行者是"往会话里加入"的东西，不是它的出厂
配置。这样定义文件足够小、足够稳，也避免了"文件里一套能力模型、数据库里另一套"
的双轨问题。

模型**不在这份定义里**——它跟随会话：部署默认值在 ``config.yaml`` 的 llm 段，单次
会话还可以各自选。这与 ``KeeperConfig.llm`` 字段的注释是同一个道理：「平台不维护
model，用哪个模型是客户端自己的事」。一个角色不该焊死在某个模型上。

工作区同理：会话各自绑定 user_space，角色本身不钉路径。

这两个刻意"不声明"，是核心 agent 与普通 agent 最大的区别——普通 agent 的模型与
工作区都存在数据库绑定里，因为那些 agent 是**被配置出来的工件**；核心 agent 是
**用户长期相处的对象**，把它钉死等于把它降级成工件。

记忆
----
会话级记忆（``turns.jsonl`` + FTS5 索引）默认就落在本目录下，见 ``memory_root()``。
换会话 id = 换子目录 = 读不到旧记忆，这是现有实现的行为，本模块不改变它。
跨会话的长期事实与用户画像（roadmap 2.1，尚未实现）也计划落在这里。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---- 目录与文件名 ----

#: 核心 agent 的目录。可用环境变量覆盖（测试、多实例部署用）。
DEFAULT_CORE_DIR = Path(
    os.environ.get("KEEPER_CORE_AGENT_DIR", "~/.keeper/core-agent")
).expanduser()

DEF_FILENAME = "agent.yaml"
SOUL_FILENAME = "soul.md"


def core_agent_dir(cfg: Any = None) -> Path:
    """核心 agent 目录。

    ``config.core_agent.dir`` 显式给了就用它，否则用默认位置。留空是**正常状态**
    （首次启动时目录还不存在），所以这里只解析路径、不要求它存在——建目录是
    :func:`ensure_core_dir` 的事，装配路径只管读。
    """
    if cfg is not None:
        raw = (getattr(getattr(cfg, "core_agent", None), "dir", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
    return DEFAULT_CORE_DIR


def def_path(cfg: Any = None) -> Path:
    return core_agent_dir(cfg) / DEF_FILENAME


def soul_path(cfg: Any = None) -> Path:
    return core_agent_dir(cfg) / SOUL_FILENAME


def memory_root(cfg: Any = None) -> Path:
    """核心 agent 的记忆根目录（会话级记忆落在这里）。

    单独给一个函数而不是让各处自己拼 ``core_agent_dir()/memory``：记忆模块有 4 个
    入口（chat/service 两处、memory/tools 两处），路径只有一个出处才不会漂。
    """
    return core_agent_dir(cfg) / "memory"


def exists(cfg: Any = None) -> bool:
    """目录里是否已有定义文件。

    只看 ``agent.yaml``：``soul.md`` 缺失有默认值（空画像），但没有定义文件就没有
    "叫什么名字、用哪个模型"，装配不出一个 agent。
    """
    return def_path(cfg).is_file()


def ensure_core_dir(cfg: Any = None) -> Path:
    """确保目录与两个文件存在；返回目录。

    **不覆盖已有内容**。首次启动时铺一份带注释的模板，之后就当它不存在——
    这里如果每次启动都重写，用户在 soul.md 里写的东西会被清掉。
    """
    d = core_agent_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)

    df = d / DEF_FILENAME
    if not df.exists():
        df.write_text(DEFAULT_DEF_YAML(), encoding="utf-8")
        logger.info("已生成核心 agent 定义: %s", df)

    sf = d / SOUL_FILENAME
    if not sf.exists():
        sf.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
        logger.info("已生成核心 agent 灵魂文件: %s", sf)

    return d


def _read_text(p: Path, limit: int = 200_000) -> str:
    """读文本文件，失败返回空串而不是抛。

    定义文件是**运行前提**，读不到应该降级成"用默认值继续跑"，而不是让整个进程起
    来——核心角色缺席时应用仍然要能用（只是没有主脑）。
    """
    try:
        return p.read_text(encoding="utf-8")[:limit]
    except OSError as e:
        logger.warning("读取 %s 失败，改用默认值: %s", p, e)
        return ""


# ---- 最小 YAML 读取 ----
#
# 为什么不直接用 pyyaml：定义文件只有固定的几个标量键，为它拉一个依赖不划算；
# 真要嵌套结构时这个解析器会明确报错而不是猜错，那比"半支持"安全。


def _parse_scalar(text: str) -> Any:
    """把 YAML 标量文本转成 Python 值。

    只处理三种写法（够定义文件用了）：带引号的字符串、真假、数字。其它原样返回字符串
    ——所以 ``name: 123`` 会变成 int 123，这是有意为之：用户写 id 时不必加引号。
    """
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    """解析**顶层单层**的 ``key: value``。

    只认顶层键（``缩进 key: value`` 也认，取值去首尾空白）；出现 ``- item``
    列表或二级缩进块时跳过那些行而不是猜结构——本模块的定义文件不需要它们，
    真需要时应该换成完整 YAML 解析（见模块文档的说明）。
    """
    out: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 只取最外层：更深的缩进属于嵌套结构，这里不解释
        if line[:1].isspace():
            continue
        if line.lstrip().startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if not key:
            continue
        out[key] = _parse_scalar(val)
    return out


# ---- 定义与灵魂 ----


class CoreAgentDef:
    """核心 agent 的定义（读自磁盘，不进数据库）。"""

    def __init__(self, raw: Dict[str, Any], soul: str, path: Path):
        self._raw = raw
        self.soul = soul
        self.path = path

    @property
    def name(self) -> str:
        return str(self._raw.get("name") or "Keeper").strip() or "Keeper"

    @property
    def description(self) -> str:
        return str(self._raw.get("description") or "").strip()

    @property
    def persona(self) -> str:
        """画像 = soul.md 全文。

        不用定义文件里的 ``persona`` 字段：灵魂文件是给人写的长文本，放在 YAML 里
        要处理缩进和多行转义，两个入口也容易写岔。保留该字段只为向后兼容——
        两者都写时以 soul.md 为准。
        """
        text = self.soul.strip()
        if text:
            return text
        legacy = str(self._raw.get("persona") or "").strip()
        return legacy

    def to_dict(self) -> Dict[str, Any]:
        """给设置页看的原始定义（保留未知键，避免编辑时把没识别的字段抹掉）。"""
        return dict(self._raw)


def load_core_def(cfg: Any = None) -> Optional[CoreAgentDef]:
    """读核心 agent 的定义与灵魂。

    定义文件不存在 → 返回 ``None``（表示"没配核心 agent"），调用方决定怎么兜底。
    存在但读不出内容 → 返回一个带默认值的 ``CoreAgentDef``，保证"有文件就能跑"。
    """
    p = def_path(cfg)
    if not p.is_file():
        return None
    raw = parse_simple_yaml(_read_text(p))
    soul = _read_text(soul_path(cfg))
    return CoreAgentDef(raw, soul, p)


# ---- 出厂模板 ----

#: 首次启动时铺的 ``soul.md``。写成"可以照着改的范本"而不是一段通用套话——
#: 一份中性的模板读起来就是产品默认值，而产品默认值会直接变成用户的第一次对话体验。
DEFAULT_SOUL_MD = """\
# 灵魂

这里是这个角色的**人格**：每次对话都会带上它。
定义（名字、模型）在同目录的 agent.yaml 里。

下面每一节都写了它在管什么。改完保存立即生效，不用重启。

## 你是谁

（用什么身份陪用户。越具体越好——"你的助手"没有信息量，
  "一个在同一台机器上长期协助的工程搭档"才有。）

## 怎么说话

（语气、节奏、用词习惯。要是你不想让用户看到列表式的回答，就在这里写清楚。）

## 关心什么

（你在意的事。回答时优先照顾什么、忽略什么。）

## 边界

（你不做什么。这一节最容易被忽略，但它是"陪伴"和"讨好"的分界线——
  没有边界的角色会什么都顺着用户说。）

## 会记住什么

（哪些信息值得长期记住。会在 memory/ 下累积，属于你和用户共同的历史。）
"""


def DEFAULT_DEF_YAML() -> str:  # noqa: N802 出厂模板用常量名函数，与常量风格一致
    """首次启动时铺的 ``agent.yaml``。

    **只有一个字段：名字。** 这是刻意的——核心 agent 是"一个角色"，而角色的
    其他一切都不该固定在它身上：

    - **人格 / 语气** → 同目录的 soul.md（那是给人写的长文本）
    - **模型** → 跟随会话。平台不维护模型、用哪个模型是客户端自己的事，
      所以它属于部署配置（config.yaml 的 llm 段）或单次会话，不属于"角色定义"。
    - **工作区** → 跟随会话。会话自己绑 user_space；这里再钉一个路径等于让角色
      永远待在同一个目录里，而陪伴的角色不该被锁死在一处。

    换句话说：``agent.yaml`` 回答"它叫什么"，其余都在别处回答。
    """
    return """\
# 核心 agent
#
# 这个文件只有一个字段：名字。
#
# 因为它是「一个角色」，而角色的其他部分都不该被固定：
#   人格与语气 → 同目录的 soul.md
#   用哪个模型 → 跟随会话（部署默认值在 config.yaml 的 llm 段）
#   工作区     → 跟随会话绑定的用户空间
#
# 刻意不在这里声明插件/能力：核心 agent 只带内置工具，
# 插件与对端是「往某个会话里加入」的东西，不是它的出厂配置。

# 显示名：对话里怎么称呼这个角色
name: Keeper
"""