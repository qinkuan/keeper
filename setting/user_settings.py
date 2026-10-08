"""**用户设置**：设置页里改的那部分配置，独立存放在 ``~/.keeper/settings.yaml``。

为什么和 ``config.yaml`` 分开：

- ``config.yaml`` 是**元配置**（部署时定，跟代码走）：服务端口、鉴权、用哪个
  agent 画像、默认 LLM / 工作区。改它是「运维动作」，通常手改 + 重启。
- ``settings.yaml`` 是**用户设置**（用的时候改，跟人走）：A2A 超时与熔断、
  完整 prompt 落盘、上下文治理。改它是「产品动作」，设置页里点一下就该立即生效。

混在一起有两个实际代价：设置页每次保存都要重写元配置（pyyaml 会**丢掉所有
注释**），而且本地个人偏好会和部署配置一起进版本库。分开之后：

- 保存只写 ``~/.keeper/settings.yaml``，元配置的注释与结构原样不动；
- 用户设置落在本机家目录，换机器 / 重装各用各的，也不会被 git 带走；
- 读取顺序：``settings.yaml`` 覆盖 ``config.yaml`` 的同名段，
  所以元配置里那段仍可当作「出厂默认」，设了就听设置的。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# 本模块原为 ``keeper/user_settings.py``，属设置读写，故并入本包。
# 用户设置文件名（keeper/ 目录下，与 config.yaml 同级）
SETTINGS_FILENAME = "settings.yaml"

# 允许出现在 settings.yaml 里的段：只有「设置页会改」的东西才准进，
# 免得这个文件慢慢变成第二个 config.yaml。
KNOWN_SECTIONS = ("observability", "a2a", "context")


def settings_path() -> Path:
    """用户设置文件路径：``~/.keeper/settings.yaml``。

    为什么放家目录而不是包目录：它描述的是「**这个人**怎么用」，不是「这份代码
    怎么部署」。跟 config.yaml（元配置，跟代码走）分开后，重装 / 换机器不会把
    个人偏好带走或弄丢。可用环境变量 ``KEEPER_SETTINGS`` 指向别处（测试 / 多实例）。
    """
    env = os.getenv("KEEPER_SETTINGS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".keeper" / SETTINGS_FILENAME


def _legacy_path() -> Path:
    """旧位置（包目录下的 settings.yaml），仅用于一次性迁移。"""
    return Path(__file__).parent / SETTINGS_FILENAME


def _migrate_legacy_once() -> None:
    """把旧位置（``keeper/settings.yaml``）的设置搬到 ``~/.keeper/settings.yaml``。

    只在「新位置没有、旧位置有」时搬一次；搬成功才删旧文件，失败就原样留着——
    配置迁移宁可留下一个冗余文件，也不能把用户的设置弄丢。
    """
    try:
        new_p, old_p = settings_path(), _legacy_path()
        if new_p.exists() or not old_p.exists():
            return
        new_p.parent.mkdir(parents=True, exist_ok=True)
        content = old_p.read_text(encoding="utf-8")
        new_p.write_text(content, encoding="utf-8")
        # 确认写成功再删旧文件
        if new_p.read_text(encoding="utf-8") == content:
            old_p.unlink()
            logger.info("用户设置已迁移到 %s（旧文件已删除）", new_p)
    except Exception as e:  # noqa: BLE001
        logger.warning("迁移旧的用户设置失败（保持原状）: %s", e)


_migrated = False


def load_raw() -> Dict[str, Any]:
    """读原始用户设置。

    **文件不存在 / 损坏都返回空 dict**，调用方因此自然回退到内置默认值
    （``config.py`` 里各 Section dataclass 的字段默认值），不会因为没有配置文件
    就报错——设置页照样能打开，行为与「配了默认值」完全一致。
    """
    global _migrated
    if not _migrated:  # 首次读取时把旧位置的设置搬过来（只做一次）
        _migrated = True
        _migrate_legacy_once()
    p = settings_path()
    if not p.exists():
        logger.debug("没有用户设置文件（%s），使用内置默认配置", p)
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("读取用户设置失败（%s），按默认配置运行: %s", p, e)
        return {}


def _header_comments() -> str:
    """取出文件**开头**的注释块。

    pyyaml 回写会抹掉所有注释，而这段说明（两个文件怎么分工、字段是什么意思）
    恰恰最该留着——回写时把它原样拼回去。只取开头的连续注释/空行，遇到第一个
    配置行就停，免得把段落里的说明错位贴到别处。
    """
    p = settings_path()
    if not p.exists():
        return ""
    head: list = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s == "" or s.startswith("#"):
            head.append(line)
            continue
        break
    return "\n".join(head).strip()


def save_raw(data: Dict[str, Any]) -> None:
    """整体写回用户设置（只写这一个文件，不碰 config.yaml）。"""
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    head = _header_comments()
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    p.write_text((head + "\n\n" if head else "") + body, encoding="utf-8")


def fingerprint() -> tuple:
    """用户设置文件的指纹（mtime_ns + 大小）。

    各处的配置缓存拿它判断「文件是不是被人改过」：改过就**立刻**重读，不必等
    TTL 到期。手改 settings.yaml 也能在同一进程内迅速生效。
    """
    p = settings_path()
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def invalidate_caches() -> None:
    """保存 / 手动重载后，让各处的配置缓存立即失效。

    没有它，改完设置最多要等一个缓存周期才生效——用户会觉得「改了没反应，
    得重启一下」。这里只清同进程的缓存；其它进程靠文件指纹自行发现变化。
    """
    try:
        from ..observability import invalidate_cfg_cache

        invalidate_cfg_cache()
    except Exception as e:  # noqa: BLE001
        logger.debug("清理 prompt dump 配置缓存失败: %s", e)
    try:
        from ..a2a.settings import invalidate_cfg_cache as invalidate_a2a_cache

        invalidate_a2a_cache()
    except Exception as e:  # noqa: BLE001
        logger.debug("清理 A2A 配置缓存失败: %s", e)
    try:
        from ..agent.context_store import invalidate_cfg_cache as invalidate_ctx_cache

        invalidate_ctx_cache()
    except Exception as e:  # noqa: BLE001
        logger.debug("清理上下文配置缓存失败: %s", e)


def save_sections(
    *,
    prompt_dump: Optional[Dict[str, Any]] = None,
    a2a: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按段保存：只改传入的那几段，其余原样保留。返回保存后的完整内容。"""
    data = load_raw()
    if prompt_dump is not None:
        obs = data.get("observability")
        if not isinstance(obs, dict):
            obs = {}
            data["observability"] = obs
        obs["prompt_dump"] = prompt_dump
    if a2a is not None:
        data["a2a"] = a2a
    if context is not None:
        data["context"] = context
    # 只保留已知段，避免写进杂项后没人认识
    data = {k: v for k, v in data.items() if k in KNOWN_SECTIONS}
    save_raw(data)
    # 写完了就立即生效：清掉各处的配置缓存，别让用户等缓存过期
    invalidate_caches()
    return data


