"""pypdf 噪音降噪自测。

验证：内核侧（``keeper.log.noise``）与插件侧（``plugins/pdf/bin/pdf.py``）都能把
"``Ignoring wrong pointing object``" 这类噪音压掉，而真正的 ERROR 不受影响。

跑法：``python tests/test_log_noise.py``
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # python/：import keeper

PASS = 0
FAIL = 0

# 仓库根（keeper/）：插件脚本在 keeper/plugins/ 下。
# 注意 sys.path 插的是**上一级**（python/）——`import keeper` 才找得到。
ROOT = Path(__file__).resolve().parents[1]


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_kernel_side() -> None:
    print("1) 内核侧：keeper.log.noise")
    from keeper.log.noise import silence_noisy_loggers

    cap = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.WARNING)
    root.addHandler(cap)
    try:
        silence_noisy_loggers()
        # 模拟 pypdf 读不规范 PDF 时记的警告
        logging.getLogger("pypdf._reader").warning(
            "Ignoring wrong pointing object 183 0 (offset 0)"
        )
        noise = [r for r in cap.records if "Ignoring wrong pointing" in r.getMessage()]
        check(not noise, "噪音警告被压掉了（pypdf._reader 子 logger 也生效）")

        # 真正的错误不能被压掉
        logging.getLogger("pypdf._reader").error("真的出错了")
        errs = [r for r in cap.records if r.levelno >= logging.ERROR]
        check(len(errs) == 1, "ERROR 仍然记录（没被一并压掉）")
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)


def test_plugin_side() -> None:
    print("2) 插件侧：plugins/pdf/bin/pdf.py")
    plugin = ROOT / "plugins" / "pdf" / "bin" / "pdf.py"
    src = plugin.read_text(encoding="utf-8")
    check(
        'logging.getLogger("pypdf").setLevel(logging.ERROR)' in src,
        "插件脚本里有降噪设置",
    )

    # 真跑一次：把插件脚本当模块加载，再让 pypdf 的子 logger 记一条那种警告，
    # 确认它被插件自己的降噪设置压掉了。
    tmp = Path(tempfile.mkdtemp(prefix="keeper-noise-"))
    probe = tmp / "probe.py"
    lines = [
        "import importlib.util, logging, sys",
        "from importlib.machinery import SourceFileLoader",
        "logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s | %(message)s')",
        # 插件脚本是 .py（Windows 上内核靠后缀识别"这是脚本，要用解释器跑"），
        # 但用 SourceFileLoader 显式加载更稳（不依赖 sys.path 与工作目录）。
        f"loader = SourceFileLoader('pdfplug', {str(plugin)!r})",
        "spec = importlib.util.spec_from_loader('pdfplug', loader)",
        "m = importlib.util.module_from_spec(spec)",
        "spec.loader.exec_module(m)",
        "assert logging.getLogger('pypdf').level == logging.ERROR, '插件里的降噪设置没生效'",
        "from pypdf import PdfReader",
        "logging.getLogger('pypdf._reader').warning('Ignoring wrong pointing object 1 0 (offset 0)')",
        "logging.getLogger('pypdf._reader').error('boom')",
    ]
    probe.write_text("\n".join(lines) + "\n", encoding="utf-8")
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        print("  （跳过实跑：没有 .venv）")
        return
    out = subprocess.run(
        [str(py), str(probe)], capture_output=True, text=True, timeout=60
    )
    combined = out.stdout + out.stderr
    check("Ignoring wrong pointing object" not in combined, "实跑：噪音没出现")
    check("boom" in combined, "实跑：ERROR 还在")

    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()


def main() -> int:
    test_kernel_side()
    test_plugin_side()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
