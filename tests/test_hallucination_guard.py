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


def test_xml_protocol_shape():
    """XML 协议形态（现行协议本体）下的幻觉判定。

    协议改成 XML 标签后，幻觉的**两条判据**要跟着换形状：
    自编 ``<OBSERVATION>``、一轮多个 ``<THOUGHT>``。
    """
    # 自编回执 → 幻觉
    fake = (
        "<THOUGHT>先写文件</THOUGHT>\n"
        "<ACTION>fs.write_file</ACTION>\n"
        '<ACTION_INPUT>{"path": "a.html"}</ACTION_INPUT>\n'
        "<OBSERVATION>已写入 a.html</OBSERVATION>\n"
    )
    reason = detect_hallucination(fake)
    assert reason, "XML 形态的自编 <OBSERVATION> 没被检出"
    d = P.parse(fake, 1)
    assert d.kind == "hallucination", f"期望 hallucination，实际 {d.kind}"
    print(f"  OK XML 自编回执判幻觉: {reason!r}")

    # 一轮两个 <THOUGHT> → 幻觉（它在替引擎编排后续轮次）
    two = (
        "<THOUGHT>先写文件</THOUGHT>\n"
        "<ACTION>fs.write_file</ACTION>\n"
        '<ACTION_INPUT>{"path": "a.html"}</ACTION_INPUT>\n'
        "<THOUGHT>再交付一下</THOUGHT>\n"
        "<ACTION>fs.deliver</ACTION>\n"
        '<ACTION_INPUT>[]</ACTION_INPUT>\n'
    )
    reason = detect_hallucination(two)
    assert reason, "XML 形态的一轮多 THOUGHT 没被检出"
    assert P.parse(two, 1).kind == "hallucination"
    print(f"  OK XML 一轮多 THOUGHT 判幻觉: {reason!r}")

    # 正常 XML 输出不误判
    ok_cases = [
        (
            "单 THOUGHT + 单 ACTION",
            "<THOUGHT>先看目录</THOUGHT>\n<ACTION>bash</ACTION>\n"
            '<ACTION_INPUT>{"command": "ls"}</ACTION_INPUT>\n',
        ),
        ("纯 FINAL", "<FINAL>已完成</FINAL>\n"),
        ("ASK", "<THOUGHT>信息不足</THOUGHT>\n<ASK>用哪个目录？</ASK>\n"),
        (
            "正文里提到 OBSERVATION 字样",
            "<THOUGHT>看看回执里写了什么</THOUGHT>\n<ACTION>bash</ACTION>\n"
            '<ACTION_INPUT>{"command": "cat OBSERVATION.md"}</ACTION_INPUT>\n',
        ),
    ]
    for name, text in ok_cases:
        assert detect_hallucination(text) == "", f"误判: {name}"
        assert P.parse(text, 1).kind != "hallucination", f"误判: {name}"
        print(f"  OK XML 未误判: {name}")


if __name__ == "__main__":
    test_real_dump_is_detected()
    test_normal_responses_not_flagged()
    test_obs_inside_content_not_flagged()
    test_real_obs_is_engine_injected()
    test_xml_protocol_shape()
    print("\n全部通过")
