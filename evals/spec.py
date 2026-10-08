"""用例规格：加载、校验、归类。

一条用例 = 一个问题 + 一份断言 + 若干准备条件。刻意做得很薄——只支持
「确定性准备」，不支持预置记忆 / 预置历史：那两样要么依赖记忆抽取（模型侧
的不确定性，评测集本身就不该有），要么让用例之间互相影响（后一条污染前一条，
跑出来的 delta 就不可信了）。15 条 smoke 用例因此全是**单轮**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class Asserts:
    """一条用例的断言集合。全部**可选**，没写的项不参与判定。

    断言分两类，优先级不同：

    - **行为型**（``answer_contains`` / ``answer_not_contains``）：断言最终答案的
      内容。稳定，优先写这种。
    - **实现型**（``must_call_tools``）：断言「必须走过某条路径」。工具改名、
      插件更新就会失效，所以只在**专门测某条机制**（如能力按需加载）时才写。

    阈值类（``max_*``）不是断言「必须做到」，而是「超出就算这条挂了」——
    它们是回归护栏：防止一次改动让某条用例的步数 / token 悄悄翻三倍。
    """

    answer_contains: List[str] = field(default_factory=list)
    answer_not_contains: List[str] = field(default_factory=list)
    # 正则（re.search，忽略大小写）：需要表达「答案里提到某个**语义**」而不是
    # 某个固定措辞时用它。典型场景——文件不存在时 agent 可以说「没有找到」
    # 「不存在」「未见」，写死任何一种都会制造假红。
    answer_matches: List[str] = field(default_factory=list)
    must_call_tools: List[str] = field(default_factory=list)
    must_not_call: List[str] = field(default_factory=list)
    # 跑完工作空间里应当出现的文件（相对路径）。用于验证「真的写出来了」，
    # 而不是只看 agent 自称写好了——自述与事实不一致正是要测的东西。
    expect_files_created: List[str] = field(default_factory=list)
    # 该问用户时要问出来（信息不足却直接瞎猜 = 挂）
    expect_suspend: Optional[bool] = None
    max_steps: Optional[int] = None
    max_tokens: Optional[int] = None
    max_duration_ms: Optional[int] = None

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Asserts":
        d = d or {}
        unknown = set(d) - {
            "answer_contains",
            "answer_not_contains",
            "answer_matches",
            "must_call_tools",
            "must_not_call",
            "expect_files_created",
            "expect_suspend",
            "max_steps",
            "max_tokens",
            "max_duration_ms",
        }
        if unknown:
            raise ValueError(f"未知的断言字段：{sorted(unknown)}")
        return Asserts(
            answer_contains=list(d.get("answer_contains") or []),
            answer_not_contains=list(d.get("answer_not_contains") or []),
            answer_matches=list(d.get("answer_matches") or []),
            must_call_tools=list(d.get("must_call_tools") or []),
            must_not_call=list(d.get("must_not_call") or []),
            expect_files_created=list(d.get("expect_files_created") or []),
            expect_suspend=d.get("expect_suspend"),
            max_steps=d.get("max_steps"),
            max_tokens=d.get("max_tokens"),
            max_duration_ms=d.get("max_duration_ms"),
        )


@dataclass
class EvalCase:
    """一条可复现的评测用例。"""

    id: str
    input: str
    title: str = ""
    tags: List[str] = field(default_factory=list)
    # 跑之前往工作空间里写的文件：{相对路径: 内容}
    files: Dict[str, str] = field(default_factory=dict)
    # 该用例允许写工作空间。默认 False——评测跑挂了也不该动到真实文件。
    allow_writes: bool = False
    asserts: Asserts = field(default_factory=Asserts)
    # 覆盖默认被测 agent（默认用命令行给的 agent）
    agent: Optional[str] = None
    # 跑这一条要跳过（保留现场但不计入基线）
    skip: bool = False
    # 失败原因（skip 时必填）
    skip_reason: str = ""

    @property
    def display_name(self) -> str:
        return self.title or self.id

    @property
    def read_only(self) -> bool:
        return not self.allow_writes


def _build_files(raw: Dict[str, Any]) -> Dict[str, str]:
    """把 ``setup.files`` 展开成「相对路径 -> 文件内容」。

    支持**生成式**内容：值写成 mapping 时形如::

        access.log:
          repeat: 240          # 重复多少行
          text: "第 {i} 条记录"  # ``{i}`` 会被替换成序号（1 起）
          head: |              # 可选：放在填充之前
            首行
          tail: "末行"          # 可选：放在填充之后

    为什么需要它：测「超长文件里的内容定位」得有几千字符的输入，而把几百行
    占位文本原样写进 YAML 既难读又难 diff。生成式让用例文件保持几十行，
    同时保证输入长度可复现。
    """
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            n = int(v.get("repeat") or 0)
            tpl = str(v.get("text") or "")
            if n <= 0:
                raise ValueError(f"文件 {k} 用了生成式写法但没给 repeat")
            body = "\n".join(tpl.format(i=i) for i in range(1, n + 1))
            parts = []
            if v.get("head"):
                parts.append(str(v["head"]).rstrip("\n"))
            parts.append(body)
            if v.get("tail"):
                parts.append(str(v["tail"]))
            out[str(k)] = "\n".join(parts) + "\n"
        else:
            out[str(k)] = "" if v is None else str(v)
    return out


def load_cases(path: str | Path) -> List[EvalCase]:
    """从 YAML 加载用例列表。

    校验刻意偏严：id 重复、空 input、写了断言但一个断言都没有——这些错误在
    跑批时才会暴露，而那时已经烧掉一堆 token 了。
    """
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(raw, list):  # 容错：允许直接写列表
        raw = {"cases": raw}
    if not isinstance(raw, dict) or "cases" not in raw:
        raise ValueError(f"{p} 顶层必须是 mapping 且含 cases 列表")

    cases: List[EvalCase] = []
    seen: set = set()
    for i, item in enumerate(raw.get("cases") or []):
        if not isinstance(item, dict):
            raise ValueError(f"{p} 第 {i + 1} 条用例不是 mapping")
        cid = str(item.get("id") or "").strip()
        if not cid:
            raise ValueError(f"{p} 第 {i + 1} 条用例缺 id")
        if cid in seen:
            raise ValueError(f"{p} 用例 id 重复：{cid}")
        seen.add(cid)

        text = str(item.get("input") or "").strip()
        if not text:
            raise ValueError(f"{p} 用例 {cid} 缺 input")

        setup = item.get("setup") or {}
        a = Asserts.from_dict(item.get("asserts"))
        has_any = any(
            [
                a.answer_contains,
                a.answer_not_contains,
                a.answer_matches,
                a.must_call_tools,
                a.must_not_call,
                a.expect_files_created,
                a.expect_suspend is not None,
                a.max_steps is not None,
                a.max_tokens is not None,
                a.max_duration_ms is not None,
            ]
        )
        if not has_any:
            raise ValueError(
                f"{p} 用例 {cid} 一条断言都没写——它只能证明「没崩」，"
                f"证明不了「答对了」"
            )

        cases.append(
            EvalCase(
                id=cid,
                input=text,
                title=str(item.get("title") or ""),
                tags=[str(t) for t in (item.get("tags") or [])],
                files=_build_files(setup.get("files") or {}),
                allow_writes=bool(item.get("allow_writes", False)),
                asserts=a,
                agent=item.get("agent"),
                skip=bool(item.get("skip", False)),
                skip_reason=str(item.get("skip_reason") or ""),
            )
        )
    return cases


def load_suite(names: Optional[List[str]] = None) -> List[EvalCase]:
    """按名字加载 ``cases/`` 下的用例集；不传则加载全部。

    名字不带扩展名（``smoke`` → ``cases/smoke.yaml``）。
    """
    if names:
        paths = []
        for n in names:
            p = CASES_DIR / n
            paths.append(p if p.suffix else p.with_suffix(".yaml"))
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"用例集不存在：{missing}")
    else:
        paths = sorted(CASES_DIR.glob("*.yaml"))
        if not paths:
            raise FileNotFoundError(f"{CASES_DIR} 下没有任何用例集")
    out: List[EvalCase] = []
    for p in paths:
        out.extend(load_cases(p))
    return out