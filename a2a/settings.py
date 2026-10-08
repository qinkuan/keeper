"""A2A 出站设置：超时/熔断的**现读配置** + 熔断状态机。

配置放在 ``config.yaml`` 的 ``a2a`` 段（见 ``keeper/config.py`` 的 A2ASection），
这里只做两件事：

1. ``a2a_cfg()``：带 5 秒缓存现读配置——改完设置页最多 5 秒生效，不用重启进程；
2. ``breaker``：按对端维度记连续失败，达到阈值就短期熔断（快速失败），
   冷却结束后自动放行一次（半开），成功即恢复。

为什么熔断状态只放内存：它描述的是「**这个进程**和对端此刻的连通情况」，
进程一重启就该重新探测，落库反而会让陈旧的熔断一直生效。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 配置缓存（读 yaml 是同步 IO，每次调用都读太浪费；5 秒 TTL）。
# 同时记「文件指纹」：settings.yaml 一改（设置页保存或手改）就立即重读，
# 不等 TTL——超时这类设置改完必须马上生效，否则用户以为没保存上。
_cfg_cache: Any = None
_cfg_cache_at: float = 0.0
_cfg_fp: Any = None
_CFG_TTL = 5.0


def invalidate_cfg_cache() -> None:
    """让配置缓存立即失效（设置保存 / 手动重载后调用）。"""
    global _cfg_cache, _cfg_cache_at, _cfg_fp
    _cfg_cache = None
    _cfg_cache_at = 0.0
    _cfg_fp = None


def a2a_cfg() -> Any:
    """读 A2A 配置；读不到就返回一组保守默认值（宁可超时，也不挂死）。"""
    global _cfg_cache, _cfg_cache_at, _cfg_fp
    now = time.time()
    try:
        from ..setting import fingerprint

        fp: Any = fingerprint()
    except Exception:  # noqa: BLE001
        fp = None
    if (
        _cfg_cache is not None
        and fp is not None
        and _cfg_fp == fp
        and now - _cfg_cache_at < _CFG_TTL
    ):
        return _cfg_cache
    try:
        from ..config import load_config

        _cfg_cache = load_config().a2a
        _cfg_cache_at = now
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 A2A 配置失败，用默认值: %s", e)
        try:
            from ..config import A2ASection

            _cfg_cache = A2ASection()
            _cfg_cache_at = now
        except Exception:  # noqa: BLE001  # 连模块都拿不到：给个最小可用值
            class _Default:
                request_timeout = 15.0
                task_timeout = 120.0
                poll_interval = 3.0
                cancel_on_timeout = True
                breaker_enabled = True
                breaker_threshold = 3
                breaker_cooldown = 60.0

            _cfg_cache = _Default()
            _cfg_cache_at = now
    _cfg_fp = fp
    return _cfg_cache


@dataclass
class _PeerState:
    """单个对端的熔断状态。"""

    name: str = ""
    failures: int = 0
    opened_at: float = 0.0  # 熔断开始时刻（0 = 未熔断）
    last_error: str = ""
    last_failure_at: float = 0.0
    half_open_sent: bool = False  # 冷却结束后是否已放过一次（半开探测）


@dataclass
class Breaker:
    """极简熔断器（进程内，按对端维度）。

    状态流转：closed →（连续失败达标）open →（冷却结束）half-open → 成功 closed /
    失败重新 open。half-open 只放行一次，避免冷却刚过就被一堆并发请求打回原形。
    """

    threshold: int = 3
    cooldown: float = 60.0
    states: Dict[str, _PeerState] = field(default_factory=dict)

    def _state(self, peer_key: str, name: str = "") -> _PeerState:
        st = self.states.get(peer_key)
        if st is None:
            st = _PeerState(name=name)
            self.states[peer_key] = st
        if name and not st.name:
            st.name = name
        return st

    def check(self, peer_key: str) -> Optional[str]:
        """是否应快速失败；返回 None 表示放行，否则返回给用户的理由。"""
        st = self.states.get(peer_key)
        if st is None or st.opened_at <= 0:
            return None
        waited = time.time() - st.opened_at
        if waited < self.cooldown:
            return (
                f"对端 {st.name or peer_key} 连续失败 {st.failures} 次已熔断，"
                f"还需 {max(0, self.cooldown - waited):.0f}s 冷却"
                f"（最后一次错误：{st.last_error}）"
            )
        # 冷却结束：半开，只放行一次
        if st.half_open_sent:
            return (
                f"对端 {st.name or peer_key} 仍处于熔断恢复中（探测请求已在途），本次先跳过"
            )
        st.half_open_sent = True
        logger.info("A2A 熔断半开：放行一次探测请求 → %s", peer_key)
        return None

    def record_success(self, peer_key: str) -> None:
        st = self.states.get(peer_key)
        if st is None:
            return
        if st.opened_at or st.failures:
            logger.info("A2A 熔断恢复：%s", peer_key)
        st.failures = 0
        st.opened_at = 0.0
        st.last_error = ""
        st.half_open_sent = False

    def record_failure(self, peer_key: str, error: str, name: str = "") -> None:
        st = self._state(peer_key, name)
        st.failures += 1
        st.last_error = str(error)[:200]
        st.last_failure_at = time.time()
        # 半开探测也失败 → 立即重新熔断，并重新计时
        st.half_open_sent = False
        if self.threshold > 0 and st.failures >= self.threshold:
            st.opened_at = time.time()
            logger.warning(
                "A2A 熔断开启：%s 连续失败 %s 次，冷却 %.0fs（%s）",
                peer_key, st.failures, self.cooldown, st.last_error,
            )

    def reset(self, peer_key: Optional[str] = None) -> int:
        """手动复位（设置页「重置熔断」）；不传 key 则清空全部。"""
        if peer_key is None:
            n = len(self.states)
            self.states.clear()
            return n
        return 1 if self.states.pop(peer_key, None) is not None else 0

    def snapshot(self) -> List[Dict[str, Any]]:
        """当前熔断状态（供设置页展示）。"""
        now = time.time()
        out: List[Dict[str, Any]] = []
        for key, st in self.states.items():
            open_ = st.opened_at > 0 and (now - st.opened_at) < self.cooldown
            out.append(
                {
                    "key": key,
                    "name": st.name or key,
                    "failures": st.failures,
                    "open": open_,
                    # 熔断剩余冷却秒数（未熔断为 0）
                    "remaining": max(
                        0.0, self.cooldown - (now - st.opened_at)
                    ) if st.opened_at else 0.0,
                    "last_error": st.last_error,
                    "last_failure_at": st.last_failure_at or None,
                }
            )
        out.sort(key=lambda d: (not d["open"], d["name"]))
        return out


# 进程内共享的熔断器；阈值/冷却由 ``sync_from_config()`` 在每次调用前刷新
breaker = Breaker()


def sync_from_config() -> None:
    """把配置里的阈值/冷却同步到熔断器（配置可随时改，5 秒内生效）。"""
    cfg = a2a_cfg()
    breaker.threshold = int(getattr(cfg, "breaker_threshold", 3) or 3)
    breaker.cooldown = float(getattr(cfg, "breaker_cooldown", 60) or 60)


def guard(peer_key: str, name: str = "") -> Optional[str]:
    """调用前的熔断检查（开关关闭时恒放行）。"""
    cfg = a2a_cfg()
    if not getattr(cfg, "breaker_enabled", True):
        return None
    sync_from_config()
    return breaker.check(peer_key)


def note_success(peer_key: str) -> None:
    breaker.record_success(peer_key)


def note_failure(peer_key: str, error: str, name: str = "") -> None:
    cfg = a2a_cfg()
    if not getattr(cfg, "breaker_enabled", True):
        return
    sync_from_config()
    breaker.record_failure(peer_key, error, name)
