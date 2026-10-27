"""按天分文件的日志 handler：``<目录>/<YYYY-MM-DD>.log``。

只管"把记录写到哪个文件"，不管"要不要写""写多细"——那些在
:mod:`keeper.log.setup`。

为什么不直接用 :class:`logging.handlers.TimedRotatingFileHandler`：它生成的是
``app.log.2026-10-27`` 这种「同一个文件轮转出多个副本」，而我们要的是**每天
一个独立文件**（``2026-10-27.log``）——按天翻页时一眼看出是哪天，不用在
文件名里再抠日期。

换文件的判定放在 :meth:`DailyFileHandler.emit` 里而不是定时器：日志是
「事件驱动」的，跨天的那一刻通常正是有日志写入的那一刻；没有日志就不必
换文件（也就不会生成空文件）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

#: 日志目录默认值。
DEFAULT_LOG_DIR = Path.home() / ".keeper" / "logs"

#: 保留最近多少天。14 天足够覆盖"上周那次"这类回溯，又不会在磁盘上堆太多。
DEFAULT_RETENTION_DAYS = 14

#: 只删**自己这个格式**的文件（``2026-10-27.log``）——目录里可能有别的日志
#: （比如旧版留下的 ``kernel.log``、桌面壳的 ``*.desktop.log``），不该被误删。
_DAILY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.log$")


class DailyFileHandler(logging.Handler):
    """把日志写到 ``<directory>/<YYYY-MM-DD>.log``，跨天自动换文件。

    参数
    ----
    directory:
        日志目录（不存在会创建）。
    retention_days:
        保留最近多少天；``0`` = 不清理。清理只删**符合 ``YYYY-MM-DD.log``
        命名**的文件，不碰目录里的其它东西。
    """

    def __init__(
        self,
        directory: Path,
        *,
        encoding: str = "utf-8",
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        super().__init__()
        self.directory = Path(directory).expanduser()
        self.encoding = encoding
        self.retention_days = retention_days
        self._day: Optional[date] = None
        self._stream = None
        self._path: Optional[Path] = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 建不了目录就让它在 emit 时报错（写不了日志不该拦住服务启动）
            pass

    # ---- 文件路径 ----

    def path_for(self, day: date) -> Path:
        """某一天对应的日志文件路径。"""
        return self.directory / f"{day.isoformat()}.log"

    @property
    def current_path(self) -> Optional[Path]:
        """当前正在写的文件（还没写过任何日志时是 None）。"""
        return self._path

    # ---- 打开 / 关闭 ----

    def _ensure_stream(self) -> None:
        """确保流开着，且开的是**今天**那个文件。"""
        today = date.today()
        if self._day == today and self._stream is not None:
            return
        self._close_stream()
        path = self.path_for(today)
        self._stream = open(path, "a", encoding=self.encoding, errors="replace")
        self._day = today
        self._path = path

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None

    # ---- logging.Handler 接口 ----

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # 格式串里有 %(agent)s，而它由 :class:`~keeper.log.setup.AgentFilter`
            # 从 ContextVar 注入。**这里再兜一层**：万一有人直接用本 handler
            # 而没挂 filter，格式化会抛 KeyError，整条日志就丢了——日志系统
            # 自己出错时静默丢日志，是最难查的那种故障。
            if not hasattr(record, "agent"):
                record.agent = "-"
            self._ensure_stream()
            self._stream.write(self.format(record) + "\n")
            # 每条都 flush：进程被杀（Ctrl+C / OOM）时最后几行不能丢。
            # 性能上可接受——这是本机单用户应用的日志量，不是高并发服务。
            self._stream.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close(self) -> None:
        self._close_stream()
        super().close()

    # ---- 过期清理 ----

    def cleanup_expired(self, now: Optional[date] = None) -> List[Path]:
        """删掉超出保留期的日志文件，返回删掉的文件列表。

        只删 ``YYYY-MM-DD.log`` 这种命名——目录里可能有别的日志（旧版留下的
        ``kernel.log``、桌面壳的 ``*.desktop.log``），不该被误删。
        """
        if self.retention_days <= 0:
            return []
        today = now or date.today()
        cutoff = today - timedelta(days=self.retention_days)
        removed: List[Path] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return []
        for p in entries:
            if not p.is_file() or not _DAILY_NAME.match(p.name):
                continue
            try:
                day = date.fromisoformat(p.stem)
            except ValueError:
                continue
            if day < cutoff:
                try:
                    p.unlink()
                    removed.append(p)
                except OSError:
                    pass
        return removed
