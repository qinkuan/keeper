"""按天分文件的日志 handler 自测。

覆盖：文件名、追加、跨天换文件、过期清理只删自己格式的文件、缺 agent 字段不丢日志。

跑法：``python tests/test_log_daily.py``
"""
from __future__ import annotations

import logging
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.log.handler import DailyFileHandler  # noqa: E402

PASS = 0
FAIL = 0

FMT = "%(asctime)s %(levelname)-7s [%(agent)s] %(name)s | %(message)s"


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}")


class _FakeDate(date):
    """可以指定"今天是哪天"的日期类，用来模拟跨天。"""

    fake_today: date = date.today()

    @classmethod
    def today(cls) -> date:
        return cls.fake_today


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="keeper-log-"))

    print("1) 文件名与写入")
    h = DailyFileHandler(tmp, retention_days=0)
    h.setFormatter(logging.Formatter(FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger("keeper.test.log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(h)
    logger.info("第一条")
    today = date.today()
    want = tmp / f"{today.isoformat()}.log"
    check(want.exists(), f"生成了 {want.name}")
    check("第一条" in want.read_text(encoding="utf-8"), "内容写进去了")
    check(today.isoformat() in want.read_text(encoding="utf-8"), "文件里带完整日期")

    print("2) 同一天内是追加")
    logger.info("第二条")
    txt = want.read_text(encoding="utf-8")
    check("第一条" in txt and "第二条" in txt, "两条都在（不是覆盖）")

    print("3) 缺 agent 字段也不丢日志")
    # 不挂 AgentFilter 时，格式串里的 %(agent)s 会 KeyError。
    # handler 自己兜底成 "-"，日志不能因此整条丢掉。
    plain = DailyFileHandler(tmp, retention_days=0)
    plain.setFormatter(logging.Formatter(FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    lg2 = logging.getLogger("keeper.test.plain")
    lg2.setLevel(logging.INFO)
    lg2.propagate = False
    lg2.addHandler(plain)
    lg2.info("没有 agent 标签")
    day_files = sorted(tmp.glob("*.log"))
    check(
        any("没有 agent 标签" in f.read_text(encoding="utf-8") for f in day_files),
        "没挂 filter 时日志仍写进去了",
    )

    print("4) 跨天换文件")
    tomorrow = today + timedelta(days=1)
    with patch("keeper.log.handler.date", _FakeDate):
        _FakeDate.fake_today = tomorrow
        logger.info("第二天第一条")
    tomorrow_file = tmp / f"{tomorrow.isoformat()}.log"
    check(tomorrow_file.exists(), f"跨天后新建了 {tomorrow_file.name}")
    check(
        "第二天第一条" in tomorrow_file.read_text(encoding="utf-8"),
        "新记录写进了新文件",
    )
    check(
        "第二天第一条" not in want.read_text(encoding="utf-8"),
        "旧文件没被追加",
    )
    _FakeDate.fake_today = today

    print("5) 过期清理只删自己格式的文件")
    keep = tmp / "kernel.log"  # 别的日志，不该被删
    keep.write_text("别人的日志", encoding="utf-8")
    desktop = tmp / "2026-10-01.desktop.log"  # 命名不匹配，不该被删
    desktop.write_text("桌面壳日志", encoding="utf-8")
    old = tmp / f"{(today - timedelta(days=30)).isoformat()}.log"
    old.write_text("很久以前", encoding="utf-8")
    recent = tmp / f"{(today - timedelta(days=3)).isoformat()}.log"
    recent.write_text("三天前", encoding="utf-8")

    h2 = DailyFileHandler(tmp, retention_days=14)
    removed = h2.cleanup_expired()
    check(not old.exists(), "30 天前的被删了")
    check(recent.exists(), "3 天前的留着")
    check(keep.exists(), "kernel.log（别人的）留着")
    check(desktop.exists(), "*.desktop.log（别人的）留着")

    print("6) retention_days=0 表示不清理")
    old2 = tmp / f"{(today - timedelta(days=99)).isoformat()}.log"
    old2.write_text("很久很久以前", encoding="utf-8")
    h3 = DailyFileHandler(tmp, retention_days=0)
    h3.cleanup_expired()
    check(old2.exists(), "retention_days=0 时不删任何文件")

    print("7) close 之后不再写")
    h.close()
    try:
        logger.info("关闭后")
    except Exception as e:  # noqa: BLE001
        check(False, f"close 后写日志抛异常：{e}")
    else:
        check(True, "close 后写日志不抛异常（handleError 兜住）")

    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
