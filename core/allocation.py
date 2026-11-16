"""货款分摊（承接 look_sku.calculate_receipt_allocation）。

原脚本用「子集和 DP」在一个订单的多条出库记录里找一组，使其结算金额之和
逼近该订单的货款金额（缩放到整数，容差 ±1）。

本模块保留该算法思路，但：
  · 金额单位统一为「分」的 int（原为 float 缩放 1e6），不再有 1e-6 元的隐式容差
  · 分摊后强校验 sum(分摊) == 货款合计，不一致抛 AllocationMismatch（原为 exit()）
  · DP 组合数上限显式可配，避免大订单组合爆炸
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

import pandas as pd

from .config import ADVICE_CARRY, MATCHED, UNMATCHED
from .errors import AllocationMismatch
from .money import allocate_exact, from_fen, safe_int, sum_fen

# DP 参与组合的最大行数（超过则退化为按结算金额比例分摊）
MAX_DP_ROWS = 22


@dataclass
class AllocationResult:
    """一次分摊的结果与自检数据。"""

    frame: pd.DataFrame
    total_gross_fen: int
    total_allocated_fen: int
    diff_fen: int
    dp_used: int
    ratio_fallback: int
    strategies: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.strategies is None:
            self.strategies = {}

    @property
    def ok(self) -> bool:
        return self.diff_fen == 0

    def verify_or_raise(self, ctx_label: str = "") -> None:
        if self.diff_fen != 0:
            raise AllocationMismatch(
                f"货款分摊合计与账单合计不一致{('（' + ctx_label + '）') if ctx_label else ''}",
                detail={
                    "gross_yuan": float(from_fen(self.total_gross_fen)),
                    "allocated_yuan": float(from_fen(self.total_allocated_fen)),
                    "diff_yuan": float(from_fen(self.diff_fen)),
                    "hint": "请检查账单货款金额与出库单结算金额是否口径一致",
                },
            )


def _subset_sum_fen(target: int, weights: Sequence[int], cap: int = MAX_DP_ROWS) -> list[int] | None:
    """在 weights 中找一个子集，其和最接近 target（分）。返回各位置的分摊额列表。

    规模超过 cap 时返回 None，由调用方走比例分摊兜底。
    """
    n = len(weights)
    if n == 0:
        return None
    if n > cap:
        return None

    # dp[sum] = 达到该和所用的下标集合（用位掩码，n<=22 安全）
    dp: dict[int, int] = {0: 0}
    for i, w in enumerate(weights):
        wi = int(w or 0)
        if wi <= 0:
            continue
        for s, mask in list(dp.items()):
            ns = s + wi
            if ns > target and target > 0:
                continue
            if ns not in dp:
                dp[ns] = mask | (1 << i)

    best_sum: int | None = None
    best_mask = 0
    for s, mask in dp.items():
        if best_sum is None or abs(s - target) < abs(best_sum - target):
            best_sum, best_mask = s, mask

    if best_sum is None:
        return None

    out = [0] * n
    for i in range(n):
        if best_mask & (1 << i):
            out[i] = int(weights[i] or 0)
    # 子集和与 target 的残差补给子集里最后一个非零项，保证合计精确
    residual = int(target) - sum(out)
    if residual != 0:
        for i in reversed(range(n)):
            if out[i] > 0:
                out[i] += residual
                break
        else:
            out[0] += residual
    return out


def allocate_group(group: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """对单个订单分组做货款分摊（严格对齐原 look_sku.calculate_receipt_allocation）。

    原口径的五段判定，顺序敏感：
      1. 未匹配 → 组内每行 分摊 = 货款金额（未匹配组通常单行；补录行货款为空 → 0）
      2. 货款金额为 NaN（补录行）→ 组内全 0
      3. 结算金额 > 0 的行为有效行；无有效行时：结算合计为 0 → 全额落首行，否则全 0
      4. 有效行结算合计 == 货款金额 → 各行分摊 = 各自结算金额（最常见路径）
      5. 否则：单行结算恰等于货款 → 该行拿全额、其余 0
              否则：DP 子集和命中 → 子集内各行拿自己的结算额、其余 0
              否则：按结算额比例分摊，余数补最后一行

    返回 (frame, 所用策略名)，策略名供上层统计与排查。

    注意「重复行货款置 0」不在这里做：原脚本是在分摊**之后**才对组内重复订单号
    mask 货款金额（见 allocate 末尾）。提前置 0 会让 iloc[0] 取到 0，
    导致同一订单的整组分摊塌成 0（2026-08 DY023 实测差 55 元 / 1 行）。
    """
    out = group.copy()
    if out.empty:
        out["货款分摊金额"] = []
        return out, "empty"

    match_state = str(out["巨益单据匹配状态"].iloc[0] or "")
    raw_gross = out["货款金额"].iloc[0]

    if match_state == UNMATCHED:
        amounts = [safe_int(v) for v in out["货款金额"].tolist()]
        out["货款分摊金额"] = amounts
        return out, "unmatched_passthrough"

    if _is_nan(raw_gross):
        out["货款分摊金额"] = 0
        return out, "gross_nan"

    gross = safe_int(raw_gross)
    settle = [safe_int(v) for v in out["结算金额"].tolist()]
    valid_idx = [i for i, s in enumerate(settle) if s > 0]

    if not valid_idx:
        # 原脚本：无有效行时，若结算合计为 0 → 全额落首行；否则整组 0
        if sum(settle) == 0:
            amounts = [0] * len(out)
            amounts[0] = gross
            out["货款分摊金额"] = amounts
            return out, "no_valid_first_row"
        out["货款分摊金额"] = 0
        return out, "no_valid_zero"

    valid_total = sum(settle[i] for i in valid_idx)
    amounts = [0] * len(out)

    if valid_total == gross:
        for i in valid_idx:
            amounts[i] = settle[i]
        return out.assign(货款分摊金额=amounts), "settle_equal"

    single = [i for i in valid_idx if settle[i] == gross]
    if single:
        amounts[single[0]] = gross
        return out.assign(货款分摊金额=amounts), "single_match"

    combo = _subset_sum_exact(gross, settle, valid_idx)
    if combo:
        for i in combo:
            amounts[i] = settle[i]
        return out.assign(货款分摊金额=amounts), "subset_sum"

    amounts = _ratio_allocate(gross, [settle[i] for i in valid_idx])
    full = [0] * len(out)
    for pos, i in enumerate(valid_idx):
        full[i] = amounts[pos]
    return out.assign(货款分摊金额=full), "ratio"


def _subset_sum_exact(target: int, settle: list[int], valid_idx: list[int],
                      cap: int = MAX_DP_ROWS) -> list[int] | None:
    """在有效行的结算额里找一个子集，其和**恰好**等于 target（分）。

    原 find_combination 的容差是 ±1（1e-6 元缩放），在分单位下等价于精确相等。
    只返回精确解；找不到返回 None，由比例分摊兜底（原脚本同）。
    """
    if target <= 0 or len(valid_idx) > cap:
        return None
    dp: dict[int, int] = {0: 0}
    for pos, i in enumerate(valid_idx):
        w = int(settle[i] or 0)
        if w <= 0 or w > target:
            continue
        for s, mask in list(dp.items()):
            ns = s + w
            if ns > target or ns in dp:
                continue
            dp[ns] = mask | (1 << pos)
    mask = dp.get(int(target))
    if mask is None:
        return None
    return [i for pos, i in enumerate(valid_idx) if mask & (1 << pos)]


def _ratio_allocate(gross: int, weights: list[int]) -> list[int]:
    """按结算额比例分摊（精确复刻原脚本 ratio 兜底分支）。

    原算法：前 n-1 行各自 round(结算额 × ratio, 2)，最后一行拿「货款 - 已分摊」的补偿值。
    不是 allocate_exact 的「截断 + 余数补给最后一个非零权重项」——两者在
    中间行四舍五入进位时会差 1 分。2026-08 DY023 实测（货款 138.45，
    结算 89.00 / 139.00）：
      allocate_exact → [5405, 8440]
      原脚本 ratio   → [5404, 8441]   ← 基准导出的是这个
    按原算法复刻后与基准逐分一致。

    注意原脚本的 round() 是 Python 银行家舍入（round-half-even），
    而项目金额纪律用 ROUND_HALF_UP。此处按纪律统一 ROUND_HALF_UP，
    与基准的差异只可能出现在「恰好半分」的极端值上，且总额恒等。
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [int(gross)]

    total_w = sum(int(w) for w in weights)
    if total_w <= 0:
        return allocate_exact(gross, [1] * n)

    out: list[int] = []
    acc = 0
    for i, w in enumerate(weights):
        if i < n - 1:
            part = int(
                (Decimal(int(gross)) * Decimal(int(w)) / Decimal(total_w))
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            out.append(part)
            acc += part
        else:
            out.append(int(gross) - acc)   # 尾行补偿，保证合计精确等于货款
    return out


def _is_nan(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    return False


def allocate(df: pd.DataFrame, *, group_col: str = "订单编号") -> AllocationResult:
    """全表分摊 + 自检 + 重复订单号货款置 0（严格对齐原 calculate_receipt_allocation）。

    输入 df 需含：订单编号 / 货款金额（分或 None） / 结算金额（分） / 巨益单据匹配状态
    输出：同结构 + 货款分摊金额（分）；货款金额已对组内重复行 mask 为 0。

    分组键必须包含「数据来源」：秒退订单会同时出现在收款侧与退款侧，
    只按订单编号分组会把两侧并成一组，导致只取首行货款金额、另一侧分摊丢失。
    """
    if df is None or df.empty:
        empty = df.copy() if df is not None else pd.DataFrame()
        if empty is not None and not empty.empty:
            empty["货款分摊金额"] = 0
        return AllocationResult(
            frame=empty if empty is not None else pd.DataFrame(),
            total_gross_fen=0, total_allocated_fen=0, diff_fen=0,
            dp_used=0, ratio_fallback=0,
        )

    group_keys = [group_col]
    if "数据来源" in df.columns:
        group_keys = ["数据来源", group_col]

    parts: list[pd.DataFrame] = []
    strategies: dict[str, int] = {}
    dp_used = 0
    ratio_fallback = 0
    for _, group in df.groupby(group_keys, sort=False, dropna=False):
        g, strategy = allocate_group(group)
        strategies[strategy] = strategies.get(strategy, 0) + 1
        if strategy == "subset_sum":
            dp_used += 1
        elif strategy == "ratio":
            ratio_fallback += 1
        parts.append(g)

    res = pd.concat(parts, ignore_index=False).sort_index() if parts else df.copy()
    if "货款分摊金额" not in res.columns:
        res["货款分摊金额"] = 0

    # 分摊完成后再把组内重复订单号的货款金额置 0（原脚本 final_df 的 transform mask）。
    # 一步 join 结果里同一订单被拆成多行，货款只能计一次。
    res["货款金额"] = (
        res.groupby(group_keys, sort=False)["货款金额"]
        .transform(lambda s: s.mask(s.duplicated(), 0))
    )

    res["货款分摊金额"] = res["货款分摊金额"].astype("int64")

    total_gross = sum_fen(res["货款金额"].tolist())
    total_alloc = sum_fen(res["货款分摊金额"].tolist())
    return AllocationResult(
        frame=res,
        total_gross_fen=int(total_gross),
        total_allocated_fen=int(total_alloc),
        diff_fen=int(total_gross - total_alloc),
        dp_used=dp_used,
        ratio_fallback=ratio_fallback,
        strategies=strategies,
    )


def verify_direction(
    res_receipt: pd.DataFrame, res_refund: pd.DataFrame
) -> dict[str, int]:
    """收款侧与退款侧分别做分摊自检（原脚本在 exit() 前做的两次校验）。"""
    out: dict[str, int] = {}
    for name, df in (("receipt", res_receipt), ("refund", res_refund)):
        if df is None or df.empty:
            out[f"{name}_diff_fen"] = 0
            continue
        gross = sum_fen(df["货款金额"].tolist())
        alloc = sum_fen(df.get("货款分摊金额", pd.Series(dtype="int64")).tolist())
        out[f"{name}_diff_fen"] = int(gross - alloc)
    return out
