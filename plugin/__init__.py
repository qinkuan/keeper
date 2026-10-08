"""插件库：目录扫描、清单解析、每-agent 的链接管理。

``lib.py`` 原来叫 ``keeper/plugin_lib.py``，搬进本包是为了让「与插件有关的代码」
集中在一处。调用方统一 ``from ..plugin import X``。

对外的公共 API（其余名字以下划线开头，属内部实现，不在此承诺稳定）：

- 库与目录：``plugins_root`` / ``scan_library`` / ``find_plugin``
- 每-agent 的插件目录：``agent_plugins_dir`` / ``linked_plugins``
- 绑定：``link_plugin`` / ``unlink_plugin``
- 清单与依赖：``read_manifest`` / ``parse_env_dependencies`` /
  ``check_env_requirements`` / ``detect_runtime_versions``
- 类型：``LibraryPlugin`` / ``EnvDependency``
"""

from .lib import (
    PLUGIN_MANIFEST,
    EnvDependency,
    LibraryPlugin,
    agent_plugins_dir,
    check_env_requirements,
    detect_runtime_versions,
    find_plugin,
    link_plugin,
    linked_plugins,
    parse_env_dependencies,
    plugins_root,
    read_manifest,
    resolve_plugin_root,
    scan_library,
    unlink_plugin,
)

__all__ = [
    "PLUGIN_MANIFEST",
    "EnvDependency",
    "LibraryPlugin",
    "agent_plugins_dir",
    "check_env_requirements",
    "detect_runtime_versions",
    "find_plugin",
    "link_plugin",
    "linked_plugins",
    "parse_env_dependencies",
    "plugins_root",
    "read_manifest",
    "resolve_plugin_root",
    "scan_library",
    "unlink_plugin",
]
