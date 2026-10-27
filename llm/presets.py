"""出厂模型预设包（**只读**）：给前端填表用的模板数据。

它是什么
--------
随发行包分发的一份 YAML（``keeper/presets/llm/<服务商>.yaml``），里面写着
各家服务商支持哪些模型、地址是什么、上下文多大、单价多少。

它的边界（很重要）
------------------
**预设只是前端填表时的"默认值 + 候选项"**：向导里选服务商 → 自动带出地址、
上下文、价格 → 用户想改哪项改哪项 → 提交后**落库的一切以提交的数据为准**。

所以服务端**不记账、不合并**：库里没有"这条来自哪个预设"的痕迹，也不会有
"预设升级后覆盖用户配置"的逻辑。用户建的就是用户的，与预设包再无关系。

价格：可以给多段
----------------
有些服务商按"高峰 / 非高峰"两档收费。keeper 的价段支持 **星期 + 时段** 两个
维度，取价时"更具体的那段优先"（有时段的比不限时段的更具体，见
``observability/pricing.py`` 的 ``_price_for``），所以错峰能直接表达：

    段 1：全天兜底（非高峰价）—— weekdays / 时段都不限
    段 2：周一至周五 09:00–12:00（高峰价）
    段 3：周一至周五 14:00–18:00（高峰价）

时段按**机器本地时间**匹配（``pricing.py`` 的 ``_minute_of_day`` 用
``when.astimezone()``），所以预设里的时段是"本地时间口径"——按 UTC+8 写的
预设，在 UTC+8 的机器上完全准确；机器时区不同则错峰匹配会整体偏移。

计价一律用人民币
----------------
预设里的价格统一用 **CNY（¥）**。原因很实际：成本报表会把不同模型的花费
**加在一起**，混着美元和人民币的两行数字，加出来的总额是错的（还看不出错在哪）。
需要看美元价的用户可以在模型管理页自己加价段。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 预设包目录：keeper/presets/llm/*.yaml（随发行包分发，冻结形态落在
# <MEIPASS>/keeper/presets/llm —— 与 packaging/keeper-kernel.spec 的 datas 对齐）。
PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets" / "llm"

# 参与「预设 → 数据库」写回的字段。**api_key 不在内**：预设不带密钥，
# 用户填的 Key 任何时候都不该被预设动。
MERGE_FIELDS = (
    "name",
    "provider",
    "model_name",
    "base_url",
    "context_limit",
    "temperature",
    "timeout",
    "max_retries",
)

# 「用户没填过」的哨兵。区别于 None：None 是"显式清空"，没填过是"没这回事"。
_MISSING = object()


@dataclass
class PresetPrice:
    """一段价格（可能是全天兜底，也可能是某个时段的错峰价）。"""

    input_per_1m: Optional[float] = None
    cached_per_1m: Optional[float] = None
    output_per_1m: Optional[float] = None
    currency: str = "CNY"
    #: 适用星期：逗号分隔的 ISO 星期号（1=周一 … 7=周日）；空 = 不限
    weekdays: Optional[str] = None
    #: 时段（本地时间口径，一天内的分钟数 0–1439）；与 time_to 同时为 None = 不限
    time_from_minute: Optional[int] = None
    time_to_minute: Optional[int] = None
    note: Optional[str] = None

    @property
    def has_price(self) -> bool:
        return self.input_per_1m is not None and self.output_per_1m is not None


@dataclass
class PresetModel:
    """一条出厂预设（= 数据库里一行 llm_profiles）。

    字段与 ``llm_profiles`` 对齐，另加展示用的 label / docs_url 与价格段。
    """

    key: str
    provider: str
    model_name: str
    name: str
    label: str = ""
    base_url: Optional[str] = None
    context_limit: Optional[int] = None
    temperature: float = 0.2
    timeout: int = 120
    max_retries: int = 3
    #: 价格段（可能多段：全天兜底 + 高峰时段…）
    prices: List[PresetPrice] = field(default_factory=list)
    docs_url: Optional[str] = None

    @property
    def preset_key(self) -> str:
        """稳定标识：``<provider>.<key>``。

        用 provider + 文件内 key，而不是展示名：改名（本地化、避重名）不该让
        预设"变成另一个预设"。
        """
        return f"{self.provider}.{self.key}"

    @property
    def has_price(self) -> bool:
        return any(p.has_price for p in self.prices)

    def profile_values(self) -> Dict[str, Any]:
        """要写进 llm_profiles 的字段（不含 api_key）。"""
        return {
            "name": self.name,
            "provider": self.provider,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "context_limit": self.context_limit,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }


@dataclass
class PresetFile:
    """一个预设文件（通常一个服务商一个文件）。"""

    provider: str
    label: str = ""
    base_url: Optional[str] = None
    docs_url: Optional[str] = None
    defaults: Dict[str, Any] = field(default_factory=dict)
    models: List[PresetModel] = field(default_factory=list)


# --------------------------------------------------------------------------
# 读取预设包
# --------------------------------------------------------------------------

def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _hhmm_to_minute(v: Any) -> Optional[int]:
    """``"09:30"`` → 570。非法值返回 None（当成不限时段，别静默算错时段）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _parse_prices(item: Dict[str, Any]) -> List[PresetPrice]:
    """解析一个条目的价格。

    两种写法都支持：

    - 单段写法 ``price: {input_per_1m: …, cached_per_1m: …, output_per_1m: …}``
    - 多段写法 ``prices: [ {…}, {…} ]``（错峰用）

    单段写法会被转成"全天兜底"的一段，行为与之前一致。
    """
    out: List[PresetPrice] = []

    def one(p: Dict[str, Any], fallback_currency: str) -> Optional[PresetPrice]:
        if not isinstance(p, dict):
            return None
        return PresetPrice(
            input_per_1m=_as_float(p.get("input_per_1m", p.get("input_per_1m"))),
            cached_per_1m=_as_float(p.get("cached_per_1m", p.get("cached_per_1m"))),
            output_per_1m=_as_float(
                p.get("output_per_1m", p.get("output_per_1m"))
            ),
            currency=str(p.get("currency", fallback_currency) or fallback_currency),
            weekdays=(str(p.get("weekdays")).strip() if p.get("weekdays") else None),
            time_from_minute=_hhmm_to_minute(
                p.get("time_from", p.get("time_from_minute"))
            ),
            time_to_minute=_hhmm_to_minute(
                p.get("time_to", p.get("time_to_minute"))
            ),
            note=str(p.get("note")).strip() if p.get("note") else None,
        )

    fallback_currency = str(item.get("currency") or "CNY")
    single = one(item.get("price") or {}, fallback_currency)
    if single and (single.has_price or single.note):
        out.append(single)

    raw_list = item.get("prices")
    if isinstance(raw_list, list):
        for p in raw_list:
            seg = one(p, fallback_currency)
            if seg and seg.has_price:
                out.append(seg)
    return out


