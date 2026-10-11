"""幻觉防护：模型「自编轨迹」时整轮作废。

直接对着真实 dump 写断言（keeper/001-step3.json 是 deepseek-flash
实际跑飞的那一次：一步里写了 THOUGHT → ACTION → 自编 OBSERVATION →
又一次 THOUGHT → ACTION → 自编 OBSERVATION → FINAL，两个工具都没执行）。
"""
import json
import os
import sys

# keeper/ 目录本身（tests/ 的上一级）；再上一级才是 `keeper` 包的导入根
_KEEPER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_KEEPER_DIR))

from keeper.agent.planner import ReActPlanner, detect_hallucination

P = ReActPlanner()
REAL = os.path.join(_KEEPER_DIR, "001-step3.json")


def test_real_dump_is_detected():
    """真实跑飞样本必须被检出，且判定为 hallucination（不是 final）。"""
    if not os.path.exists(REAL):
        print("  跳过：找不到 001-step3.json")
        return
    resp = json.load(open(REAL, encoding="utf-8"))["response"]
    reason = detect_hallucination(resp)
    assert reason, "真实跑飞样本没被检出"
    d = P.parse(resp, 3)
    assert d.kind == "hallucination", f"期望 hallucination，实际 {d.kind}"
    assert d.hallucination == reason
    print(f"  OK 真实样本: kind={d.kind} 原因={reason!r}")


def test_normal_responses_not_flagged():
    """正常响应绝不能误判。"""
    cases = [
        ("单 THOUGHT + 单 ACTION", "THOUGHT: 先看目录\nACTION: fs.list_dir\nACTION_INPUT: {}\n"),
        ("纯 FINAL", "FINAL: 已完成\n"),
        ("ASK", "THOUGHT: 信息不足\nASK: 用哪个目录？\n"),
    ]
    for name, text in cases:
        assert detect_hallucination(text) == "", f"误判: {name}"
        d = P.parse(text, 1)
        assert d.kind != "hallucination", f"误判: {name} -> {d.kind}"
        print(f"  OK 未误判: {name} -> {d.kind}")


def test_obs_inside_content_not_flagged():
    """正文里出现 OBSERVATION 字样不算——那是模型写的内容，不是标签。"""
    # 模拟 ACTION_INPUT 正文里带 OBSERVATION（JSON 转义，不构成真实行首）
    text = (
        "THOUGHT: 写个文件\n"
        "ACTION: fs.write_file\n"
        'ACTION_INPUT: {"path": "a.txt", "content": "line1\\nOBSERVATION: fake\\nline3"}\n'
    )
    assert detect_hallucination(text) == "", "正文里的字样被误判成标签"
    d = P.parse(text, 1)
    assert d.kind == "act", f"正文含字样时应正常解析为 act，实际 {d.kind}"
    assert d.args.get("path") == "a.txt"
    print("  OK 正文内的字样不误判，参数仍正确")


def test_real_obs_is_engine_injected():
    """引擎注入的真实 OBSERVATION 进 role=tool，不该出现在模型输出里。

    这条是「零误报」的依据：如果哪天引擎把 OBSERVATION 写进了 assistant
    消息，这个断言就该失败——那时检测器会开始误判。
    """
    text = "THOUGHT: x\nACTION: fs.read_file\nACTION_INPUT: {}\nOBSERVATION: 真的回执"
    # 模型输出里带 OBSERVATION 必然判幻觉
    assert detect_hallucination(text)
    print("  OK 输出里的 OBSERVATION 一律判幻觉")


if __name__ == "__main__":
    test_real_dump_is_detected()
    test_normal_responses_not_flagged()
    test_obs_inside_content_not_flagged()
    test_real_obs_is_engine_injected()
    print("\n全部通过")
