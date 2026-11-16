"""平台适配器注册表。

新增平台的标准动作（原脚本要抄一个 600 行函数，这里只需三步）：
  1. 新建 core/platforms/{name}.py，继承 BillAdapter，填 platform/display_name
     与 default_read_options；差异逻辑写在 clean()/normalize()
  2. 在本文件 _ADAPTERS 注册
  3. 管理页「账单整理 · 平台规则」补 column_map / biz_code 规则（多数平台不用写代码）

列名结构与淘系/拼多多一致的平台，甚至可以只写规则表不写代码（用 GenericAdapter）。
"""
from __future__ import annotations

from typing import Any

from ..errors import BillParseError
from .base import BillAdapter, ParseContext
from .akc import AkcAdapter
from .douyin import DouyinAdapter
from .hyk import HykAdapter
from .jd import JdAdapter
from .kuaishou import KuaishouAdapter
from .pdd import PddAdapter
from .sph import SphAdapter
from .taobao import TaobaoAdapter
from .vip import VipAdapter
from .xhs import XhsAdapter
from .youzan import YouzanAdapter

_ADAPTERS: dict[str, type[BillAdapter]] = {
    TaobaoAdapter.platform: TaobaoAdapter,
    DouyinAdapter.platform: DouyinAdapter,
    JdAdapter.platform: JdAdapter,
    PddAdapter.platform: PddAdapter,
    # 第二批（2026-11-16，承接 bill_arr 对应 *_arr，8 月基准逐列对齐）
    KuaishouAdapter.platform: KuaishouAdapter,
    SphAdapter.platform: SphAdapter,
    YouzanAdapter.platform: YouzanAdapter,
    XhsAdapter.platform: XhsAdapter,
    HykAdapter.platform: HykAdapter,
    AkcAdapter.platform: AkcAdapter,
    VipAdapter.platform: VipAdapter,
}

# 中文名 → platform（管理页下拉与规则表双向查找）
DISPLAY_TO_PLATFORM: dict[str, str] = {}


class GenericAdapter(BillAdapter):
    """通用适配器：完全由规则表驱动（column_map + biz_code + read_option）。

    用于结构与已支持平台相近、只是列名不同的新平台，无需写代码。
    """

    platform = "generic"
    display_name = "通用（规则表驱动）"


def register(cls: type[BillAdapter]) -> type[BillAdapter]:
    _ADAPTERS[cls.platform] = cls
    return cls


def get_adapter(platform: str, rules: dict[str, dict[str, str]] | None = None) -> BillAdapter:
    """按 platform 取适配器实例（注入规则表）。"""
    key = str(platform or "").strip().lower()
    if not key:
        raise BillParseError("未指定平台", detail={"hint": "请选择账单平台"})

    cls = _ADAPTERS.get(key)
    if cls is None:
        # 未注册平台：若规则表里有 column_map，则用通用适配器兜底
        if rules and rules.get("column_map"):
            adapter = GenericAdapter(rules)
            adapter.platform = key
            adapter.display_name = key
            return adapter
        raise BillParseError(
            f"平台 {key} 尚未接入",
            detail={
                "available": sorted(_ADAPTERS.keys()),
                "hint": (
                    "可在「账单整理 · 平台规则」页配置 column_map / biz_code 规则，"
                    "由通用适配器接管；或新增 core/platforms/{platform}.py"
                ),
            },
        )
    return cls(rules)


def list_platforms() -> list[dict[str, Any]]:
    """管理页平台下拉数据。"""
    out: list[dict[str, Any]] = []
    for key, cls in _ADAPTERS.items():
        out.append(
            {
                "platform": key,
                "display_name": cls.display_name,
                "file_patterns": list(cls.file_patterns),
                "needs_extra_files": list(cls.needs_extra_files),
                "is_b2b": cls.is_b2b,
            }
        )
    return sorted(out, key=lambda x: x["display_name"])


def build_display_map() -> dict[str, str]:
    for key, cls in _ADAPTERS.items():
        DISPLAY_TO_PLATFORM[cls.display_name] = key
    return DISPLAY_TO_PLATFORM


build_display_map()


def parse_platform(value: str) -> str:
    """中文名或 platform 标识都接受，统一返回 platform 标识。"""
    v = str(value or "").strip()
    if not v:
        return ""
    if v.lower() in _ADAPTERS:
        return v.lower()
    return DISPLAY_TO_PLATFORM.get(v, v.lower())


__all__ = [
    "BillAdapter",
    "ParseContext",
    "GenericAdapter",
    "get_adapter",
    "list_platforms",
    "parse_platform",
    "register",
    "PLATFORMS",
]

PLATFORMS = _ADAPTERS