def section(name: str) -> Dict[str, Any]:
    """取某一段的原始 dict（没设过就是空 dict）。"""
    v = load_raw().get(name)
    return v if isinstance(v, dict) else {}


# ──────────────────────────────────────────────────────────────────────────
# 默认值回填：settings.yaml 里**应当有每一项的显式配置**，初始值 = 代码默认
#
# 为什么要有这一步
# ----------------
# 「没写这个键」和「写了，值等于默认」运行起来完全一样，但对**人**不一样：
# 前者意味着"这台机器上到底生效的是多少"只能去读代码才知道，而 settings.yaml
# 的存在意义就是让人在本机一眼看到、并直接改。所以每次启动都把代码里新增的键
# 补进文件，值取代码默认——文件从"只有被改过的键"变成"全量配置"，改哪一项都
# 不用先去查默认值是多少。
#
# 为什么不能用 ``save_raw``（yaml 整体回写）来做
# ---------------------------------------------
# pyyaml 回写会**抹掉所有注释**，只留 ``_header_comments`` 那段开头注释。本机的
# settings.yaml 往往是手写的、带着几十条行内说明（为什么是这个值），一回填就全
# 没了。所以这里走**文本层面的定点插入**：只把缺失的键补进对应的段，其余行一个
# 字节都不动。
# ──────────────────────────────────────────────────────────────────────────

