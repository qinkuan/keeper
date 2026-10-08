"""JSON-RPC 2.0 解析 / 分发 / 错误码（A2A 承载层）。

不绑定任何业务逻辑：``handle`` 接收一个已解析的请求 dict 和一个 ``dispatch(method, params)``
协程，由调用方注入具体方法映射（见 ``__init__.py``）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from .models import JSONRPCRequest, JSONRPCResponse, JSONRPCError

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A2A 业务错误码（自定义区间，避免与标准码冲突）
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002


def _error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    return JSONRPCResponse(
        id=req_id, error=JSONRPCError(code=code, message=message, data=data)
    ).model_dump(exclude_none=True)


async def handle(
    raw: Dict[str, Any],
    dispatch: Callable[[str, Any], Awaitable[Any]],
) -> Dict[str, Any]:
    """处理单个 JSON-RPC 请求对象，返回响应对象（dict）。

    ``dispatch`` 约定：
      - 返回业务结果（dict / pydantic model dump）
      - 抛 ``NotImplementedError`` -> METHOD_NOT_FOUND
      - 抛 ``KeyError`` / ``ValueError`` / ``TypeError`` -> INVALID_PARAMS
      - 其它异常 -> INTERNAL_ERROR
    """
    try:
        req = JSONRPCRequest(**raw)
    except Exception as e:  # noqa: BLE001
        return _error(None, INVALID_REQUEST, f"invalid request: {e}")

    try:
        result = await dispatch(req.method, req.params)
    except NotImplementedError:
        return _error(req.id, METHOD_NOT_FOUND, f"method not found: {req.method}")
    except (KeyError, ValueError, TypeError) as e:
        return _error(req.id, INVALID_PARAMS, str(e))
    except Exception as e:  # noqa: BLE001
        return _error(req.id, INTERNAL_ERROR, str(e))

    return JSONRPCResponse(id=req.id, result=result).model_dump(exclude_none=True)
