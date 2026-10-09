"""单条超长工具输出的降级回归：先外部化（给 block_id），截断只当兜底。

为什么要有这个测试：曾经的顺序是「先硬截断、再让滚动外部化判断」，可 messages 里
的内容已经 ≤ observation_limit(3500)，永远够不到 externalize_min_chars(5000)，
外部化这条兜底路径一次都没触发过——超长结果只有截断、没有出路，模型看不到中段
又没有 id 可 read。

跑法：`python keeper/tests/test_context_externalize.py`
（块目录被换到临时目录，不碰真实的 ~/.keeper/context-store）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# parents: [0]=tests/  [1]=keeper/  [2]=项目根（keeper 包的父目录）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.agent import context_store as cs  # noqa: E402

TMP = tempfile.mkdtemp(prefix="keeper-ctx-test-")

CFG = SimpleNamespace(
    enabled=True,
    dir=TMP,
    externalize_min_chars=5000,
    never_externalize="",
    retain_days=7,
    preview_head=400,
    preview_tail=200,
    observation_limit=3500,
)


def _block_id(text: str) -> str:
    return text.split('block_id="')[1].split('"')[0]


def case(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  —— ' + detail) if detail else ''}")
    return ok


def main() -> int:
    cs.ctx_cfg = lambda: CFG  # 配置与块目录都换成临时的
    results: list[bool] = []

    # 1) 超长 → 外部化，且 read(block_id) 能取回**逐字不差**的原文
    raw = "".join(f"第{i}行命中内容\n" for i in range(1, 400))  # ~3900 字符
    out = cs.externalize("fs.search_files", raw, min_chars=CFG.observation_limit + 1)
    results.append(case("超长输出走外部化而不是截断", out != raw and 'block_id="ctx:' in out))
    results.append(case("block_id 取回的原文与输出一致", cs.read_chunk(_block_id(out)) == raw))

    # 2) 3500~5000 区间：曾经既不外部化又被截断的死角
    mid = "x" * 4200
    out_mid = cs.externalize("fs.read_file", mid, min_chars=CFG.observation_limit + 1)
    results.append(case("3500~5000 区间不再被截断（有 block_id）", 'block_id="ctx:' in out_mid))

    # 3) 没超限：原样返回，不写盘
    results.append(case("未超限原样返回", cs.externalize("t", "短输出", min_chars=3501) == "短输出"))

    # 4) 豁免名单 → 退回硬截断，且文案给出路（否则模型只会原样重调）
    CFG.never_externalize = "secret.*"
    big = "".join(f"第{i}行命中内容\n" for i in range(1, 400))
    no_ext = cs.externalize("secret.read", big)
    clipped = cs.clip_observation(big, CFG.observation_limit)
    results.append(case("豁免工具不外部化", no_ext == big))
    results.append(case("兜底截断不超限", len(clipped) <= 3600, f"{len(clipped)} 字符"))
    results.append(
        case("截断文案写明怎么重试", "省略" in clipped and "缩小范围" in clipped)
    )
    CFG.never_externalize = ""

    # 5) 截断保留头尾（结论/报错常在结尾，硬切会坑模型）
    t = "HEAD" + "y" * 5000 + "TAIL"
    c = cs.clip_observation(t, CFG.observation_limit)
    results.append(case("截断保留头尾", c.startswith("HEAD") and c.endswith("TAIL")))

    # 6) 关掉总开关 → 外部化不生效（由截断兜底）
    CFG.enabled = False
    results.append(case("总开关关掉时不外部化", cs.externalize("t", big, min_chars=10) == big))
    CFG.enabled = True

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