# 文件不存在时用它当开头说明（与 save_raw 的 _header_comments 用同一份口径）
_DEFAULT_HEADER = """# 用户设置（本机的 ~/.keeper/settings.yaml）：设置页里改的东西都写这里，立即生效。
#
# 与 keeper/config.yaml 的分工：
#   config.yaml   —— 元配置（部署时定，跟代码走）：端口、鉴权、用哪个 agent、默认 LLM
#   settings.yaml —— 用户设置（用的时候改，跟人走）：A2A 超时与熔断、prompt 落盘、上下文治理
#
# 读取顺序：settings.yaml 覆盖 config.yaml 的同名段；这里没写的键沿用代码默认。
# 改完立即生效（无需重启）：设置页保存会自动刷新缓存，直接手改本文件也会在
# 下次读取时被发现；想立刻刷新可点设置页的「重新加载」。
# 本机文件，换机器各用各的（可加进 .gitignore）。
#
# 下面每一项都是**显式写出的**，初始值等于代码默认（keeper/config.py 的 dataclass
# 字段）；启动时代码里新增的键会自动补到这里，值同样取默认。
"""


def default_settings() -> Dict[str, Any]:
    """各段的**出厂默认**：唯一来源是 ``config.py`` 的 dataclass 字段。

    与运行时的默认值同源，所以「文件里写着的初始值」必然等于「不写时生效的值」——
    不会出现两处各写一遍、改了一处忘了另一处。
    """
    from dataclasses import asdict

    from ..config import A2ASection, ContextSection, PromptDumpSection

    return {
        "context": asdict(ContextSection()),
        "a2a": asdict(A2ASection()),
        "observability": {"prompt_dump": asdict(PromptDumpSection())},
    }


def _kv_lines(tree: Dict[str, Any], indent: int) -> List[str]:
    """把标量键值对渲染成 YAML 行（值交给 yaml，保证引号 / 转义正确）。"""
    out: List[str] = []
    for k, v in tree.items():
        if isinstance(v, dict):
            continue
        # 不能直接 ``safe_dump(v)``：顶层是标量时它会输出 ``auto\n...\n``
        # （带文档结束标记 ``...``），插进文件就把 YAML 结构搞坏了。
        # 套一层 dict 再剥掉键名，拿到的才是干净的标量。
        dumped = yaml.safe_dump({"_": v}, allow_unicode=True, sort_keys=False)
        scalar = dumped.split(":", 1)[1].strip()
        out.append(f"{' ' * indent}{k}: {scalar}")
    return out


def _section_lines(tree: Dict[str, Any], indent: int = 2) -> List[str]:
    """整段渲染（仅在文件里**完全没有这一段**时用）。标量在前，子块在后。"""
    out = _kv_lines(
        {k: v for k, v in tree.items() if not isinstance(v, dict)}, indent
    )
    for k, v in tree.items():
        if isinstance(v, dict):
            out += [f"{' ' * indent}{k}:"] + _kv_lines(v, indent + 2)
    return out


def _block_range(
    lines: List[str], header: str, indent: int, lo: int, hi: int
) -> Optional[Tuple[int, int]]:
    """在 [lo, hi) 里找 ``header:`` 块的 (起始行, 结束行)；找不到返回 None。

    结束的判定是「下一行缩进 <= 本块标题的缩进」，因此块内的注释 / 空行都算块内。
    """
    pat = re.compile(r"^" + " " * indent + re.escape(header) + r":\s*(#.*)?$")
    start = -1
    for i in range(lo, hi):
        if pat.match(lines[i]):
            start = i
            break
    if start < 0:
        return None
    end = hi
    for j in range(start + 1, hi):
        s = lines[j].strip()
        if not s or s.startswith("#"):
            continue
        cur = len(lines[j]) - len(lines[j].lstrip(" "))
        if cur <= indent:
            end = j
            break
    return start, end


