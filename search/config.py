"""检索引擎配置：切片参数、检索参数、索引与模型落盘位置。

从独立项目 k-search 迁入（见 ``doc/indexing.md``）。迁移时按 keeper 的约定
改了三处：

1. **运行时数据落到 ``~/.keeper/`` 下**（与工作区、插件、记忆同源），不再落在
   仓库目录里——索引数据库和模型权重都不该进 git。
2. **被索引的目录默认取「我的空间」**（``settings.yaml`` 的 ``user_space.path``），
   不再是写死的绝对路径：keeper 里「用户的文件」就这一个位置。
3. **环境变量覆盖打开**（``KEEPER_SEARCH_*``）：keeper 其它模块也用 env 覆盖
   （如 ``KEEPER_WORKSPACE_ROOT``），保持一致。

配置优先级：**环境变量 > 这里的默认值**。``REPO_DIRS`` 为空时表示「用我的空间」。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

#: keeper 的家目录：运行时数据统一放这里（工作区 / 插件 / 记忆 / 检索索引）。
KEEPER_HOME = Path.home() / ".keeper"

#: 检索模块的家目录（索引数据库 + 模型权重）。
SEARCH_HOME = KEEPER_HOME / "search"

# ===================== 分块参数 =====================

# Markdown 结构化切片：标题树 + 块级元素 + 父子链
CHUNK_MIN_CHARS = 200        # 叶子内容短于此值时，尝试与同级相邻叶子合并
CHUNK_MAX_CHARS = 1000       # 叶子内容上限（代码块/表格/超长句子允许超出）
PARENT_MAX_CHARS = 2000      # 父块子树内容上限，超出则降级为摘要式父块
DOC_TYPE = "user_document"   # user_document（建多级父块到 H2）| user_profile（只建叶子）

#: **嵌入文本格式版本**。改动 ``embed_text`` 的构造方式（字段顺序、加了哪些内容、
#: 前缀变了……）时必须 +1，否则库里旧向量会被错误复用（向量复用的判据是
#: ``content_hash``，它只算正文、不含 embed_text）——改了格式却没换版本，
#: 等于改了等于没改。
#:
#: 版本变了的后果：prescreen 不再跳过这些文件（要重新切片）、旧向量不再复用
#: （全部重新嵌入）。一次全量重建的代价 ≈ 5 万 chunk × 30ms ≈ 25 分钟（后台跑）。
EMBED_FORMAT_VERSION = 4

#: **切片格式版本**。改动切片逻辑（而正文文字一个字都没变）时必须 +1。
#:
#: 与 ``EMBED_FORMAT_VERSION`` 分开的原因：**代价不同**。
#: - embed 版本变了 → ``embed_text`` 变了 → 旧向量作废 → 全部重新嵌入（花钱）；
#: - 切片版本变了 → 只是 chunk 里的定位信息过期（如 PDF 从「章节页区间」改成
#:   「精确页」）→ 重新切一遍即可，``content_hash`` 不变 → **旧向量全部复用**。
#:
#: 合进一列会让「要重嵌」和「只要重切」混为一谈，升级切片逻辑平白多花一次
#: 全量嵌入的钱。
#:
#: 用途：prescreen 用它判断「能不能只看 mtime+size 就跳过这个文件」——
#: 版本不一致时不跳过，重新切一遍（不重嵌）。
CHUNK_FORMAT_VERSION = 1

#: **关键词字段格式版本**。改动 ``tokens`` / ``title_tokens`` / ``name_tokens`` /
#: ``doc_title_tokens`` 的构造方式时必须 +1，否则老库里按旧规则算出来的字段
#: 会被继续使用（打分的输入错了，排序就错）。
#:
#: 与 ``EMBED_FORMAT_VERSION`` 不同：它变了**不需要重新嵌入向量**——
#: 关键词字段可以从 ``chunks.text`` / ``chunks.titlepath`` / ``documents`` 现算，
#: 一次回填即可（见 ``store/db.py::_backfill_keyword_fields``）。
KEYWORD_FORMAT_VERSION = 2

# ===================== 索引同步（自动更新） =====================

#: 是否启用后台定时增量扫描。关了就只能靠前端「建立索引」按钮或文件监听。
SYNC_ENABLED = True
#: 定时扫描间隔（秒）。扫描本身很便宜（只 stat，不读文件内容），
#: 3 万文件一次约 200ms，所以 30 秒一次也只有每天 14 分钟 CPU（0.6% 单核）。
#: 这是**兜底**——秒级响应靠桌面端的文件监听（desktop/spaceWatch.js）。
SYNC_INTERVAL = 30.0
#: 每隔多久做一次**全量 hash 校准**（秒）。定时扫描靠 stat 预筛，
#: 而 stat 会漏报（cp -p / rsync --times / 备份恢复保留了旧 mtime），
#: 所以要定期不信任 stat、把所有文件重新读一遍确认。
SYNC_FULL_INTERVAL = 1800.0


def _setting_number(key: str) -> float:
    """从用户设置（``settings.yaml`` 的 ``search`` 段）读一个数值。

    读不到 / 写坏了都返回 ``None``（调用方回落到环境变量或代码默认）。
    """
    try:
        from ..setting.user_settings import get_search_config

        v = get_search_config().get(key)
        return None if v is None else float(v)
    except Exception as e:  # noqa: BLE001 设置层不可用不该让检索模块挂掉
        logger.debug("读不到检索同步设置：%s", e)
        return None


def sync_enabled() -> bool:
    """是否启用自动同步（用户设置 > 环境变量 > 代码默认）。"""
    v = _setting_number("sync_enabled")
    if v is not None:
        return bool(v)
    return bool(SYNC_ENABLED)


def sync_interval() -> float:
    """增量扫描间隔（秒）。用户设置 > 环境变量 > 代码默认。"""
    v = _setting_number("sync_interval")
    if v is not None and v > 0:
        return v
    return float(SYNC_INTERVAL)


def sync_full_interval() -> float:
    """全量校准间隔（秒）。用户设置 > 环境变量 > 代码默认。"""
    v = _setting_number("sync_full_interval")
    if v is not None and v > 0:
        return v
    return float(SYNC_FULL_INTERVAL)

# ===================== 检索参数 =====================

DEFAULT_TOP_K = 10
SEARCH_MODE = "hybrid"    # hybrid（向量+关键词）| vector（纯向量）| keyword（纯 FTS5/BM25）
#: 融合方式。**由开发定死，不开放给前端调节**——融合方式直接决定最终分是不是
#: 0~1 的置信度，也就决定了分数门槛还有没有意义，让用户调容易调坏。
#: noisy_or（证据充分度，默认）| weighted（alpha 加权）
FUSION_MODE = "noisy_or"
RRF_K = 60                # RRF 常数，越大越"平"（名次差异被压缩）
VECTOR_TOP_N = 50         # 向量召回候选数
KEYWORD_TOP_N = 50        # 关键词（FTS5/BM25）召回候选数
#: weighted 模式下的向量权重 0~1，其余给关键词。同样是服务端定死，不开放前端。
HYBRID_ALPHA = 0.65

# ===================== 最终分（0~1）与阈值 =====================

#: **文件目录搜索**的展示门槛（最终分 0~1）。低于它的结果不展示。
MIN_SCORE_UI = 0.5

#: **agent 搜索**的可信门槛（0~1）。比 UI 严：agent 会把它当答案用，
#: 宁缺毋滥。
MIN_SCORE_AGENT = 0.8

#: 向量路的余弦 → 0~1 的标定区间：余弦 ≤ LO 记 0 分，≥ HI 记满分。
#: 实测（embeddinggemma，7 篇语料）：真命中 0.74~0.80，泛用词 0.71~0.77，
#: 语料里根本没有的词 0.64~0.73。语料越大、区分度越高。
SCORE_VEC_LO = 0.65
SCORE_VEC_HI = 0.80

#: 关键词路「证据充分度」里，各个位置最多能贡献多少。
#: 命中在文件名里 → 最可能就是用户要找的那个文件；只在正文里出现 → 可能是巧合。
KEYWORD_EVIDENCE_WEIGHTS = {
    "name": 1.0,        # 文件名
    "doc_title": 0.9,   # 文档标题
    "path": 0.8,        # 章节路径
    "body": 0.8,        # 正文
}

#: 关键词路的词频饱和参数（BM25 标准做法 k1）：出现次数越多，边际收益越小。
KEYWORD_K1 = 1.2

# ===================== 关键词打分（BM25F）=====================

#: **字段权重**（自己算 BM25F 时用；不再是 FTS5 的 bm25 列权重）。
#: 文件名权重最高——用户最可能记得的就是文件名。
KEYWORD_WEIGHTS = {
    "body": 1.0,        # 正文
    "path": 3.0,        # 章节路径（块级）
    "name": 8.0,        # 文件名（文档级）
    "doc_title": 3.0,   # 文档标题（文档级）
}

#: 长度归一强度（BM25 的 b）：0 = 不归一，1 = 完全按长度归一。
KEYWORD_B = 0.75

#: 候选召回上限。FTS5 只负责**召回**（谁含这些词），排序由我们自己算 BM25F，
#: 所以这里取多少个候选决定的是"打分范围"，不是最终条数。
KEYWORD_CANDIDATE_LIMIT = 400

# ===================== 索引数据库 =====================

DATA_DIR = SEARCH_HOME
DB_PATH = DATA_DIR / "search.db"

# ===================== 向量模型 =====================

# LiteRT-LM / .litertlm 端侧格式，用 litert-lm Python SDK 进程内加载
MODEL_REPO = "litert-community/embeddinggemma-2-740m-litert-lm"   # 下载源（HuggingFace 仓库）
MODEL_FILE = "embeddinggemma-2-740m.litertlm"                      # 通用 CPU/GPU 权重文件
MODEL_DIR = SEARCH_HOME / "models" / MODEL_REPO.split("/")[-1]     # 本地模型存储地址
# 向量推理后端：auto（自动探测，有 GPU 就用）| cpu | gpu
#
# 实测（macOS M1 Pro / embeddinggemma-2-740m）：
#   CPU：加载 0.2s，单条 62ms
#   GPU(Metal)：加载 6.4s（含 WebGPU kernel 编译），预热后单条 31ms
# 所以 GPU 对**批量索引**划算、对**单条查询**只是略快；而老机器 / 无 GPU 的
# Linux 服务器上 GPU 会直接加载失败——因此默认走 auto：探测不到就退 CPU。
EMBED_BACKEND = "auto"

# HuggingFace 镜像端点（主下载源，国内开箱即用）
HF_ENDPOINT = "https://hf-mirror.com"
# 备用下载源：阿里魔搭 ModelScope 模型 ID（与 HF 同模型，主源失败可回退）
MODELSCOPE_ID = "litert-community/embeddinggemma-2-740m-litert-lm"

# ===================== 被索引的目录 =====================

#: 显式指定的索引根目录。**留空 = 用「我的空间」**（用户设置里那一个目录）。
#:
#: 显式指定用环境变量 ``KEEPER_SEARCH_REPO``（多目录用 ``:`` 分隔），或直接改这里。
REPO_DIRS: List[str] = []


def default_roots() -> List[Path]:
    """要索引的目录列表。

    顺序：**显式配置 > 「我的空间」**。

    「我的空间」未配置时返回空列表——那不是错误，只是没有可索引的东西；
    调用方（``indexer.index_roots``）会索引到 0 个文件。
    """
    if REPO_DIRS:
        return [Path(p).expanduser() for p in REPO_DIRS]
    # 延迟 import：config 被 import 时不该连带把设置层拉起来
    try:
        from ..setting import get_user_space_path

        p = (get_user_space_path() or "").strip()
    except Exception as e:  # noqa: BLE001 设置层不可用不该让整个检索模块 import 失败
        # 「读不到配置」不是致命问题：没有可索引的东西而已。
        logger.debug("读不到「我的空间」配置：%s", e)
        p = ""
    return [Path(p).expanduser()] if p else []


# ===================== 环境变量覆盖 =====================
#
# keeper 其它模块也用 env 覆盖（``KEEPER_WORKSPACE_ROOT`` 等），这里保持一致。
# 全部走 ``KEEPER_SEARCH_*`` 前缀。

_env_repo = os.getenv("KEEPER_SEARCH_REPO", "").strip()
if _env_repo:
    REPO_DIRS = [p for p in _env_repo.split(os.pathsep) if p]

_env_db = os.getenv("KEEPER_SEARCH_DB_PATH", "").strip()
if _env_db:
    DB_PATH = Path(_env_db).expanduser()

_env_model_dir = os.getenv("KEEPER_SEARCH_MODEL_DIR", "").strip()
if _env_model_dir:
    MODEL_DIR = Path(_env_model_dir).expanduser()

_env_chunk_min = os.getenv("KEEPER_SEARCH_CHUNK_MIN_CHARS", "").strip()
if _env_chunk_min:
    CHUNK_MIN_CHARS = int(_env_chunk_min)

_env_chunk_max = os.getenv("KEEPER_SEARCH_CHUNK_MAX_CHARS", "").strip()
if _env_chunk_max:
    CHUNK_MAX_CHARS = int(_env_chunk_max)

_env_parent_max = os.getenv("KEEPER_SEARCH_PARENT_MAX_CHARS", "").strip()
if _env_parent_max:
    PARENT_MAX_CHARS = int(_env_parent_max)

_env_doc_type = os.getenv("KEEPER_SEARCH_DOC_TYPE", "").strip()
if _env_doc_type:
    DOC_TYPE = _env_doc_type

_env_top_k = os.getenv("KEEPER_SEARCH_TOP_K", "").strip()
if _env_top_k:
    DEFAULT_TOP_K = int(_env_top_k)

_env_mode = os.getenv("KEEPER_SEARCH_MODE", "").strip()
if _env_mode:
    SEARCH_MODE = _env_mode

_env_fusion = os.getenv("KEEPER_SEARCH_FUSION", "").strip()
if _env_fusion:
    FUSION_MODE = _env_fusion

_env_alpha = os.getenv("KEEPER_SEARCH_HYBRID_ALPHA", "").strip()
if _env_alpha:
    HYBRID_ALPHA = float(_env_alpha)

_env_backend = os.getenv("KEEPER_SEARCH_BACKEND", "").strip()
if _env_backend:
    EMBED_BACKEND = _env_backend

_env_sync_enabled = os.getenv("KEEPER_SEARCH_SYNC_ENABLED", "").strip()
if _env_sync_enabled:
    SYNC_ENABLED = _env_sync_enabled.lower() not in ("0", "false", "no", "off")

_env_sync_interval = os.getenv("KEEPER_SEARCH_SYNC_INTERVAL", "").strip()
if _env_sync_interval:
    SYNC_INTERVAL = float(_env_sync_interval)

_env_sync_full = os.getenv("KEEPER_SEARCH_SYNC_FULL_INTERVAL", "").strip()
if _env_sync_full:
    SYNC_FULL_INTERVAL = float(_env_sync_full)
