"""keeper 的日志：控制台 + **按天分文件**的落盘。

- :mod:`keeper.log.handler` —— ``DailyFileHandler``：写到
  ``~/.keeper/logs/<YYYY-MM-DD>.log``，跨天自动换文件，保留最近 N 天；
- :mod:`keeper.log.setup` —— ``setup_file_logging``：装配到 root logger，
  以及共用的 ``AgentFilter``（给每行补 ``[agent]`` 标签）。

用法（内核启动时，见 ``keeper/main.py``）::

    import logging
    from keeper.log import setup_file_logging

    logging.basicConfig(level=logging.INFO, format=fmt)
    setup_file_logging(logging.INFO, fmt)

控制台照旧打，文件是**额外**的一份。写不了文件不拦启动。

第三方库的已知噪音（pypdf 读不规范 PDF 时的 ``Ignoring wrong pointing
object``）由 :mod:`keeper.log.noise` 统一降噪。
"""
from .handler import (
    DEFAULT_LOG_DIR,
    DEFAULT_RETENTION_DAYS,
    DailyFileHandler,
)
from .noise import NOISY_LOGGERS, silence_noisy_loggers
from .setup import AgentFilter, setup_file_logging

__all__ = [
    "AgentFilter",
    "DailyFileHandler",
    "DEFAULT_LOG_DIR",
    "DEFAULT_RETENTION_DAYS",
    "NOISY_LOGGERS",
    "setup_file_logging",
    "silence_noisy_loggers",
]
