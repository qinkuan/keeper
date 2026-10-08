"""可观测：LLM 调用的用量记录、归属回填与聚合。

设计见 ``doc/observability-design.md``。三个要点：

1. **落库粒度是「一次 LLM 调用一行」**（``llm_calls`` 表）。step / 消息 / 任务 /
   agent 各级用量全部由本表**聚合**得出，不在各表冗余 token 列，避免多份数据
   不一致。之所以不靠「累计值做差」：``LLM`` 是 agent 级单例，``usage`` 跨会话
   累加，并发下会把别的会话的消耗算进来，且无法归属到具体 step / 消息。
2. **归属从 ``chat/context.py`` 的 ContextVar 读**（请求级隔离，并发安全），
   不用参数层层透传——调用链很深（process → llm），透参会污染一堆签名。
3. **任何统计失败都不能影响主流程**，故所有写库一律包 try/except 只记 debug 日志。

原来是 1856 行的单体 ``keeper/observability.py``，按职责拆成下面几个模块。
**对外接口全部从这里导出**，调用方继续 ``from ..observability import X`` 不变：

===========  ====================================================
模块          职责
===========  ====================================================
``recording``  记账：LLM 调用 / 工具调用 / 能力加载 / ReAct 解析
``timeline``   时间线：消息的 step 明细、每步耗时、单消息完整时间线
``stats``      统计：用量成本多维聚合、耗时分位、错误归类、提问率等
``pricing``    定价：按 (profile, model, provider) 匹配价格，支持时段折扣
``budget``     额度与聚合：预算上限检查、按维度聚合用量与成本
``dump``       完整 prompt 落盘（调试用，需在设置里开启 prompt_dump）
===========  ====================================================

依赖方向是单向的：``recording`` / ``timeline`` → ``_context``；``stats`` →
``pricing`` / ``_context``；``budget`` → ``pricing``；``dump`` → ``_context`` +
配置/存储。反向没有引用，所以拆开不会产生循环导入。
"""

from ._context import _ctx, _resolve_round_anchor
from .budget import aggregate, budget_limits, check_budget
from .dump import dump_llm_call, invalidate_cfg_cache, read_message_dump, maybe_cleanup
from .pricing import _context_limit_for, _cost_of, _price_for
from .recording import (
    backfill_step_id,
    record_capability_load,
    record_llm_call,
    record_react_parse_stat,
    record_tool_call,
)
from .stats import (
    _blank_usage,
    _bucket_start,
    ask_rate,
    capability_stats,
    duplicate_calls,
    error_breakdown,
    react_parse_stats,
    slowest_calls,
    tool_stats,
    usage_by_messages,
    usage_timeseries,
)
from .timeline import (
    _clip,
    message_timeline,
    set_message_duration,
    step_metrics_for_message,
)

__all__ = [
    # 记账
    "record_llm_call",
    "record_tool_call",
    "record_capability_load",
    "record_react_parse_stat",
    "backfill_step_id",
    # 时间线
    "message_timeline",
    "set_message_duration",
    "step_metrics_for_message",
    # 统计
    "aggregate",
    "usage_by_messages",
    "usage_timeseries",
    "slowest_calls",
    "error_breakdown",
    "ask_rate",
    "duplicate_calls",
    "tool_stats",
    "react_parse_stats",
    "capability_stats",
    # 额度
    "check_budget",
    "budget_limits",
    # prompt 落盘（调试）
    "dump_llm_call",
    "read_message_dump",
    "invalidate_cfg_cache",
    "maybe_cleanup",
    # 跨模块共享的内部工具（dump / stats 等模块直接引用，故一并导出）
    "_ctx",
    "_resolve_round_anchor",
]
