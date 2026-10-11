"""流式运行的**事件回放中心**（RunHub）。

为什么需要它
------------
SSE 是「一条 TCP 上的 HTTP 长连接」。整页刷新后浏览器把它关了，服务端那半条
连接随之消失——这在协议层面不可逆，不存在「把旧连接接回去」的办法。

但用户看到的不是连接，是**内容**。所以要解决的是另一件事：把这一轮产生的事件
在服务端内存里留一份缓冲，刷新后用**同一个 ``run_id``** 重新订阅：先把已经
发生的回放出来（页面立刻恢复到刷新那一刻该有的样子），再接上还没发生的实时流。

和「合成 in_progress 气泡 + 轮询」的关系
----------------------------------------
``load_messages`` 合成气泡 + 3s 轮询解决的是「看得见步骤」，但有两个硬伤：

1. **正文（``delta``）找不回来**。它只存在于那条已断的连接里，服务端没留副本；
   ``answer`` 又只在整轮跑完时落库一次。于是刷新回来的那几分钟，步骤在涨、
   正文区是空的，直到跑完才「啪」地一次性出现完整答案；
2. 3s 一次全量查库，长会话步骤越多越费。

RunHub 补的就是第 1 条——也是唯一肉眼可见的缺口。轮询**保留**作降级：进程
重启后 hub 是空的，那时这一轮本来也已经结束，走轮询拿到落库结果是正确行为。

缓冲上限
--------
- ``steps``：一轮 ReAct 通常几十步，超过 ``_MAX_STEPS`` 丢最早的（步骤是过程，
  丢开头几步不影响「现在在干什么」）；
- ``text``：正文按**累积字符串**存而不是逐条 delta，因此不会随 token 数爆内存。
  超过 ``_MAX_TEXT_CHARS`` 后停止累积（置 ``truncated``），但实时订阅者照收后续
  delta，且 ``done`` 帧带完整 answer——前端以 done 为准覆盖正文，所以截断对
  最终结果是**无损**的，只影响「中途刷新那一次」的回放完整度。

生命周期
--------
- 创建：``chat_stream`` 的 runner 起跑前（``run_id`` 为空则整体跳过，无从回放）
- 结束：``done`` / ``error`` 任一发生 → 标记 finished、给所有订阅者发结束哨兵
- 回收：``_reap`` 每 30s 巡检一次；finished 后留 ``_TTL_SECONDS`` 给重连，没跑完
  的最多留 ``_MAX_AGE_SECONDS``（防 runner 异常没调 finish 造成常驻泄漏）
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_MAX_STEPS = 400
_MAX_TEXT_CHARS = 120_000
# 跑完后留给「刷新回来重连」的窗口：5 分钟足够用户发现页面空了再刷新
_TTL_SECONDS = 300.0
# 一直没结束的运行最多活 2 小时（兜底，正常由 _MAX_RUN_SECONDS 收尾）
_MAX_AGE_SECONDS = 7200.0
_SWEEP_INTERVAL = 30.0


@dataclass
class RunBuffer:
    """一次流式运行的内存缓冲 + 订阅者集合。"""

    run_id: str
    agent_id: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    text: str = ""
    truncated: bool = False
    dropped_steps: int = 0
    finished: bool = False
    done_payload: Optional[Dict[str, Any]] = None
    error_message: str = ""
    subs: Set["asyncio.Queue"] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0

    def snapshot(self) -> Dict[str, Any]:
        """回放用的快照：步骤 + 累积正文 + 是否已结束。"""
        return {
            "run_id": self.run_id,
            "steps": self.steps,
            "text": self.text,
            "truncated": self.truncated,
            "dropped_steps": self.dropped_steps,
            "finished": self.finished,
            "done": self.done_payload,
            "error": self.error_message,
        }

    def fanout(self, item: Any) -> None:
        """把一帧广播给所有订阅者。

        队列无界，``put_nowait`` 不会阻塞；单个订阅者出异常不能影响其他订阅者，
        也不能回头把 runner 打断——所以这里只吞异常。
        """
        for q in list(self.subs):
            try:
                q.put_nowait(item)
            except Exception:  # 容错：广播失败绝不能打断正在跑的 agent
                logger.debug("RunHub 广播失败（已忽略）: run_id=%s", self.run_id)


# run_id -> RunBuffer
_HUB: Dict[str, RunBuffer] = {}


def ensure_run(run_id: Optional[str], agent_id: str = "") -> Optional[RunBuffer]:
    """为这一轮建缓冲（已存在则复用）。

    ``run_id`` 为空时返回 None：客户端没带 run_id 的降级路径无从回放，后续
    publish 全部自动跳过。
    """
    if not run_id:
        return None
    buf = _HUB.get(run_id)
    if buf is None:
        buf = RunBuffer(run_id=run_id, agent_id=agent_id)
        _HUB[run_id] = buf
        # 巡检任务：保证任何情况下这块缓冲最终会被回收
        try:
            asyncio.create_task(_reap(run_id))
        except RuntimeError:  # 没有运行中的事件循环（理论上不会走到）
            pass
    return buf


def publish_step(run_id: Optional[str], data: Dict[str, Any]) -> None:
    if not run_id:
        return
    buf = _HUB.get(run_id)
    if buf is None:
        return
    buf.steps.append(data)
    if len(buf.steps) > _MAX_STEPS:
        buf.steps.pop(0)
        buf.dropped_steps += 1
    buf.fanout(("step", data))


def publish_delta(run_id: Optional[str], text: str) -> None:
    if not run_id or not text:
        return
    buf = _HUB.get(run_id)
    if buf is None:
        return
    if len(buf.text) < _MAX_TEXT_CHARS:
        buf.text += text
    else:
        # 不再累积（防内存无界），但实时订阅者照收，且 done 帧会带完整正文
        buf.truncated = True
    buf.fanout(("delta", {"text": text}))


def finish(run_id: Optional[str], kind: str, payload: Any) -> None:
    """标记这一轮结束（done / error 任一），广播并给订阅者发结束哨兵。"""
    if not run_id:
        return
    buf = _HUB.get(run_id)
    if buf is None:
        return
    if buf.finished:
        return  # 幂等：done 之后再 error 不该覆盖
    buf.finished = True
    buf.finished_at = time.monotonic()
    if kind == "done":
        buf.done_payload = payload if isinstance(payload, dict) else {"data": payload}
        buf.fanout(("done", payload))
    else:
        msg = payload.get("message", "") if isinstance(payload, dict) else str(payload)
        buf.error_message = str(msg)
        buf.fanout(("error", payload))
    for q in list(buf.subs):
        try:
            q.put_nowait(None)  # 结束哨兵
        except Exception:
            pass


def snapshot(run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """回放快照；hub 里没有（进程重启 / 已回收）则 None。"""
    if not run_id:
        return None
    buf = _HUB.get(run_id)
    return buf.snapshot() if buf is not None else None


def subscribe(run_id: Optional[str]) -> Optional["asyncio.Queue"]:
    """订阅实时事件；返回订阅队列，None 表示「无可订阅」（已结束或不存在）。"""
    if not run_id:
        return None
    buf = _HUB.get(run_id)
    if buf is None or buf.finished:
        return None
    q: "asyncio.Queue" = asyncio.Queue()
    buf.subs.add(q)
    return q


def unsubscribe(run_id: Optional[str], q: "asyncio.Queue") -> None:
    if not run_id:
        return
    buf = _HUB.get(run_id)
    if buf is not None:
        buf.subs.discard(q)


async def _reap(run_id: str) -> None:
    """巡检回收：finished 后留 TTL，未结束的最多留 _MAX_AGE_SECONDS。"""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL)
        buf = _HUB.get(run_id)
        if buf is None:
            return
        anchor = buf.finished_at or buf.created_at
        limit = _TTL_SECONDS if buf.finished else _MAX_AGE_SECONDS
        if time.monotonic() - anchor > limit:
            _HUB.pop(run_id, None)
            for q in list(buf.subs):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            return