def _existing_keys(lines: List[str], lo: int, hi: int, indent: int) -> set:
    """块内已有的键名（只看 ``indent + 2`` 这一层的 ``key:`` 行）。"""
    pat = re.compile(r"^" + " " * (indent + 2) + r"([A-Za-z_]\w*):")
    out: set = set()
    for j in range(lo, hi):
        m = pat.match(lines[j])
        if m:
            out.add(m.group(1))
    return out


def _recompute_end(lines: List[str], lo: int, indent: int) -> int:
    """块内插了行之后重新算块尾（同一个「缩进变浅即结束」的判定）。"""
    for j in range(lo + 1, len(lines)):
        s = lines[j].strip()
        if not s or s.startswith("#"):
            continue
        cur = len(lines[j]) - len(lines[j].lstrip(" "))
        if cur <= indent:
            return j
    return len(lines)


def _last_content_idx(lines: List[str], lo: int, hi: int) -> int:
    """块内最后一个**配置项**行的下标 +1（新行插在这里）。

    只认「非空且不是注释」的行：段尾那几行注释是**下一段的说明**（比如写在
    ``a2a:`` 上方的「协作：…」），把键插到它们后面虽然 YAML 上没错，但读起来
    会被误认为属于下一段。
    """
    idx = lo + 1
    for j in range(lo + 1, hi):
        s = lines[j].strip()
        if s and not s.startswith("#"):
            idx = j + 1
    return idx


def _ensure_block(
    lines: List[str],
    rng: Tuple[int, int],
    indent: int,
    tree: Dict[str, Any],
    added: List[str],
) -> None:
    """把 tree 里缺失的键补进块（就地改 lines）；嵌套 dict 递归进子块。"""
    lo, hi = rng
    existing = _existing_keys(lines, lo, hi, indent)
    # ① 嵌套的子块（如 observability.prompt_dump）
    for key, val in tree.items():
        if not isinstance(val, dict):
            continue
        sub = _block_range(lines, key, indent + 2, lo, hi)
        if sub is None:  # 整块都没有：连同子键一起插进去
            ins = _last_content_idx(lines, lo, hi)
            lines[ins:ins] = [
                f"{' ' * (indent + 2)}{key}:"
            ] + _kv_lines(val, indent + 4)
            added.extend(f"{key}.{k}" for k in val)
        else:
            _ensure_block(lines, sub, indent + 2, val, added)
        # 插完子块后块范围变长了，重新算一次，别让下一轮的插入位置失效
        hi = _recompute_end(lines, lo, indent)

    # ② 本层的标量键：整批插入，保持 dataclass 里的字段顺序
    missing = [k for k, v in tree.items() if not isinstance(v, dict) and k not in existing]
    if missing:
        ins = _last_content_idx(lines, lo, hi)
        lines[ins:ins] = _kv_lines({k: tree[k] for k in missing}, indent + 2)
        added.extend(missing)


def ensure_defaults() -> bool:
    """把「代码里有、文件里没写」的键补进 settings.yaml；返回是否真的写了盘。

    幂等：全都有就不写（连 mtime 都不动）。启动调一次即可，之后用户在设置页
    改哪个键都是改那个键，不会被下一次启动覆盖。
    """
    p = settings_path()
    defaults = default_settings()
    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                _DEFAULT_HEADER
                + "\n"
                + yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            logger.info("已按默认值生成用户设置: %s", p)
            return True

        lines = p.read_text(encoding="utf-8").splitlines()
        added: List[str] = []
        for section_name, tree in defaults.items():
            rng = _block_range(lines, section_name, 0, 0, len(lines))
            if rng is None:
                # 整段都没有：追加到文件末尾
                lines += ["", f"{section_name}:"] + _section_lines(tree)
                added.append(f"{section_name}（整段）")
                continue
            _ensure_block(lines, rng, 0, tree, added)
        if not added:
            return False
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("用户设置已补入 %s 个默认值: %s", len(added), "、".join(added))
        return True
    except Exception as e:  # noqa: BLE001 回填失败绝不能影响启动
        logger.warning("回填用户设置默认值失败（保持原状）: %s", e)
        return False
