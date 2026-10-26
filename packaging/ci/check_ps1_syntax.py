"""轻量 PowerShell 语法自检：BOM/CRLF + 引号/花括号/圆括号配对 + 行续接符位置。

不是完整解析器，但能抓住本次 CI 失败的那类问题（编码导致的引号乱入）。
"""
import pathlib
import re
import sys

# Windows 控制台默认 cp1252：print 中文会 UnicodeEncodeError，整个步骤直接失败。
# 切成 UTF-8（Actions 日志本来就是 UTF-8）；切不了就退到 errors="replace"。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGETS = [
    ROOT / "packaging" / "build_windows.ps1",
    ROOT / "packaging" / "ci" / "smoke_windows.ps1",
]

problems = 0


def check(path: pathlib.Path) -> int:
    bad = 0
    raw = path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        print(f"[FAIL] {path.name}: 缺少 UTF-8 BOM")
        bad += 1
    text = raw.decode("utf-8-sig")
    if "\r\n" not in text:
        print(f"[FAIL] {path.name}: 换行符不是 CRLF（PowerShell 5.1 的行续接符需要 CRLF）")
        bad += 1
    lines = text.split("\r\n")

    # 1) 剥掉块注释（<# ... #>）与 here-string（@"..."@ / @'...'@），保留行号
    stripped = []
    in_block = False
    in_here = False
    here_end = ""
    for line in lines:
        if in_block:
            if "#>" in line:
                line = line.split("#>", 1)[1]
                in_block = False
            else:
                stripped.append("")
                continue
        if in_here:
            if line.lstrip().startswith(here_end):
                in_here = False
            stripped.append("")
            continue
        if "<#" in line:
            head, _, tail = line.partition("<#")
            if "#>" in tail:
                line = head + tail.split("#>", 1)[1]
            else:
                line = head
                in_block = True
        m = re.search(r'@(["\'])$', line.rstrip())
        if m:
            in_here = True
            here_end = m.group(1) + "@"
            stripped.append("")
            continue
        stripped.append(line)
    if in_block:
        print(f"[FAIL] {path.name}: 块注释未闭合")
        bad += 1
    if in_here:
        print(f"[FAIL] {path.name}: here-string 未闭合")
        bad += 1

    depth_curly = depth_paren = 0
    for lineno, line in enumerate(stripped, 1):
        body = line.rstrip()
        # 行续接符必须在行尾，且反引号后不能有空白
        if body.endswith("`") and not body.endswith("``"):
            if body != line:
                print(f"[FAIL] {path.name}:{lineno}: 行续接符后有空白")
                bad += 1
            body = body[:-1]

        code = []
        idx = 0
        while idx < len(body):
            ch = body[idx]
            if ch == "#":
                break  # 行注释
            if ch == "'":
                end = body.find("'", idx + 1)
                if end == -1:
                    print(f"[FAIL] {path.name}:{lineno}: 单引号未闭合")
                    bad += 1
                    break
                idx = end + 1
                continue
            if ch == '"':
                end = idx + 1
                while end < len(body):
                    if body[end] == "`":
                        end += 2
                        continue
                    if body[end] == '"':
                        break
                    end += 1
                if end >= len(body):
                    print(f"[FAIL] {path.name}:{lineno}: 双引号未闭合")
                    bad += 1
                    break
                idx = end + 1
                continue
            code.append(ch)
            idx += 1
        code = "".join(code)
        depth_curly += code.count("{") - code.count("}")
        depth_paren += code.count("(") - code.count(")")
        if depth_curly < 0 or depth_paren < 0:
            print(f"[FAIL] {path.name}:{lineno}: 括号提前闭合 (curly={depth_curly}, paren={depth_paren})")
            bad += 1
            depth_curly = max(depth_curly, 0)
            depth_paren = max(depth_paren, 0)

    if depth_curly or depth_paren:
        print(f"[FAIL] {path.name}: 结束时未配对 curly={depth_curly} paren={depth_paren}")
        bad += 1
    if not bad:
        print(f"[OK] {path.name}: {len(lines)} 行，配对正常")
    return bad


for p in TARGETS:
    problems += check(p)

sys.exit(1 if problems else 0)
