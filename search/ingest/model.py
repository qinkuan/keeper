"""模型下载与就绪检测：从 HuggingFace（镜像）下载 embeddinggemma-2-740m.litertlm。

下载在后台线程进行，状态通过模块级 _state 暴露，供 /model/status 轮询。
"""
import os
import threading
from pathlib import Path

from .. import config

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


def _download_hf(model_dir: Path) -> None:
    from huggingface_hub import snapshot_download
    if config.HF_ENDPOINT:
        os.environ.setdefault("HF_ENDPOINT", config.HF_ENDPOINT)
    snapshot_download(
        repo_id=config.MODEL_REPO,
        allow_patterns=[config.MODEL_FILE],
        local_dir=str(model_dir),
        local_dir_use_symlinks=False,
    )


def _download_modelscope(model_dir: Path) -> None:
    if not config.MODELSCOPE_ID:
        raise ValueError("未配置 MODELSCOPE_ID（litert 模型仅有 HuggingFace 源）")
    from modelscope import snapshot_download as ms_download
    try:
        ms_download(
            model_id=config.MODELSCOPE_ID,
            local_dir=str(model_dir),
            allow_patterns=[config.MODEL_FILE],
        )
    except TypeError:
        # 旧版 modelscope 不支持 allow_patterns 参数，回退为全量拉取（保证可用）
        ms_download(model_id=config.MODELSCOPE_ID, local_dir=str(model_dir))


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
                _download_hf(model_dir)
            except Exception as e:  # 主源失败，回退备源
                if source == "hf":
                    raise
                _set_state("downloading", f"HF 镜像失败，回退魔搭：{e}", "modelscope")
                _download_modelscope(model_dir)
        elif source == "modelscope":
            _set_state("downloading", "从魔搭下载中…", "modelscope")
            _download_modelscope(model_dir)

        if is_model_ready(model_dir):
            _set_state("done", "模型已就绪", source)
        else:
            _set_state("error", "下载完成但模型文件不完整", source)
    except Exception as e:
        _set_state("error", f"下载失败：{e}", source)


def start_download(source: str = "auto") -> None:
    """在后台线程启动下载，调用方立即返回。"""
    threading.Thread(target=download_model, args=(source,), daemon=True).start()
