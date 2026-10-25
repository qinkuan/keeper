"""索引同步：让索引**自动**跟上文件的变化。

背景
----
索引是「快照」，文件是活的。用户改一个 md、切到搜索页就要搜到新内容——
如果索引只在手动点「建立索引」时才更新，这个体验就是坏的。

三层机制
--------
1. **stat 预筛**（``index_roots(..., prescreen=True)``）：扫描时先看
   ``mtime + size``，没变过的文件**连打开都不打开**。3 万文件 / 2.8GB 的语料，
   全量 hash 要 6～7 秒（读 2.8GB 磁盘），只 stat 只要 200ms。
2. **定时增量扫描**（本模块）：每 ``config.sync_interval()`` 秒扫一次，
   把外部编辑器 / 别的 App 改掉的文件补进索引。分钟级延迟，成本 0.6% 单核。
3. **定期全量校准**：stat 会漏报（``cp -p`` / ``rsync --times`` / 备份恢复会保留
   旧 mtime，内容其实变了）。所以每隔 ``config.sync_full_interval()`` 秒做一次
   **不信任 stat** 的全量 hash 校准。

桌面端还有第 4 层：Electron 主进程监听「我的空间」，文件一变就调
``POST /search/index``（带 paths）——那一层是**秒级**的，本模块是兜底。

配置
----
间隔与开关都能在「搜索 → 索引管理 → 索引配置」里改，落到
``~/.keeper/settings.yaml`` 的 ``search`` 段，**改完立即生效**：
每轮循环都重新读一次配置，改间隔不需要重启；改开关会立刻停 / 起扫描。

并发
----
同一时刻只允许一个索引任务在跑（``_index_lock``）：

- 定时扫描、手动「建立索引」、文件监听回调三者共用这把锁；
- 拿不到锁就**跳过这一轮**，不排队——下一轮定时扫描会补上。
  （排队没意义：索引结果对"最新状态"才有用，迟到的旧任务只会浪费 CPU。）

预热的关系
----------
扫描要调 embedder 算向量，而模型加载要 1～7 秒。所以扫描用的是
``ingest/embed.py::get_embedder`` 那个**共享单例**——启动预热加载好的模型
就是扫描要用的那个，不会出现"预热白做"。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from . import config

logger = logging.getLogger(__name__)

#: 全局索引锁：定时扫描 / 手动建索引 / 文件监听回调共用。
_index_lock = threading.Lock()

#: 「配置变了，醒一下」信号。改间隔后不必等满一个旧周期。
_wake = threading.Event()
#: 「该停了」信号（用户在界面上关掉了自动同步）。
_stop = threading.Event()
#: 后台扫描线程（用于判断要不要重新拉起）。
_thread: Optional[threading.Thread] = None

#: 同步状态（给 /search/status 用）。
_state: Dict[str, Any] = {
    "enabled": False,
    "interval": config.SYNC_INTERVAL,
    "scans": 0,          # 已完成的扫描轮数
    "last_scan_at": 0.0, # 最近一次扫描结束的时刻
    "last_full_at": 0.0, # 最近一次全量校准的时刻
    "last_stats": None,  # 最近一次的统计
    "last_error": "",    # 最近一次失败原因
    "watched_paths": 0,  # 文件监听通知过来的路径数（累计）
}


def index_running() -> bool:
    """当前是否有索引任务在跑。"""
    return _index_lock.locked()


def try_run_index(fn, *args, **kwargs):
    """执行一个索引任务；已有任务在跑就**跳过**（返回 ``None``）。

    返回值：任务的返回值；被跳过时是 ``None``。

    为什么是「跳过」而不是「排队」：索引关心的是文件的**最新状态**，
    一个迟到的旧任务没有价值；定时扫描 30 秒后还会再来一次。
    """
    if not _index_lock.acquire(blocking=False):
        logger.debug("已有索引任务在跑，本次跳过")
        return None
    try:
        return fn(*args, **kwargs)
    finally:
        _index_lock.release()


def scan_once(roots=None, force=False, doc_type=None, full=False):
    """扫描一次「我的空间」，更新索引。

    ``full=False``：靠 stat 预筛，只读变化了的文件（日常用这个）。
    ``full=True``：不信任 stat，把所有文件重新读一遍算 hash（定期校准用）。

    返回统计字典；已有任务在跑时返回 ``None``。
    """
    from .service import SearchService

    def _work():
        svc = SearchService()
        try:
            return svc.index(roots=roots, force=force, doc_type=doc_type,
                             prescreen=not full)
        finally:
            svc.close()

    return try_run_index(_work)


def _record(stats, full: bool) -> None:
    now = time.time()
    _state["scans"] += 1
    _state["last_scan_at"] = now
    _state["last_stats"] = stats
    _state["last_error"] = ""
    if full:
        _state["last_full_at"] = now


def _scan_loop(delay: float, roots=None, doc_type=None) -> None:
    """后台扫描线程体。

    每轮都**重新读一次配置**（间隔可能刚被用户改过），所以调间隔立即生效。
    每隔 ``sync_full_interval`` 秒做一次全量校准。
    任何一次出错都只是记日志，不许把线程搞死（否则索引从此再也不自动更新）。
    """
    if delay > 0:
        time.sleep(delay)

    last_full = time.time()
    while not _stop.is_set():
        try:
            interval = config.sync_interval()
            full_interval = config.sync_full_interval()
            now = time.time()
            full = (now - last_full) >= full_interval
            stats = scan_once(roots=roots, doc_type=doc_type, full=full)
            if stats is not None:
                _record(stats, full)
                if full:
                    last_full = now
                if stats.get("indexed") or stats.get("deleted") or stats.get("moved"):
                    logger.info("索引已同步：%s", stats)
        except Exception as e:  # noqa: BLE001 扫描失败不许拖死线程
            logger.warning("索引自动同步失败（不影响使用）：%s", e)
            _state["last_error"] = str(e)
        # 等下一轮。配置被改时会立刻被唤醒（_wake），不用等满一个旧周期。
        _wake.wait(max(1.0, interval))
        _wake.clear()


def start_background(interval: Optional[float] = None,
                     full_interval: Optional[float] = None,
                     delay: float = 3.0,
                     roots=None,
                     doc_type=None) -> bool:
    """启动后台定时扫描。返回**是否真的启动了**。

    延迟几秒再开始：启动瞬间 CPU 正在建表、装载 agent、预热模型，
    没必要跟它们抢。

    ``interval`` / ``full_interval`` 一般不传——生效值每轮从配置里读
    （用户在界面上改了立即生效）。传了则是「强制指定」，只用于测试。
    """
    global _thread

    if not config.sync_enabled():
        logger.info("索引自动同步已关闭")
        return False
    if _thread is not None and _thread.is_alive():
        return False

    if interval is not None:
        config.SYNC_INTERVAL = float(interval)
    if full_interval is not None:
        config.SYNC_FULL_INTERVAL = float(full_interval)

    _stop.clear()
    _state["enabled"] = True
    _state["interval"] = config.sync_interval()
    _thread = threading.Thread(
        target=_scan_loop,
        args=(delay, roots, doc_type),
        name="search-sync",
        daemon=True,
    )
    _thread.start()
    logger.info(
        "索引自动同步已启动：每 %.0f 秒增量扫描一次，每 %.0f 秒全量校准一次",
        config.sync_interval(), config.sync_full_interval(),
    )
    return True


def apply_config() -> None:
    """按当前配置调整后台扫描：改了间隔立刻生效，改了开关立刻起 / 停。

    用户在「索引配置」里保存后调它——不用重启内核。
    """
    if config.sync_enabled():
        _stop.clear()
        _state["enabled"] = True
        _state["interval"] = config.sync_interval()
        start_background()
        _wake.set()          # 立刻按新间隔跑下一轮
        logger.info("索引自动同步已按新配置生效：每 %.0f 秒扫描一次",
                    config.sync_interval())
    else:
        _stop.set()
        _state["enabled"] = False
        logger.info("索引自动同步已按新配置关闭")


def stop_background() -> None:
    """停掉后台扫描（关窗口 / 退出时用）。"""
    _stop.set()
    _wake.set()


def status() -> Dict[str, Any]:
    """同步状态（给 /search/status 用）。"""
    out = dict(_state)
    out["running"] = index_running()
    out["interval"] = config.sync_interval()
    out["full_interval"] = config.sync_full_interval()
    out["enabled"] = config.sync_enabled()
    return out


def note_watched(count: int) -> None:
    """记一笔：文件监听通知了多少个路径进来。"""
    _state["watched_paths"] += int(count)
