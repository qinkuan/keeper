"""第三方库的已知噪音：降噪，但不动真正有用的信息。

有些第三方库把"我正在修复一个不规范的输入"记成 WARNING。对**调用方**来说
那不是错误——文件能正常读、结果也对——但几十条刷屏会把真正的警告埋掉。

处理原则：**按 logger 降级，不逐条匹配消息**。原因在下面（filter 不继承）。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

#: 需要降噪的第三方 logger → 降到什么级别。
#:
#: **pypdf**：读"不规范"的 PDF 时会刷一串::
#:
#:     WARNING pypdf._reader | Ignoring wrong pointing object 183 0 (offset 0)
#:
#: 那是它在**修复**交叉引用表（对象的偏移是 0 或指向越界），修完照常能读——
#: 实测那几份 PDF 33/35 页都能正常打开抽文字。所以它是噪音不是错误：文件本身
#: 没坏到读不了，只是生成器写得不够规范（Word/WPS 导出、网盘转存都常见）。
#:
#: 压到 ERROR 而不是逐条过滤消息：pypdf 的 logger 是子 logger
#: （``pypdf._reader``），而 **filter 不沿父 logger 继承**——只有 level 继承。
#: 想压住子 logger 的噪音，只能用级别。pypdf 自己没设过级别（已核），所以设
#: 父 logger 就够；真出问题时它记的是 ERROR / CRITICAL，不受影响。
NOISY_LOGGERS: Dict[str, int] = {
    "pypdf": logging.ERROR,
}


def silence_noisy_loggers(
    names: Optional[Dict[str, int]] = None,
) -> List[str]:
    """把已知噪音的第三方 logger 降到指定级别，返回实际改动的名字。

    幂等：重复调用不会叠加副作用。设级别对**尚未创建**的子 logger 也生效
    （level 继承是创建时按父级算的），所以不需要等这些库被 import。
    """
    table = NOISY_LOGGERS if names is None else names
    changed: List[str] = []
    for name, level in table.items():
        lg = logging.getLogger(name)
        if lg.level != level:
            lg.setLevel(level)
            changed.append(name)
    return changed
