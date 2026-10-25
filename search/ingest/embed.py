"""EmbeddingGemma (LiteRT-LM / .litertlm) 封装：进程内用 litert-lm Python SDK 加载。

官方要求文本输入加任务指令前缀（否则检索质量下降）：
  - 文档索引： "task: search result | text: " + 文本
  - 搜索查询： "task: search query | text: " + 文本
详见 https://ai.google.dev/edge/litert-lm 。
"""
import logging
import platform
import threading
from pathlib import Path

from .. import config
from ..deps import require_numpy

logger = logging.getLogger(__name__)

# 任务指令前缀（必须）
_DOC_PREFIX = "task: search result | text: "
_QUERY_PREFIX = "task: search query | text: "

try:
    from litert_lm import Backend, EmbeddingEngine
except Exception:  # 未安装时给友好提示，而非 import 阶段崩溃
    Backend = None
    EmbeddingEngine = None


def gpu_probe() -> tuple:
    """这台机器**有没有可能**用上 GPU 加速。

    返回 ``(可用, 说明)``。

    为什么是「可能」而不是「一定」：litert-lm 的 Python API **不暴露**设备枚举
    （accelerator registry 在 C++ 内部，只在日志里露一手），所以静态判断只能
    **排除明显没 GPU 的情况**，剩下的交给「真加载一次」验证。

    静态判断能省下什么：没有 GPU 的 Linux 服务器上，试加载 GPU 要白等好几秒
    甚至直接崩——而 `/dev/dri`、`/dev/nvidia*` 都不存在，这一眼就能看出来。
    """
    system = platform.system()
    machine = (platform.machine() or "").lower()

    if system == "Darwin":
        if machine == "arm64":
            return True, "Apple Silicon（Metal）"
        # Intel Mac：macOS 10.15+ 都有 Metal，集显也能跑；虚拟机里可能没有，
        # 那种情况由加载失败回退兜住。
        return True, "macOS Intel（Metal）"
    if system == "Windows":
        # WebGPU/DX12 后端：有没有独显静态看不出来，交给加载时验证。
        return True, "Windows（WebGPU / DX12）"
    if system == "Linux":
        if Path("/dev/dri").exists() or Path("/dev/nvidia0").exists():
            return True, "Linux（Vulkan / 有渲染设备）"
        return False, "Linux 上没找到 GPU 设备节点（/dev/dri、/dev/nvidia*）"
    return False, f"未识别的平台：{system}"


def _resolve_backend():
    """按配置决定用哪个后端。

    ``auto`` → 探测到 GPU 就 GPU，否则 CPU；显式 ``cpu`` / ``gpu`` → 照办
    （但 GPU 加载失败仍会回退，见 :meth:`Embedder.engine`）。
    """
    if Backend is None:
        return None
    want = (getattr(config, "EMBED_BACKEND", "auto") or "auto").lower()
    if want == "gpu":
        return Backend.GPU()
    if want == "cpu":
        return Backend.CPU()
    ok, reason = gpu_probe()
    if ok:
        logger.info("检测到可用 GPU：%s", reason)
        return Backend.GPU()
    logger.info("没有可用 GPU（%s），用 CPU", reason)
    return Backend.CPU()


