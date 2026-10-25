"""启动预热：索引库里**已经有向量**时，提前把 embedding 模型加载好。

为什么需要
----------
embedding 模型 462MB，加载 + GPU kernel 编译要 1～7 秒。如果等用户第一次搜索
才加载，那第一次搜索就会卡住几秒——搜索框转圈、没反馈，用户多半以为坏了。
在应用启动时后台加载好，查询时就是无感的。

四条前提，缺一不做
------------------
1. **索引库里确实有向量** —— 没建过索引就别去加载 462MB 权重（用户可能只聊天）。
2. **模型文件在** —— 没下载或被删了都跳过（页面会提示去下载）。
3. **没装 litert-lm** —— 跳过（关键词检索本来就不需要模型）。
4. **不阻塞启动** —— 跑在 daemon 线程里，失败只记日志，绝不影响 keeper 起不来。

预热用的是**共享单例**（``ingest/embed.py::get_embedder``）：预热加载好的模型
就是查询要用的那个，不会出现「预热白做、查询照样慢」。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

#: 启动后延迟多久开始预热（秒）。给主流程（装载 agent、建表）留出 CPU。
DEFAULT_DELAY = 1.5


def _model_file_present(model_path: Optional[Path] = None) -> bool:
    path = Path(model_path) if model_path else (config.MODEL_DIR / config.MODEL_FILE)
    return path.is_file()


def _indexed_has_vectors(db_path: Path) -> bool:
    """索引库里有没有向量。

    用只读方式打开：预热不该顺手建库、也不该改动任何东西。
    """
    if not db_path.is_file():
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE vector IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def should_warmup(
    db_path: Optional[Path] = None,
    model_path: Optional[Path] = None,
) -> tuple:
    """该不该预热。返回 ``(该预热, 原因)``——原因会写进日志。"""
    from .ingest.embed import Backend

    if Backend is None:
        return False, "没装 litert-lm，没有向量通道可预热"
    path = Path(db_path) if db_path else config.DB_PATH
    if not _model_file_present(model_path):
        return False, "模型文件不在（先下载模型）"
    if not _indexed_has_vectors(path):
        return False, "索引库里还没有向量（没建过索引）"
    return True, "索引库里已有向量"


def _warmup(db_path: Optional[Path] = None) -> None:
    """真正干活的线程体。失败只记日志。"""
    try:
        from .ingest.embed import get_embedder

        em = get_embedder()
        if em is None:
            logger.info("检索预热跳过：embedder 不可用")
            return
        # 触发真正的加载（engine 属性）+ 一次预热推理
        em.embed_query("warmup")
        logger.info(
            "检索模型已预热（后端 %s），首次查询不用再等模型加载",
            em.backend_name or "?",
        )
    except Exception as e:  # noqa: BLE001 预热失败不该影响 keeper
        logger.warning("检索模型预热失败（不影响使用）：%s", e)


def warmup_in_background(
    db_path: Optional[Path] = None,
    delay: float = DEFAULT_DELAY,
) -> bool:
    """启动后台预热。返回**是否开始了预热**。

    调用方（``main.py``）不需要看返回值——预热是尽力而为的，不是必须成功的。
    """
    ok, reason = should_warmup(db_path)
    if not ok:
        logger.info("检索预热跳过：%s", reason)
        return False

    def _run() -> None:
        if delay > 0:
            # 让主流程先跑起来：预热要占 CPU（还会抢 GPU），别跟启动抢
            import time

            time.sleep(delay)
        _warmup(db_path)

    threading.Thread(target=_run, name="search-warmup", daemon=True).start()
    logger.info("将在 %.1f 秒后后台预热检索模型", delay)
    return True
