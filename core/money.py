"""金额口径：Decimal ↔ 分（INTEGER）。

原脚本全程 `float + round(2)`，分摊容差 1e-6 元，财务口径上会累积分位漂移。
本模块统一：
  · 计算用 Decimal（quantize 到 0.01）
  · 存储用「分」的 int（SQLite INTEGER，见 schema.sql 的 *_fen 列）
  · 展示用 Decimal 字符串或元 float（仅用于 UI，不回写计算）

分摊容差显式声明为 ALLOWED_TOLERANCE_FEN = 0（分位精确），
任何非零差异都由 AllocationMismatch 暴露，而不是悄悄吸收。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

import pandas as pd

CENT = Decimal("0.01")

# 分摊校验容差：0 分（必须完全相等）。原脚本容差 ±1（1e-6 元缩放后），此处收紧。
ALLOWED_TOLERANCE_FEN = 0

# 毛利率用基点存储（1bp = 0.01%），避免浮点比率
BP_SCALE = Decimal("10000")


def q2(value: Any) -> Decimal:
    """量化到分（两位小数，四舍五入）。None/空/非法 → Decimal('0.00')。"""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        d = value
    else:
        s = str(value).strip()
        if not s or s.lower() in ("nan", "none", "nat", "-"):
            return Decimal("0.00")
        s = s.replace(",", "").replace("￥", "").replace("¥", "")
        try:
            d = Decimal(s)
        except Exception:
            return Decimal("0.00")
        if d.is_nan() or d.is_infinite():
            return Decimal("0.00")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def to_fen(value: Any) -> int:
    """元 → 分（int）。"""
    return int((q2(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_fen(fen: Any) -> Decimal:
    """分 → 元（Decimal，两位）。"""
    try:
        n = int(fen or 0)
    except (TypeError, ValueError):
        n = 0
    return (Decimal(n) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def fen_to_float(fen: Any) -> float:
    """分 → 元 float。仅供 UI 展示与 JSON 序列化，禁止回写计算。"""
    return float(from_fen(fen))


def safe_int(value: Any, default: int = 0) -> int:
    """安全转 int：None / NaN / NaT / 非数值串回落 default。

    不能用 `int(v or 0)`：float('nan') 是 truthy，`or` 不兜底，int(nan) 抛 ValueError。
    真实库里未匹配行的结算金额/吊牌价就是 NaN（NULL）。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))
    if isinstance(value, float):
        if value != value:  # NaN
            return default
        return int(value)
    try:
        if pd.isna(value):  # numpy 标量 / NaT
            return default
    except (NameError, TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def sum_fen(values: Iterable[Any]) -> int:
    """求和（输入可为分 int 或元 Decimal/str，统一按分累加）。"""
    total = 0
    for v in values:
        if isinstance(v, Decimal):
            total += to_fen(v)
        else:
            total += safe_int(v)
    return total


def gross_bp(revenue_fen: int, cost_fen: int) -> int | None:
    """毛利率（基点）。收入为 0 时返回 None（UI 显示 '-'，不显示 0%）。

    毛利 = 收入 - 成本；毛利率 = 毛利 / 收入。
    """
    if not revenue_fen:
        return None
    gross = Decimal(revenue_fen - cost_fen)
    rate = (gross / Decimal(revenue_fen)) * BP_SCALE
    return int(rate.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def bp_to_pct(bp: int | None) -> float | None:
    """基点 → 百分比数值（1234bp → 12.34）。仅供 UI 展示，不写基准列。"""
    if bp is None:
        return None
    return round(bp / 100.0, 2)


def bp_to_rate(bp: int | None) -> float | None:
    """基点 → 基准比率（6871bp → 0.6871，round 4 位）。

    2026-11-14 浏览器实测：导出 Excel 的「账单结算口径毛利率」写成 68.71，
    而基准 TM007 店铺透视同列是 0.6871 —— 大 100 倍。财务按列对照会误判
    毛利率暴涨。基准 cost_match 用的是 round(毛利/收入, 4) 的**比率**，
    不是百分数；「毛利率按基点」只是内部存储纪律，落到基准列必须还原成比率。
    """
    if bp is None:
        return None
    return round(bp / 10000.0, 4)


def allocate_exact(total_fen: int, weights: list[int]) -> list[int]:
    """按权重精确分摊 total_fen，余数补给最后一个非零权重项。

    保证 sum(结果) == total_fen（分位精确），用于货款金额按 SKU 行分摊。
    weights 全为 0 时：均摊，余数补给首项。
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [int(total_fen)]

    total_w = sum(weights)
    if total_w <= 0:
        base, rem = divmod(int(total_fen), n)
        out = [base] * n
        out[0] += rem
        return out

    out: list[int] = []
    acc = 0
    last_idx = -1
    for i, w in enumerate(weights):
        if w > 0:
            last_idx = i
        part = int(Decimal(total_fen) * Decimal(w) / Decimal(total_w))
        out.append(part)
        acc += part
    diff = int(total_fen) - acc
    if diff != 0:
        target = last_idx if last_idx >= 0 else 0
        out[target] += diff
    return out
