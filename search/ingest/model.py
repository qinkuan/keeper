"""模型下载与就绪检测：把 embeddinggemma-2-740m.litertlm 拉到本地。

下载在后台线程进行，状态通过模块级 _state 暴露，供 /model/status 轮询。

为什么不用 huggingface_hub / modelscope
---------------------------------------
这两个包都**不在 keeper 的依赖清单里**（各自会带来一大串传递依赖），而模型
下载本质就是「按 URL 拉一个大文件」—— httpx 已经在依赖里，自己下更可控：

- 少两个重量级依赖，打包产物更小；
- 能报下载进度（huggingface_hub 的进度条在后台线程里不好透出来）；
- 镜像源可配（``KEEPER_SEARCH_HF_ENDPOINT``），国内默认走 hf-mirror。

下载源：

1. HuggingFace 镜像（默认 ``https://hf-mirror.com``）
2. 魔搭 ModelScope（备源，主源失败时回退）
"""
import logging
import threading
import time
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

# 单进程内共享的下载状态
_state = {
    "status": "idle",   # idle | downloading | done | error
    "message": "",
    "source": "",       # hf | modelscope
}
_lock = threading.Lock()


def is_model_ready(model_dir=None) -> bool:
    """判断本地是否已下载 .litertlm 权重文件。"""
    model_dir = Path(model_dir or config.MODEL_DIR)
    return (model_dir / config.MODEL_FILE).is_file()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _set_state(status: str, message: str = "", source: str = "") -> None:
    with _lock:
        _state["status"] = status
        _state["message"] = message
        _state["source"] = source


def _model_url(source: str) -> str:
    """模型文件的直链（source: hf | modelscope）。"""
    if source == "modelscope":
        # 魔搭的 resolve 形式：/models/<id>/resolve/master/<文件>
        return (
            f"https://www.modelscope.cn/models/{config.MODELSCOPE_ID}"
            f"/resolve/master/{config.MODEL_FILE}"
        )
    endpoint = (config.HF_ENDPOINT or "https://huggingface.co").rstrip("/")
    # HF 的 resolve 会 302 到 CDN（hf-mirror 也一样），httpx 跟着重定向即可
    return f"{endpoint}/{config.MODEL_REPO}/resolve/main/{config.MODEL_FILE}"


def _download_http(model_dir: Path, source: str) -> None:
    """用 httpx 直连下载模型文件（带进度上报 + 原子落盘）。"""
    import httpx

    url = _model_url(source)
    target = model_dir / config.MODEL_FILE
    tmp = target.with_name(target.name + ".part")
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)

    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        done = 0
        last_report = 0.0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report >= 1.0:
                    last_report = now
                    if total:
                        pct = f"{done * 100 // total}%"
                        size = f"{done // (1 << 20)}MB / {total // (1 << 20)}MB"
                    else:
                        pct, size = "", f"{done // (1 << 20)}MB"
                    _set_state("downloading", f"下载中 {pct}（{size}）", source)

    # 原子替换：下到一半断线不会留下半个模型文件冒充「已就绪」
    tmp.replace(target)


def download_model(source: str = "auto") -> None:
    """下载 .litertlm 到 config.MODEL_DIR。source: auto | hf | modelscope。"""
    model_dir = Path(config.MODEL_DIR)
    model_dir.mkdir(parents=True, exist_ok=True)

    if is_model_ready(model_dir):
        _set_state("done", "模型已就绪", "")
        return

    _set_state("downloading", "准备下载…", source)
    try:
        if source in ("auto", "hf"):
            try:
                _set_state("downloading", "从 HuggingFace 镜像下载中…", "hf")
                _download_http(model_dir, "hf")
            except Exception as e:  # 主源失败，回退备源
                if source == "hf":
                    raise
                _set_state("downloading", f"HF 镜像失败，回退魔搭：{e}", "modelscope")
                _download_http(model_dir, "modelscope")
        elif source == "modelscope":
            _set_state("downloading", "从魔搭下载中…", "modelscope")
            _download_http(model_dir, "modelscope")

        if is_model_ready(model_dir):
            _set_state("done", "模型已就绪", source)
            _notify_ready()
        else:
            _set_state("error", "下载完成但模型文件不完整", source)
    except Exception as e:
        _set_state("error", f"下载失败：{e}", source)


def _notify_ready() -> None:
    """模型刚就绪 → 触发一次索引同步（后台线程）。

    为什么需要这一步：用户常见的顺序是"先建关键词索引、后来才下载模型"。
    这时索引里的向量还是空的，而文档本身没变——普通增量扫描原来会认为
    "没变过"直接跳过，于是语义检索一直是关着的（界面上却显示模型已就绪）。
    现在跳过条件会看"向量齐不齐"，缺向量的文档会被重新处理、顺带补上向量；
    这里再主动触发一次扫描，用户就不用干等下一个同步周期。
    """
    def _work() -> None:
        try:
            from .. import sync

            stats = sync.scan_once()
            if stats:
                logger.info("模型就绪后已同步索引：%s", stats)
        except Exception as e:  # noqa: BLE001 补建失败不影响模型本身可用
            logger.warning("模型就绪后同步索引失败（不影响使用）：%s", e)

    threading.Thread(target=_work, daemon=True).start()


def start_download(source: str = "auto") -> None:
    """在后台线程启动下载，调用方立即返回。"""
    threading.Thread(target=download_model, args=(source,), daemon=True).start()
