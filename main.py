"""通用 agent 命令行入口（单进程多 agent 版）。

用法：
    python -m keeper.main                          # 默认读同目录 config.yaml
    python -m keeper.main --config <config.yaml> [port]

职责：
1. 读框架配置（server 端口 / 鉴权）；
2. 建表（幂等）；
3. 从库装载所有 active agent 到同一进程（每个 agent 一个实例，按 agent_id 路由）；
4. 起 HTTP：对话/配置接口挂在 /agents/{agent_id} 下；前端（web/dist）作为站点根
   被同源托管——/ 是 landing 页（列所有 agent），点进去 ?agent=<id> 进入对话。

A2A 入站：**按目标 agent 路由**——每个已装载 agent 各有自己的端点
``POST /agents/{agent_id}/a2a`` 与 ``GET /agents/{agent_id}/.well-known/agent.json``。
因此单进程多 agent 下，外部 agent（或本进程内另一个 agent）可用
``http://host:port/agents/{agent_id}/a2a`` 精确调到某一个 agent。
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

# 支持直接运行本文件（IDE 右键 Run/Debug、python keeper/main.py）。
# 坑：直接运行时 sys.path 里会有脚本所在目录 keeper/，它与 keeper 包同名，
# 若其中存在 keeper.py 就会遮蔽真正的包。所以先摘掉该目录，再把项目根插到最前。
_HERE = str(Path(__file__).resolve().parent)         # .../niubiplatform/keeper
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])  # .../niubiplatform
sys.path[:] = [p for p in sys.path if p != _HERE]
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from keeper.config import load_config
from keeper.agent.keeper import build_all_agents, list_keepers, shutdown_all
from keeper.store import dispose_engine, init_db


def _setup_logging() -> None:
    """把日志直接打到控制台（前台运行时可见），级别可用 LOG_LEVEL 覆盖。

    每条日志前面带 ``[agent]``：单进程装多个 agent 时，LLM 调用 / 工具 / 对端
    调用这些日志会混在一起，没有它分不清哪行属于哪个 agent。值由请求入口写进
    ContextVar（见 ``chat.context.set_agent``），这里用 filter 读出来注入；
    不在任何 agent 上下文里（比如启动装载阶段）时显示 ``-``。
    """
    from keeper.chat.context import agent_label

    class _AgentFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if not hasattr(record, "agent"):
                record.agent = agent_label()
            return True

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s [%(agent)s] %(name)s | %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    # basicConfig 建的是 root 的 handler，给它挂上 filter 即可覆盖所有
    # 没有独立 handler 的 logger（keeper.*、httpx 等都会走到 root）。
    for h in logging.getLogger().handlers:
        h.addFilter(_AgentFilter())

    # 可选的**文件**日志：宝塔进程守护 / systemd 这类拉起方式，日志只进容器里
    # 的 stdout，进程一重启上次的记录就没了——排查「上次那个错为什么发生」
    # 全靠人肉复现。设 LOG_FILE 即额外落一份（控制台仍然照打）。
    #
    # 一定要用 Rotating：agent 的 LLM 调用与工具日志量不小，不限大小能把磁盘
    # 撑爆；10MB × 5 份 = 最多 50MB，够回溯又不失控。
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler

            path = Path(log_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                str(path),
                maxBytes=int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024)),
                backupCount=int(os.getenv("LOG_BACKUP_COUNT", 5)),
                encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
            fh.addFilter(_AgentFilter())
            logging.getLogger().addHandler(fh)
            logging.getLogger().info("日志文件已开启：%s", path)
        except Exception as e:  # noqa: BLE001
            # 写不了日志文件不该拦住服务启动：控制台还能看，只是没有留档。
            logging.getLogger().warning("开启 LOG_FILE 失败（继续用控制台）：%s", e)


def make_app():
    """构建 FastAPI 应用：CORS + 业务路由（对话）+ 前端静态托管。"""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from keeper.chat.api import router as chat_router
    from keeper.chat.api import user_space_router
    from keeper.api.agents import router as agents_router
    from keeper.api.models import models_router
    from keeper.api.metrics import router as metrics_router
    from keeper.framework.api import router as framework_router

    app = FastAPI(title="Keeper")

    # 用户设置：把「代码里有、settings.yaml 里还没写」的键按默认值补进去。
    # 目的不是改行为（补的值就等于默认值），而是让本机那份文件始终是**全量配置**：
    # 想改哪一项不用先去代码里查默认值是多少。幂等，且只做定点插入、不动注释。
    from keeper.setting import ensure_defaults

    ensure_defaults()

    # 允许本地前端（keeper/web 的 vite dev server）直接调用；生产同源托管无需跨域。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5273",
            "http://127.0.0.1:5273",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 对话 / 配置接口：/agents/{agent_id}/...（按 agent 路由到本进程内实例）
    app.include_router(chat_router)
    # 用户空间模块（用户自管的命名文件路径）
    app.include_router(user_space_router)
    # 本地运行时只保留 agent 相关管理接口（配置缓存 / 热重载）。
    # mcp / skill / tool 的定义已统一归 platform 管，keeper 不再提供也不再维护。
    app.include_router(agents_router)
    # 插件库（只读：库根路径 + 库里有哪些插件）
    from keeper.api.plugins import router as plugins_router

    app.include_router(plugins_router)
    # 模型管理（本地维护的模型预设；agent 通过绑定表引用）
    app.include_router(models_router)
    # 可观测：用量聚合查询（/metrics/...），数据来自 llm_calls 事实表
    app.include_router(metrics_router)
    # 效果评估：读历史基线 + 触发评测（评测本体跑在子进程里，见 keeper/api/evals.py）
    from keeper.api.evals import router as evals_router

    app.include_router(evals_router)
    # 设置：本地可观测 / 调试开关（写回 ~/.keeper/settings.yaml，**不碰**
    # config.yaml —— 元配置只由人手动改，见 keeper/setting/user_settings.py）
    from keeper.api.settings import router as settings_router

    app.include_router(settings_router)
    # framework 环境（~/.keeper/framework 下集中管理的 python / node 运行时）
    app.include_router(framework_router)
    # A2A 入站：按 agent 路由，每个已装载 agent 各有自己的端点
    # POST /agents/{agent_id}/a2a 与 GET /agents/{agent_id}/.well-known/agent.json
    from keeper.a2a import build_a2a_router

    app.include_router(build_a2a_router())

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "agents": [
                {
                    "id": a.agent_id,
                    "name": a.name,
                    "ready": a.check_readiness().ok,
                }
                for a in list_keepers()
            ],
        }

    # 生产托管：把构建好的前端（web/dist）作为站点根，单进程同时提供 API + 页面。
    # 放在最后，确保 /agents、/health 等业务路由优先于通配的静态挂载匹配。
    web_dist = Path(_HERE) / "web" / "dist"
    if web_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")
    else:
        logging.getLogger("keeper").warning(
            "未找到前端构建产物 %s，仅提供 API；开发可用 keeper/web/start_web.sh 起前端",
            web_dist,
        )

    return app


async def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    """启动 HTTP 服务；退出时优雅关闭所有 agent（释放 MCP 等）并释放连接池。"""
    import uvicorn

    app = make_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await shutdown_all()
        # 连接池里没归还的连接不能在退出时被 GC 掉——那会刷一串
        # "non-checked-in connection ... will be terminated" 警告。显式释放，安静退出。
        try:
            await dispose_engine()
        except Exception:  # noqa: BLE001
            # 退出路径不该因为释放失败而抛异常，掩盖真正的退出原因
            logging.getLogger("keeper").debug("释放数据库连接池失败（忽略）", exc_info=True)


def main() -> None:
    _setup_logging()
    logger = logging.getLogger("keeper")
    args = sys.argv[1:]

    # --config 指定框架配置；剩下的位置参数是端口
    cfg_path = None
    rest: list = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--config":
            cfg_path = args[i + 1] if i + 1 < len(args) else None
            i += 2
        else:
            rest.append(a)
            i += 1

    cfg = load_config(cfg_path)   # 框架：端口 / 鉴权
    host, port = cfg.host, cfg.port
    if rest:
        port = int(rest[0])   # 命令行位置参数优先级最高

    logger.info("监听地址: %s:%d", host, port)

    async def run() -> None:
        # 1) 初始化存储：建表（幂等，已存在则跳过）
        await init_db()
        # 2) 从数据库装载所有 active agent 到本进程（单个失败只跳过，不拖垮整体）
        await build_all_agents()
        # 3) 启动对外 HTTP API；Ctrl+C 时 serve() 的 finally 会优雅关闭
        await serve(host=host, port=port)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已退出")


if __name__ == "__main__":
    main()
