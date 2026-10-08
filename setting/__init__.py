"""用户设置：读写 ``~/.keeper/settings.yaml``（设置页改的东西都落这里）。

与 ``config.yaml`` 的分工：

- ``config.yaml`` —— **元配置**，只由人手动改（模型清单、provider、插件目录等）；
- ``settings.yaml`` —— **用户设置**，由设置页写入、**立即生效**，无需重启；
  读取时覆盖 ``config.yaml`` 的同名段。

只有几个「用的时候调」的段会进 settings.yaml（见 ``api/settings.py``）：
``observability`` / ``a2a`` / ``context``。每次读取都会与代码默认值合并，
所以 settings.yaml 里缺项也能正常跑。

``user_settings.py`` 原是 ``keeper/user_settings.py``，搬进本包是为了让
「与设置读写有关的代码」集中在一处。调用方统一 ``from ..setting import X``。
"""

from .user_settings import (
    KNOWN_SECTIONS,
    SETTINGS_FILENAME,
    default_settings,
    ensure_defaults,
    fingerprint,
    invalidate_caches,
    load_raw,
    save_raw,
    save_sections,
    section,
    settings_path,
)

__all__ = [
    # 路径
    "settings_path",
    "SETTINGS_FILENAME",
    "KNOWN_SECTIONS",
    # 读写
    "load_raw",
    "save_raw",
    "section",
    "save_sections",
    "default_settings",
    "ensure_defaults",
    # 变更通知
    "fingerprint",
    "invalidate_caches",
]