def _parse_models(
    raw_models: Any, provider: str, base_url: Optional[str], defaults: Dict[str, Any]
) -> List[PresetModel]:
    """把一个文件里的 models 段解析成 PresetModel 列表（跳过不合法的条目）。"""
    out: List[PresetModel] = []
    if not isinstance(raw_models, list):
        return out
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        model_name = str(item.get("model_name") or key).strip()
        if not key or not model_name:
            logger.warning("预设条目缺少 key/model_name，跳过：%r", item)
            continue
        price = item.get("price") or {}
        if not isinstance(price, dict):
            price = {}
        out.append(
            PresetModel(
                key=key,
                provider=str(item.get("provider") or provider).strip(),
                model_name=model_name,
                name=str(item.get("name") or model_name).strip(),
                label=str(item.get("label") or "").strip(),
                base_url=(
                    str(item.get("base_url")).strip()
                    if item.get("base_url")
                    else base_url
                ),
                context_limit=_as_int(item.get("context_limit")),
                temperature=float(
                    item.get("temperature", defaults.get("temperature", 0.2))
                ),
                timeout=int(item.get("timeout", defaults.get("timeout", 120))),
                max_retries=int(
                    item.get("max_retries", defaults.get("max_retries", 3))
                ),
                prices=_parse_prices(item),
                docs_url=(
                    str(item.get("docs_url")).strip()
                    if item.get("docs_url")
                    else None
                ),
            )
        )
    return out


def load_preset_files() -> List[PresetFile]:
    """读预设包目录下的全部 ``*.yaml``。

    容错原则：**坏文件只跳过、不报错**。预设包是随包分发的静态资源，
    读不出来不该让 keeper 起不来（更不该让用户看到一屏报错）。
    """
    files: List[PresetFile] = []
    if not PRESETS_DIR.is_dir():
        logger.info("未找到模型预设包目录：%s（跳过播种）", PRESETS_DIR)
        return files
    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 —— 坏文件不该拖垮整体
            logger.warning("模型预设文件读取失败（跳过）：%s：%s", path.name, e)
            continue
        if not isinstance(raw, dict):
            logger.warning("模型预设文件格式不对（跳过）：%s", path.name)
            continue
        provider = str(raw.get("provider") or path.stem).strip()
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            defaults = {}
        base_url = str(raw.get("base_url")).strip() if raw.get("base_url") else None
        models = _parse_models(
            raw.get("models"), provider, base_url, defaults
        )
        if not models:
            logger.warning("模型预设文件没有可用条目（跳过）：%s", path.name)
            continue
        files.append(
            PresetFile(
                provider=provider,
                label=str(raw.get("label") or provider).strip(),
                base_url=base_url,
                docs_url=str(raw.get("docs_url")).strip() if raw.get("docs_url") else None,
                defaults=defaults,
                models=models,
            )
        )
    return files


def load_presets() -> List[PresetModel]:
    """全部预设条目（跨文件汇总）。"""
    out: List[PresetModel] = []
    for f in load_preset_files():
        out.extend(f.models)
    return out


@dataclass
class PresetProvider:
    """一家服务商的预设（文件级信息 + 它支持的模型清单）。"""

    provider: str
    label: str = ""
    base_url: Optional[str] = None
    docs_url: Optional[str] = None
    models: List[PresetModel] = field(default_factory=list)


def load_providers() -> List[PresetProvider]:
    """按服务商分组的预设。

    新建模型向导用它渲染"选服务商 → 选模型"的两级选择：一个服务商一个文件，
    文件里的 models 就是它支持的模型及其配置（地址、上下文、价格）。
    """
    return [
        PresetProvider(
            provider=f.provider,
            label=f.label,
            base_url=f.base_url,
            docs_url=f.docs_url,
            models=f.models,
        )
        for f in load_preset_files()
    ]


def get_preset(preset_key: str) -> Optional[PresetModel]:
    for p in load_presets():
        if p.preset_key == preset_key:
            return p
    return None
