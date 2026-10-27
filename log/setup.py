"""日志装配：把"写哪些、写多细、写哪"这三件事定下来。

拆成两个模块的原因：

- :mod:`keeper.log.handler` 只管**写到哪个文件**（按天分文件、跨天换、过期清理）；
- 本模块管**要不要写、写多细、控制台长什么样**——这些是"策略"，会随
  环境变量变（``LOG_LEVEL`` / ``KEEPER_LOG_DIR`` / ``LOG_FILE``），不该和
  文件句柄管理混在一起。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .handler import DEFAULT_LOG_DIR, DEFAULT_RETENTION_DAYS, DailyFileHandler

__all__ = [
    "AgentFilter",
    "setup_file_logging",
    "DEFAULT_LOG_DIR",
    "DEFAULT_RETENTION_DAYS",
    "DailyFileHandler",
]


class AgentFilter(logging.Filter):
    """给每条日志补上 ``[agent]`` 标签。

    单进程装多个 agent 时，LLM 调用 / 工具 / 对端调用这些日志会混在一起，
    没有它分不清哪行属于哪个 agent。值由请求入口写进 ContextVar（见
    ``chat.context.set_agent``），这里用 filter 读出来注入；不在任何 agent
    上下文里（比如启动装载阶段）时显示 ``-``。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "agent"):
            from ..chat.context import agent_label

            record.agent = agent_label()
        return True


def setup_file_logging(
    level: int,
    fmt: str,
    *,
    directory: Optional[Path] = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    datefmt: str = "%Y-%m-%d %H:%M:%S",
) -> Optional[DailyFileHandler]:
    """给 root logger 挂上按天分文件的 handler，返回它（失败返回 None）。

    控制台输出照旧（由调用方的 ``basicConfig`` 负责）——文件是**额外**的一份，
    不是替代。

    ``directory`` 默认 ``~/.keeper/logs``；可用环境变量 ``KEEPER_LOG_DIR`` 覆盖。
    整个过程包在 try 里：写不了日志文件**不该拦住服务启动**（控制台还能看，
    只是没有留档）。
    """
    log_dir = Path(
        directory or os.environ.get("KEEPER_LOG_DIR") or DEFAULT_LOG_DIR
    ).expanduser()
    try:
        handler = DailyFileHandler(log_dir, retention_days=retention_days)
        handler.setLevel(level)
        # 文件里带**完整日期**：控制台用 %H:%M:%S 就够（同一天内看），文件要
        # 跨天翻阅，光有时分秒看不出是哪天。
        handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        handler.addFilter(AgentFilter())
        root = logging.getLogger()
        root.addHandler(handler)
        removed = handler.cleanup_expired()
        if removed:
            logging.getLogger().info(
                "已清理过期日志：%s（保留 %d 天）",
                "、".join(p.name for p in removed),
                retention_days,
            )
        return handler
    except Exception as e:  # noqa: BLE001
        logging.getLogger().warning("开启日志文件失败（继续用控制台）：%s", e)
        return None
