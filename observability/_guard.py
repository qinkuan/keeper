"""可观测层的兜底策略：**吞运行时故障，但不吞编程错误**。

为什么要有这个文件：本包要遵守「任何统计失败都不能影响主流程」（见
``observability/__init__.py``），所以到处都是 ``except Exception``。但这个原则
被一刀切地套用后，会把**代码本身写错了**一起吞掉，且一律只记 debug——于是
「用量详情和 prompt dump 查不到」这件事，在接口返回值上看起来像是「这轮没数据」，
和真正的 bug 完全无法区分。

真实案例：``observability/_context.py`` 的 ``_resolve_round_anchor`` 从
``timeline.py`` 搬过来时漏了 ``from sqlalchemy import select``，函数内
``except Exception`` 把 ``NameError`` 吃掉后 ``return message_id``——而
``message_timeline`` / ``read_message_dump`` / ``duplicate_calls`` 三处都依赖它，
结果「查看详情」和「查看 dump」同时失灵，没有任何报错，只是数据为空。

于是把异常分成两类，分别对待：

=========================  ====================================================
异常类型                   处置
=========================  ====================================================
运行时故障                 照旧吞掉 + debug 日志。这是兜底的本意
                           （DB 锁、IO 抖动、超时）
编程错误                   **不吞**。查询路径直接抛，写入路径至少记 ERROR
                           （NameError / ImportError / AttributeError /
                           TypeError / SyntaxError）
=========================  ====================================================

两个入口按**路径性质**选：

- :func:`raise_if_bug` —— **查询路径**用。查询失败本来就该让人看见（接口报错
  比返回空数据诚实得多），编程错误直接抛。
- :func:`note_if_bug` —— **写入路径**用。记账/落盘失败绝不能把对话搞挂，所以
  不抛；但编程错误意味着「数据在静默地丢」，必须留 ERROR 级痕迹。
- :func:`warn_bug_only` —— 纯容错场景（失败是预期内的解析类操作）。

单独抽成模块而不是塞进 ``_context.py``：``pricing`` / ``stats`` / ``budget`` 都要用，
放 ``_context`` 会凭空造出一条依赖边。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 「代码本身写错了」的异常类型。它们不是运行时故障，兜底没有意义——
# 重试一次结果一样，只会一直静默失败下去。
_BUG_ERRORS = (
    NameError,        # 用了没导入的名字（最常见：搬文件漏了 import）
    ImportError,      # 模块 / 符号不存在
    AttributeError,   # 拼错属性名、上游改了接口
    TypeError,        # 签名改了、传参类型不对
    SyntaxError,      # 代码本身就解析不过（动态执行时才会碰到）
    IndentationError,
)


def is_bug(e: BaseException) -> bool:
    """这个异常是「代码写错了」还是「运行时故障」。"""
    return isinstance(e, _BUG_ERRORS)


def raise_if_bug(e: BaseException, where: str) -> None:
    """查询路径的兜底：编程错误照常抛出，别让它伪装成「查不到数据」。

    在 ``except`` 块里调用；正常返回说明是运行时故障，调用方照旧
    ``logger.debug`` 吞掉即可。

    :param e: 捕获到的异常
    :param where: 出错位置描述，会进日志（如「按轮聚合 step 用量」）
    """
    if not is_bug(e):
        return
    # 打 error 再抛是**故意**重复一次：外层（FastAPI）也会打，但那条只是
    # 「请求处理失败」；这条带原始调用栈，更贴近真正的出错点。
    logger.error("%s 遇到编程错误，不再兜底: %r", where, e, exc_info=True)
    raise e


def note_if_bug(e: BaseException, where: str) -> None:
    """写入路径的兜底：绝不抛（记账挂了不能把对话搞挂），但编程错误要留痕。

    与 :func:`raise_if_bug` 的区别只在于**不抛**——适用记账、落盘、清理这类
    「失败就等于丢数据、但不能阻断主流程」的地方。丢数据本身已是既定风险，
    再抛异常只是把风险扩大到对话；这里能做的最有价值的事，是让
    「数据在静默地丢」这件事在日志里可见。
    """
    if not is_bug(e):
        return
    logger.error(
        "%s 遇到编程错误（已吞掉，这条数据/这次记账会丢）: %r", where, e, exc_info=True
    )


def warn_bug_only(e: BaseException, where: str) -> None:
    """纯容错场景用：吞掉是合理的，但编程错误仍升到 warning 留一条痕迹。

    用于「本来就允许失败」的解析类逻辑（如按用户输入的时间桶字符串
    ``strptime``、清理旧文件时单个文件读不了）——那里的失败是**预期内**的，
    不该抛、也不该刷 error，但代码写错仍应可见。
    """
    if not is_bug(e):
        return
    logger.warning("%s 遇到编程错误（预期外）: %r", where, e, exc_info=True)