class Embedder:
    """进程内加载 .litertlm，用 litert-lm 的 EmbeddingEngine 计算向量。"""

    def __init__(self, model_path=None, backend=None):
        self.model_path = str(model_path or (config.MODEL_DIR / config.MODEL_FILE))
        self.backend = backend if backend is not None else _resolve_backend()
        self._engine = None
        self._dimension = None
        #: 回退原因（GPU 不可用时记下来，页面上要能看见「为什么没用上 GPU」）
        self.backend_note = ""
        #: 模型是否已经加载并预热过（启动预热会置位，页面上显示「已预热」）
        self.warmed = False
        # 推理调用串行化：keeper 的 FastAPI 把同步端点丢进线程池，同一个
        # embedder 可能被多个请求线程同时用到，而推理引擎的并发安全性没有保证。
        # 检索是低频操作，串行化的代价可以忽略。
        self._lock = threading.Lock()

    @property
    def backend_name(self) -> str:
        """当前实际用的后端：``gpu`` / ``cpu``（litert-lm 没装时为空）。"""
        if Backend is None:
            return ""
        if isinstance(self.backend, Backend.GPU):
            return "gpu"
        if isinstance(self.backend, Backend.CPU):
            return "cpu"
        return ""

    def _open_engine(self, backend):
        """加载模型并**预热**一次。

        预热是必须的：GPU 后端第一次推理要编译 WebGPU kernel（实测 1 秒），
        不预热的话这笔开销会落在用户的第一次搜索上——一个 62ms 的查询突然卡 1 秒，
        用户会以为卡死了。放到加载期，索引管理页上就能看到「加载中」。
        """
        engine = EmbeddingEngine(
            self.model_path,
            backend=backend,
            vision_backend=None,
            audio_backend=None,
        ).__enter__()
        try:
            engine.compute_embedding("warmup")
        except Exception as e:  # noqa: BLE001
            logger.warning("预热失败（%s）：%s", type(e).__name__, e)
            raise
        self.warmed = True
        return engine

    @property
    def engine(self):
        if self._engine is None:
            if EmbeddingEngine is None:
                raise RuntimeError(
                    "未找到 litert-lm，请先安装：pip install litert-lm==0.18.0"
                )
            try:
                self._engine = self._open_engine(self.backend)
            except Exception as e:  # noqa: BLE001
                if not isinstance(self.backend, Backend.GPU):
                    raise
                # GPU 加载/预热失败 → 退 CPU。
                # 「搜不了」比「搜得慢」严重得多，所以这里无条件回退，只记一条日志。
                self.backend_note = f"GPU 不可用，已回退 CPU（{type(e).__name__}: {str(e)[:120]}）"
                logger.warning("%s", self.backend_note)
                self.backend = Backend.CPU()
                self._engine = self._open_engine(self.backend)
        return self._engine

    def encode(self, texts):
        """对给定文本直接向量化（指令前缀由调用方拼好）。"""
        items = list(texts)
        with self._lock:
            eng = self.engine  # 首次触发加载
            try:
                vecs = [eng.compute_embedding(it).embedding for it in items]
            except Exception as e:  # noqa: BLE001
                if not isinstance(self.backend, Backend.GPU):
                    raise
                # GPU 能加载但推理时炸（比如显存不够、kernel 编译失败）→ 退 CPU 重来。
                self.backend_note = f"GPU 推理失败，已回退 CPU（{type(e).__name__}: {str(e)[:120]}）"
                logger.warning("%s", self.backend_note)
                self.backend = Backend.CPU()
                self._engine = self._open_engine(self.backend)
                vecs = [self._engine.compute_embedding(it).embedding for it in items]
        np = require_numpy()

        arr = np.array(vecs, dtype=np.float32)
        if arr.shape[0]:
            self._dimension = arr.shape[1]
        return arr

    def embed_documents(self, texts, batch_size=32):
        """对叶子 chunk 的 embed_text 向量化（前缀已在 embed_text 中，不再重复添加）。"""
        out = []
        for i in range(0, len(texts), batch_size):
            out.append(self.encode(list(texts[i:i + batch_size])))
        np = require_numpy()

        arr = np.vstack(out) if out else np.empty((0, 0), dtype=np.float32)
        return arr

    def embed_query(self, text):
        arr = self.encode([_QUERY_PREFIX + text.strip()])
        return arr[0]

    @property
    def dimension(self):
        if self._dimension is None:
            self.embed_documents(["__probe__"])
        return self._dimension


# --------------------------------------------------------------------------
# 共享单例
#
# 为什么单例要放在**这一层**而不是各调用方自己 new：
# 预热（``search/warmup.py``）在应用启动时把模型加载好，用户第一次搜索就该是
# 快的。如果预热和查询各持一个 Embedder，预热加载的权重会被丢掉，查询时照样
# 要重新加载一遍——预热就白做了。
#
# 构造 ``Embedder`` 本身不加载权重（真正的加载在第一次 embed 时），所以拿单例
# 很便宜，可以放心在任何地方调。

_embedder_lock = threading.Lock()
_embedder: Embedder = None
_embedder_failed = False


def get_embedder() -> Embedder:
    """跨调用复用的 embedder；没装 litert-lm 时返回 ``None``。

    失败过一次就不再重试（每次请求都重试会刷屏，而且必然还是失败）。
    """
    global _embedder, _embedder_failed
    if _embedder is None and not _embedder_failed:
        with _embedder_lock:
            if _embedder is None and not _embedder_failed:
                try:
                    _embedder = Embedder()
                except Exception as e:  # noqa: BLE001
                    _embedder_failed = True
                    logger.warning("embedding 模型不可用（降级为纯关键词检索）：%s", e)
    return _embedder
