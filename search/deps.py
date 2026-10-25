"""可选依赖的加载与提示。

**为什么单独一个模块**：numpy 是**向量路径专用**的依赖，关键词检索（SQLite FTS5
+ BM25）完全不需要它。所以 numpy 在各模块里都是**延迟导入**——顶层不 import，
用到向量时才 import。

代价是：缺依赖时抛的是裸的 ``No module named 'numpy'``，用户不知道该怎么办。
这个模块把提示收在一处，统一说清「怎么装」和「不装也能干什么」。
"""
from __future__ import annotations


def require_numpy():
    """拿 numpy；没装就抛一个**能照做**的错。

    抛 ``RuntimeError`` 而不是 ``ImportError``：对使用者来说这更像「功能不可用」
    （HTTP 层会转成 503 + 人话），而不是「代码写错了」。
    """
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "向量检索需要 numpy，但当前环境没装。"
            "装它：pip install numpy；"
            "不装也能用——把检索通道选成「纯关键词」（SQLite FTS5 + BM25），"
            "那条路径不依赖 numpy。"
        ) from e
    return np
