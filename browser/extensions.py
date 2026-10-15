"""keeper 自带浏览器扩展（浮球）的两个常量/工具。

这个文件曾经有 187 行，管两件事：**用户扩展管理**（解包 ``.crx``、列出/删除已装
扩展，往 ``~/.keeper/chrome/extensions/`` 里放）和 **keeper 自带扩展**（给
``--load-extension`` 用）。现在前一半整块删掉了——``api/browser_agent.py`` 的扩展
管理端点随 Playwright 一起没了，那个 extensions 目录也没人再往里放东西了
（``~/.keeper/chrome/userdata`` 还在，但那是内置浏览器的 profile，与扩展无关）。

留下的两样都只服务一件事：**告诉用户"该把扩展装成什么样"**（keeper「浏览器」页）。

这个目录不再"装上就能用"：扩展由用户自己在 ``chrome://extensions`` 里加载这个未打包
目录，而内核升级会改这里的文件 —— 下次要重新点一次「重新加载」，否则浮球跑的还是旧
代码（这正是 ``Extension context invalidated`` 那类报错的背景）。

顺带一提，``--load-extension`` 这条路本身也不再走了：扩展装在**用户自己的 Chrome**
里，内核不参与任何启动参数，也就够不到那个进程的配置存储 —— 地址和 token 改成用户
粘贴「连接码」提供，见 ``desktop/keeper-extension/background.js``。

剩下那个函数算的是**用户该装出来的扩展 ID**，理由见它的 docstring。
"""

from pathlib import Path

# keeper 自带扩展（浮球）的目录。随内核发布，内核靠它算出上面那个"该装出来的 ID"，
# 好让「浏览器」页显示出来，供用户核对。
#
# 为什么不放进 ``~/.keeper``：扩展由用户在 Chrome 里自己加载，内核不往任何
# ``--load-extension`` 参数里塞路径；真正需要的只是这个路径本身。

SYSTEM_EXTENSIONS_ROOT = Path(__file__).resolve().parents[1] / "desktop" / "keeper-extension"


def extension_id_for_path(path: str | Path) -> str:
    """未打包扩展的 ID = ``sha256(绝对路径)`` 的前 16 字节按 nibble 映射成 a-p。

    为什么需要算它
    ------------
    扩展装在**用户的日常 Chrome** 里，内核没法再伸手进去替它写配置（那是另一个
    进程、另一个 profile），于是配置改成用户在界面上填。但
    :func:`~api.agent_bridge._origin_ok` 那道校验会比对浏览器带来的
    ``Origin: chrome-extension://<id>`` —— 所以界面必须把**该装出来的 ID** 告诉用户，
    否则装完连不上时，没有任何线索能看出是"路径选错了、ID 变了"。

    而静默失败恰恰是这类问题最难缠的地方：握手被拒之后扩展只会报"没连上内核"，
    用户无从判断是路径错了还是 token 过期。前端「浏览器」页会拿这个值和实际连上
    的 ID 对比，把差异直接摆出来。

    规则来源：Chromium 对未打包扩展用路径（绝对、不带尾斜杠）的 SHA-256 当 key，
    再按每字节两个 nibble 各映射到 ``a``~``p``，16 字节 → 32 个字符。
    **路径必须逐字节相同** —— 拷到别的目录、从符号链接打开、大小写不同，ID 都会变。

    带 ``key`` 字段的 manifest 不走这条路（ID 由开发者密钥决定），那种扩展
    自己往 manifest 里放 id 字段更省事。
    """
    import hashlib

    raw = str(path)
    # 尾斜杠会让哈希完全不同（实测同一目录带/不带斜杠是两个 ID），而用户从
    # chrome://extensions 的选择器里选目录时很容易带上它，所以这里统一剥掉。
    if len(raw) > 1 and raw.endswith("/"):
        raw = raw.rstrip("/") or "/"
    digest = hashlib.sha256(raw.encode("utf-8")).digest()[:16]
    return "".join(chr(ord("a") + n) for b in digest for n in (b >> 4, b & 0x0F))
