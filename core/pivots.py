"""透视汇总（承接 look_sku.result 的店铺/款号透视段 + cost_match.final_pivot）。

三张透视：
  · 店铺数据透视：按 店铺名称 + 处理意见 汇总货款分摊、结算金额、成本、毛利
  · 款号数据透视：按 店铺 + 款号 + 处理意见 汇总（含吊牌价维度）
  · 匹配透视：按 店铺 汇总收款的货款分摊金额（原 comine_pivot）

毛利口径（原脚本）：
  · 账单结算口径毛利 = 货款分摊金额 - 发货成本；毛利率 = 毛利 / 货款分摊金额
  · 订单结算口径毛利 = 结算金额 - 发货成本；毛利率 = 毛利 / 结算金额
  · 毛利率以基点（bp）存储，1bp = 0.01%，避免浮点比率漂移

氚云导入模板（cost_match.chuanyun）：店铺名称 / 业务类别 / 资金方向 / 金额
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import numpy as np
import pandas as pd

from .config import SRC_RECEIPT, SRC_REFUND
from .money import bp_to_pct, bp_to_rate, fen_to_float, from_fen, gross_bp, safe_int

# 店铺透视列（原 shop_pivot_col + 店铺编码）
SHOP_PIVOT_COLUMNS: tuple[str, ...] = (
    "店铺名称", "店铺编码", "处理意见", "货款分摊金额", "订单结算金额", "数量",
    "发货成本", "标准成本", "账单结算口径毛利", "账单结算口径毛利率",
    "订单结算口径毛利", "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)

# 款号透视列（原 product_pivot_col + 店铺编码）
PRODUCT_PIVOT_COLUMNS: tuple[str, ...] = (
    "店铺名称", "店铺编码", "处理意见", "款号", "品牌", "年份", "季节", "吊牌价",
    "货款分摊金额", "订单结算金额", "数量", "发货成本", "标准成本",
    "账单结算口径毛利", "账单结算口径毛利率", "订单结算口径毛利", "订单结算口径毛利率",
    "冲发出商品数量", "冲发出商品",
)

# 匹配透视列（原 look_sku unicolumns L1562/L2042：店铺编码第2列，末列数据来源）
MATCH_PIVOT_COLUMNS: tuple[str, ...] = (
    "店铺名称", "店铺编码", "款号", "处理意见", "品类", "年份", "季节", "品牌",
    "吊牌价", "数量", "货款分摊金额", "结算金额", "标准成本", "发货成本", "数据来源",
)

# SKU 对账阶段（不带成本表）透视分组键里的缺失占位。
# 原脚本 look_sku L1569/L1602/L2238/L2268：pivot 前对分组键 fillna("未知")——
# 只填 NaN/None，**不填空串**；空串保留为独立分组（基准 Excel 里是空白单元格）。
PIVOT_NA_LABEL = "未知"
_MATCH_GROUP_KEYS = ("店铺名称", "款号", "处理意见", "品类", "年份", "季节", "品牌", "吊牌价")

# 内部基点列：不入基准 Excel（exporters 按 SHOP/PRODUCT_SHEET_COLUMNS 裁掉），
# 但必须留在 df 里传到 pivot_to_db_rows 填 gross_*_bp。
# 2026-11-14 浏览器实测：这两列被 .loc[:, 基准列] 裁掉 → 入库 bp 恒 NULL →
# 管理页店铺/款号透视的「账单毛利率 / 订单毛利率」全空（款号 23964 行、店铺 110 行），
# 而毛利额有值（23571 / 101 行）。回归脚本的 gcols 没放毛利率列，所以三层全绿也没报。
BP_COLUMNS: tuple[str, ...] = ("账单结算口径毛利率bp", "订单结算口径毛利率bp")

# 氚云导入模板列（原 cost_match.chuanyun）
CHUANYUN_COLUMNS: tuple[str, ...] = ("店铺名称", "业务类别", "资金方向", "金额")

OFFSET_SHIP_ADVICE = "冲发出商品"
OFFSET_COST_ADVICE = "冲成本"
CARRY_ADVICE = "结转成本"

# 收款侧合并标签固定「结转成本」，退款侧合并标签固定「冲成本/冲发出商品」
# （原 cost_match 第 467 / 576 行硬编码，不是按明细聚合出来的）
RECEIPT_ADVICE_LABEL = "结转成本"
REFUND_ADVICE_LABEL = "冲成本/冲发出商品"

# 增值税率：不含税口径下毛利 = 金额 / 1.13 - 发货成本（原 cost_match 字面量 1.13）
VAT_DIVISOR = Decimal("1.13")

# 毛利率基点换算（基准 round(…, 4) → 1bp = 0.0001）
BP_PER_UNIT = Decimal("10000")


def _safe_group(df: pd.DataFrame, index: list[str], *, money_cols=None,
                agg_map: dict[str, str] | None = None) -> pd.DataFrame:
    """按 index 分组求和；缺列补 0，index 列缺失则返回空表。

    money_cols 里的列量化到分（int），其余数值列保持原样。
    agg_map 可自定义「输出列 → 源列」。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    keys = [c for c in index if c in df.columns]
    if not keys:
        return pd.DataFrame()
    out = df.copy()
    if agg_map is None:
        agg_map = {
            "货款分摊金额": "货款分摊金额",
            "订单结算金额": "结算金额",
            "数量": "数量",
            "发货成本": "发货成本",
            "标准成本": "标准成本",
        }
    if money_cols is None:
        money_cols = ("货款分摊金额", "订单结算金额", "发货成本", "标准成本")

    for src in agg_map.values():
        if src not in out.columns:
            out[src] = 0
        out[src] = pd.to_numeric(out[src], errors="coerce").fillna(0)

    agg = {dst: (src, "sum") for dst, src in agg_map.items()}
    res = out.groupby(keys, as_index=False, dropna=False).agg(**agg)
    for c in money_cols:
        if c in res.columns:
            res[c] = res[c].map(lambda v: int(round(float(v))))
    if "数量" in res.columns:
        res["数量"] = res["数量"].astype("int64")
    return res


def _add_margin(g: pd.DataFrame, *, include_tax: bool = False,
                stage: str = "cost") -> pd.DataFrame:
    """补毛利与毛利率。两个阶段口径**不同**，stage 区分：

    stage="match"（SKU 对账·不带成本表，look_sku.result L1709/L1898、B2B L2336/L2437）：
      毛利 = 收入 - **标准成本**（不除税）；率 = round(毛利/收入, 4)。
      此阶段发货成本恒 0，毛利扣的是标准成本——不是发货成本。
    stage="cost"（成本匹配阶段，cost_match.py 毛利段）：
      含税：毛利 = 货款分摊金额 - 发货成本；率 = 毛利 / 货款分摊金额
      不含税（默认）：毛利 = round(货款分摊金额 / 1.13 - 发货成本, 2)
      率 = round(毛利 / (货款分摊金额 / 1.13), 4)
    订单结算口径同理，把货款分摊金额换成订单结算金额。

    金额内部一律「分」的 int：/1.13 与 round(,2) 都在 Decimal 元域做，
    再 quantize 回分；毛利率按基点存（基准 round 4 位小数 = 1bp）。
    收入为 0 时毛利率为 0（基准 np.where 的取值），UI 侧另行判断是否显示 '-'。
    """
    out = g.copy()
    for c in ("货款分摊金额", "订单结算金额", "发货成本", "标准成本", "数量"):
        if c not in out.columns:
            out[c] = 0

    bill = [int(v) for v in out["货款分摊金额"].tolist()]
    order = [int(v) for v in out["订单结算金额"].tolist()]
    ship = [int(v) for v in out["发货成本"].tolist()]
    std = [int(v) for v in out["标准成本"].tolist()]

    if stage == "match":
        # SKU 对账阶段（look_sku.result L1709/L1898、B2B L2336/L2437）：
        # 毛利 = 收入 - **标准成本**，不除税；此阶段发货成本恒 0。
        # 2026-11-17 影刀10月基准实测：HYK 结转毛利率 0.4503，旧口径（扣发货成本+除税）恒 1.0。
        out["账单结算口径毛利"] = [b - s for b, s in zip(bill, std)]
        out["订单结算口径毛利"] = [o - s for o, s in zip(order, std)]
        out["账单结算口径毛利率bp"] = [_rate_bp(bill[i], std[i], bill[i], taxed=True)
                                  for i in range(len(bill))]
        out["订单结算口径毛利率bp"] = [_rate_bp(order[i], std[i], order[i], taxed=True)
                                  for i in range(len(order))]
    elif include_tax:
        out["账单结算口径毛利"] = [b - s for b, s in zip(bill, ship)]
        out["订单结算口径毛利"] = [o - s for o, s in zip(order, ship)]
        out["账单结算口径毛利率bp"] = [_rate_bp(bill[i], ship[i], bill[i], taxed=True)
                                  for i in range(len(bill))]
        out["订单结算口径毛利率bp"] = [_rate_bp(order[i], ship[i], order[i], taxed=True)
                                  for i in range(len(order))]
    else:
        out["账单结算口径毛利"] = [_margin_untaxed_fen(bill[i], ship[i]) for i in range(len(bill))]
        out["订单结算口径毛利"] = [_margin_untaxed_fen(order[i], ship[i]) for i in range(len(order))]
        out["账单结算口径毛利率bp"] = [_rate_bp(bill[i], ship[i], bill[i], taxed=False)
                                  for i in range(len(bill))]
        out["订单结算口径毛利率bp"] = [_rate_bp(order[i], ship[i], order[i], taxed=False)
                                  for i in range(len(order))]

    # 基准同名列存的是「比率」round 4 位（TM007 店铺透视 = 0.6871），不是百分数。
    # 2026-11-14 浏览器实测 + 对照基准 Excel：这里曾用 bp_to_pct 写成 68.71，
    # 导出后财务按列对照会看到毛利率暴涨 100 倍。UI 侧的百分数由 repo.query_pivot
    # 从 bp 另算（gross_bill_pct），与基准列互不干扰。
    out["账单结算口径毛利率"] = [bp_to_rate(v) for v in out["账单结算口径毛利率bp"]]
    out["订单结算口径毛利率"] = [bp_to_rate(v) for v in out["订单结算口径毛利率bp"]]
    return out


def _excl_tax_fen(revenue_fen: int) -> Decimal:
    """收入（分）→ 不含税收入（元，未量化）。"""
    return Decimal(int(revenue_fen)) / Decimal(100) / VAT_DIVISOR


def _margin_untaxed_fen(revenue_fen: int, ship_cost_fen: int) -> int:
    """不含税毛利（分）= round(收入/1.13 - 发货成本, 2 元)。"""
    yuan = (_excl_tax_fen(revenue_fen) - from_fen(ship_cost_fen)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return int((yuan * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _rate_bp(revenue_fen: int, ship_cost_fen: int, base_fen: int, *, taxed: bool) -> int | None:
    """毛利率基点（基准 round 4 位小数）。分母为 0 → 0bp（原 np.where 分支）。"""
    if taxed:
        excl = from_fen(revenue_fen)
        margin = from_fen(revenue_fen - ship_cost_fen)
    else:
        excl = _excl_tax_fen(revenue_fen)
        margin = (excl - from_fen(ship_cost_fen)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not excl:
        return 0
    return int(((margin / excl) * BP_PER_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _dict_sum(df: pd.DataFrame, keys: list[str], value_col: str) -> dict:
    """按 keys 聚合 value_col → 字典（原 cost_match 里大量 dict(zip(...)) 的等价写法）。"""
    if df is None or df.empty or value_col not in df.columns:
        return {}
    present = [k for k in keys if k in df.columns]
    if not present:
        return {}
    d = df.copy()
    d[value_col] = pd.to_numeric(d[value_col], errors="coerce").fillna(0)
    g = d.groupby(present, as_index=False, dropna=False)[value_col].sum()
    out: dict = {}
    for _, r in g.iterrows():
        k = tuple(str(r[c]) for c in present)
        out[k if len(k) > 1 else k[0]] = int(round(float(r[value_col])))
    return out


def _negate_cols(df: pd.DataFrame, cols) -> pd.DataFrame:
    """把指定列取负（原 cost_match 退款侧透视：数量/金额一律取负）。"""
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = [-int(v) for v in pd.to_numeric(out[c], errors="coerce").fillna(0)]
    return out


def build_match_pivot(detail: pd.DataFrame, *, mutate_detail: bool = False,
                      stage: str = "match") -> pd.DataFrame:
    """匹配透视 df_pivot（原 look_sku.result L1559-1629 / B2B L2233-2293）。

    收款侧：数量取 abs，其余金额原样。
    退款侧：数量 / 货款分摊金额 / 结算金额 / 发货成本 / 标准成本 **全部取负**。
    index = 店铺名称 + 款号 + 处理意见 + 品类 + 年份 + 季节 + 品牌 + 吊牌价。

    分组键缺失语义（2026-11-17 影刀10月基准实测）：
      · 款号 / 吊牌价：保持 NaN（明细空白；pivot_table dropna=False 保留为空白组）。
      · 其余键（品类/年份/季节/品牌等）：NaN → 「未知」。
      · B2C（mutate_detail=True）：原脚本 L1569/L1602 的 fillna("未知") **就地改写
        res_receipt/res_pay**（无 .copy()），故明细 sheet 的品类/品牌也显示「未知」；
        这里同步改写传入的 detail。B2B 明细在 pivot 前已 reset_index 独立成表，
        原脚本 fillna 不影响导出明细，故 mutate_detail=False。
    """
    if detail is None or detail.empty:
        return pd.DataFrame(columns=list(MATCH_PIVOT_COLUMNS))

    index = list(_MATCH_GROUP_KEYS)
    _KEEP_NA_KEYS = {"款号", "吊牌价"}
    money = ("货款分摊金额", "结算金额", "发货成本", "标准成本")
    parts = []
    for src_name, negate in ((SRC_RECEIPT, False), (SRC_REFUND, True)):
        if "数据来源" in detail.columns:
            mask = detail["数据来源"] == src_name
            sub = detail[mask]
        else:
            mask = None
            sub = detail
        if sub is None or sub.empty:
            continue
        sub = sub.copy()
        for c in index:
            if c not in sub.columns or c in _KEEP_NA_KEYS:
                continue
            # cost 阶段：占位符已在 apply_cost_stage 重写（无款号/无吊牌价/未知/
            # 无年份/无季节；VIP 品类='None'），**不得**再 fillna("未知") 覆盖
            # （A3：分组键已变，必须按重写后键集合透视）。
            if stage == "cost":
                continue
            # B2B 品类/季节特例：原脚本 L2185/L2190 经 .astype(str)/.str 链后列变
            # object dtype，fillna("未知") 失效 → 基准透视里是空白
            # （AKC 品类 52 行、VIP 季节 18 行实测）。
            # B2C 这两列保留真 NaN → fillna 生效 →「未知」（HYK 47 行实测）。
            if not mutate_detail and c in ("品类", "季节"):
                sub[c] = sub[c].map(lambda v: None if (v is None or (isinstance(v, float) and v != v)) else str(v).strip())
                continue
            filled = sub[c].where(sub[c].notna(), PIVOT_NA_LABEL)
            sub[c] = filled
            if mutate_detail and mask is not None:
                detail.loc[mask, c] = filled.values
        g = _safe_group(sub, index, agg_map={
            "货款分摊金额": "货款分摊金额", "结算金额": "结算金额", "数量": "数量",
            "发货成本": "发货成本", "标准成本": "标准成本",
        }, money_cols=money)
        if g.empty:
            continue
        if "数量" in g.columns:
            g["数量"] = [abs(int(v)) for v in pd.to_numeric(g["数量"], errors="coerce").fillna(0)]
        if negate:
            g = _negate_cols(g, ("数量",) + money)
        g["数据来源"] = src_name
        # 店铺编码：B2C 单店恒等（原 L1585/L1617 直接赋 shop_code）；
        # B2B 多店一簿按分组键带出（原 L2241 把店铺编码放进 pivot index）。
        # 这里统一从明细取该侧首个非空编码；多店时逐行回填。
        if "店铺编码" in sub.columns:
            code_map = (
                sub.assign(_k=sub["店铺名称"].astype("string").fillna(""))
                .drop_duplicates("_k")
                .set_index("_k")["店铺编码"].astype("string").fillna("").to_dict()
            )
            g["店铺编码"] = g["店铺名称"].astype("string").fillna("").map(code_map).fillna("")
        else:
            g["店铺编码"] = ""
        parts.append(g)

    if not parts:
        return pd.DataFrame(columns=list(MATCH_PIVOT_COLUMNS))
    out = pd.concat(parts, ignore_index=True)
    for c in MATCH_PIVOT_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    return out.loc[:, list(MATCH_PIVOT_COLUMNS)]


def _assemble_pivot(
    piv: pd.DataFrame,
    *,
    level_keys: list[str],
    attr_keys: list[str],
    out_columns: tuple[str, ...],
    advice_label: str,
    carry_filter: tuple[str, ...],
    inv_filter: tuple[str, ...] | None,
    attr_source: pd.DataFrame | None = None,
    include_tax: bool = False,
    stage: str = "cost",
) -> pd.DataFrame:
    """按「先聚合金额、再按字典回填成本」口径拼一张透视。stage 区分两阶段：

    · 金额行：按 level_keys 聚合**全部**行的货款分摊金额（不过滤处理意见）
    · 订单结算金额 / 数量 / 标准成本：只聚合 carry_filter（收款=结转成本，
      退款=冲成本）命中的行
    · 发货成本：stage="match" 恒 0（look_sku L1706/L1806/L1895/L1989）；
      stage="cost" 聚合 carry_filter 行的发货成本
    · 冲发出商品数量 / 冲发出商品：只聚合 inv_filter（冲发出商品）命中的行；
      「冲发出商品」match 阶段取**标准成本**（look_sku L1787/L1977），
      cost 阶段取**发货成本**（cost_match 第 564/569 行）
    · 毛利：match 阶段 = 收入 - 标准成本（不除税）；cost 阶段见 _add_margin
    · 处理意见一律写死 advice_label（收款「结转成本」/ 退款「冲成本/冲发出商品」）
    · attr_keys（品牌/年份/季节/吊牌价）从 attr_source 按款号去重字典回填
    """
    empty = pd.DataFrame(columns=list(out_columns))
    if piv is None or piv.empty:
        return empty

    amt = _safe_group(piv, level_keys, agg_map={"货款分摊金额": "货款分摊金额"},
                      money_cols=("货款分摊金额",))
    if amt.empty:
        return empty

    advice = piv.get("处理意见", pd.Series(index=piv.index, dtype="object")).astype("string").fillna("")
    carry = piv[advice.isin(list(carry_filter))] if carry_filter else piv
    inv = piv[advice.isin(list(inv_filter))] if inv_filter else pd.DataFrame()

    carry_keys = [k for k in attr_keys if k in piv.columns]

    def _fill(dst: str, source: pd.DataFrame, col: str, keys: list[str]) -> None:
        d = _dict_sum(source, keys, col) if source is not None and not source.empty else {}
        amt[dst] = [_lookup(d, row, keys) for row in _key_tuples(amt, keys)]

    _fill("订单结算金额", carry, "结算金额", carry_keys)
    _fill("数量", carry, "数量", carry_keys)
    _fill("标准成本", carry, "标准成本", carry_keys)
    if stage == "match":
        # look_sku L1706/L1806/L1895/L1989：SKU 对账阶段发货成本恒 0
        amt["发货成本"] = 0
    else:
        _fill("发货成本", carry, "发货成本", carry_keys)

    if inv_filter:
        _fill("冲发出商品数量", inv, "数量", carry_keys)
        # match 阶段冲发出商品取标准成本（look_sku L1787/L1977）；
        # cost 阶段取发货成本（cost_match 第 564/569 行）
        _fill("冲发出商品", inv, "标准成本" if stage == "match" else "发货成本", carry_keys)
    else:
        amt["冲发出商品数量"] = 0
        amt["冲发出商品"] = 0

    amt["处理意见"] = advice_label

    # 品牌/年份/季节/吊牌价：按款号去重字典回填。
    # 原 pro_dict_* 建自 comine_pivot（build_match_pivot 输出，分组键已 fillna），
    # 不是原始明细——空款号组的属性是「未知/无年份/无季节/NaN」而非空串
    # （2026-11-17 HYK 基准实测）。attr_source 给定时用 piv 自身重建字典。
    dict_src = piv if (attr_source is not None and not attr_source.empty) else None
    if dict_src is not None and "款号" in amt.columns and "款号" in dict_src.columns:
        ds = dict_src.copy()
        ds["_k"] = ds["款号"].map(lambda v: "" if v is None or (isinstance(v, float) and v != v) else str(v))
        uniq = ds.drop_duplicates("_k").set_index("_k")
        for col in ("品牌", "年份", "季节", "吊牌价"):
            if col in uniq.columns:
                amt[col] = [
                    uniq[col].get("" if k is None or (isinstance(k, float) and k != k) else str(k))
                    for k in amt["款号"].tolist()
                ]
    for col in ("品牌", "年份", "季节", "吊牌价"):
        if col not in amt.columns:
            amt[col] = None
    if "吊牌价" in amt.columns:
        # NaN 保持空白（基准空款号组吊牌价空白），有值才量化到分。
        # cost 阶段占位文本（「无吊牌价」）原样保留——safe_int 会把它压成 0。
        def _tag_val(v):
            if v is None or (isinstance(v, float) and v != v) or v == "":
                return None
            if isinstance(v, str):
                s = v.strip()
                if not s or s.lower() in ("nan", "none", "nat"):
                    return None
                try:
                    return safe_int(float(s))
                except ValueError:
                    return s
            return safe_int(v)
        amt["吊牌价"] = [_tag_val(v) for v in amt["吊牌价"].tolist()]

    amt = _add_margin(amt, include_tax=include_tax, stage=stage)
    for c in out_columns:
        if c not in amt.columns:
            amt[c] = ""
    # 内部基点列必须跟着往下走：入库靠它填 gross_*_bp，Excel 侧由 exporters
    # 按 SHOP/PRODUCT_SHEET_COLUMNS 严格裁列，不会漏给财务。
    return amt.loc[:, list(out_columns) + list(BP_COLUMNS)]


def _key_tuples(df: pd.DataFrame, keys: list[str]):
    present = [k for k in keys if k in df.columns]
    for _, r in df.iterrows():
        t = tuple(str(r[c]) for c in present)
        yield t if len(t) > 1 else (t[0] if t else "")


def _lookup(d: dict, key, keys: list[str]):
    """字典取值：单键直接取，多键取元组；缺失 → 0（原 .apply(get).fillna(0)）。"""
    if not d:
        return 0
    if len(keys) <= 1:
        k = key[0] if isinstance(key, tuple) and key else key
        return int(d.get(k, 0) or 0)
    return int(d.get(tuple(key), 0) or 0)


def pivot_shop(detail: pd.DataFrame, *, include_tax: bool = False,
               stage: str = "match", mutate_detail: bool = False) -> pd.DataFrame:
    """店铺数据透视。stage="match"（默认，SKU 对账阶段）对齐 look_sku.result
    L1838-2013 / B2B_match L2412-2500：发货成本恒 0、毛利=收入-标准成本（不除税）、
    冲发出商品取标准成本。stage="cost" 才是 cost_match 第 418-617 行口径。

    收款侧一行「结转成本」+ 退款侧一行「冲成本/冲发出商品」，共两行（每店铺）。
    2026-11-17 影刀10月基准实测：本阶段基准由 look_sku 生成，旧实现误用 cost 口径
    导致 HYK 毛利率恒 1.0、冲发出商品恒 0。
    mutate_detail=True（B2C）时同步把分组键 NaN→「未知」写回明细（原脚本就地 fillna）。
    """
    piv = build_match_pivot(detail, mutate_detail=mutate_detail, stage=stage)
    if piv.empty:
        return pd.DataFrame(columns=list(SHOP_PIVOT_COLUMNS))

    keys = ["店铺名称"]
    parts = []
    for src_name, label, carry, inv in (
        (SRC_RECEIPT, RECEIPT_ADVICE_LABEL, (CARRY_ADVICE,), None),
        (SRC_REFUND, REFUND_ADVICE_LABEL, (OFFSET_COST_ADVICE,), (OFFSET_SHIP_ADVICE,)),
    ):
        sub = piv[piv["数据来源"] == src_name] if "数据来源" in piv.columns else piv
        if sub is None or sub.empty:
            continue
        g = _assemble_pivot(
            sub, level_keys=keys, attr_keys=keys, out_columns=SHOP_PIVOT_COLUMNS,
            advice_label=label, carry_filter=carry, inv_filter=inv,
            include_tax=include_tax, stage=stage,
        )
        if g.empty:
            continue
        # 多店一簿（爱库存/唯品会）：按店铺名称逐行映射编码，不能取首个套全部
        # （2026-11-17 实测 AKC 可可尼/秋水伊人编码张冠李戴）。
        g["店铺编码"] = _shop_code_map(detail, src_name).reindex(
            g["店铺名称"].astype("string").fillna("")).fillna("").values
        g["数据来源"] = src_name
        parts.append(g)

    if not parts:
        return pd.DataFrame(columns=list(SHOP_PIVOT_COLUMNS))
    out = pd.concat(parts, ignore_index=True)
    for c in SHOP_PIVOT_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    return out.loc[:, list(SHOP_PIVOT_COLUMNS) + ["数据来源"] + list(BP_COLUMNS)]


def _shop_code_map(detail: pd.DataFrame, src_name: str = "") -> pd.Series:
    """店铺名称 → 店铺编码 映射（多店一簿逐店对应，不能取首个套全部）。

    2026-11-17 影刀10月基准实测：AKC 店铺透视可可尼=AKC004、秋水伊人=AKC003，
    旧 _shop_code_of 取该侧首个非空编码套给所有店铺 → 张冠李戴。
    src_name 为空 → 不按数据来源过滤。
    """
    if detail is None or detail.empty or "店铺编码" not in detail.columns:
        return pd.Series(dtype="object")
    sub = detail
    if src_name and "数据来源" in detail.columns:
        sub = detail[detail["数据来源"] == src_name]
    if sub.empty or "店铺名称" not in sub.columns:
        return pd.Series(dtype="object")
    tmp = sub.assign(
        _name=sub["店铺名称"].astype("string").fillna(""),
        _code=sub["店铺编码"].astype("string").fillna(""),
    )
    tmp = tmp[tmp["_code"] != ""].drop_duplicates("_name")
    return tmp.set_index("_name")["_code"]


def _shop_code_of(detail: pd.DataFrame, src_name: str = "") -> str:
    """单店场景取首个非空编码（兼容旧调用）；多店请用 _shop_code_map。"""
    m = _shop_code_map(detail, src_name)
    return str(m.iloc[0]) if len(m) else ""


def pivot_product(detail: pd.DataFrame, *, include_tax: bool = False,
                  stage: str = "match", mutate_detail: bool = False) -> pd.DataFrame:
    """款号数据透视。stage="match"（默认）对齐 look_sku.result L1638-1835 /
    B2B_match L2303-2409；stage="cost" 才是 cost_match 第 184-411 行口径。

    收款侧：货款分摊金额为 0 的款号行剔除（原 L1657/L2318）。
    退款侧：match 阶段按 标准成本/货款分摊/订单结算 全 0 剔除（原 L1827/L2401）；
    cost 阶段按 发货成本/货款分摊/订单结算 全 0 剔除（cost_match 第 403 行）。
    """
    piv = build_match_pivot(detail, mutate_detail=mutate_detail, stage=stage)
    if piv.empty:
        return pd.DataFrame(columns=list(PRODUCT_PIVOT_COLUMNS))

    keys = ["店铺名称", "款号"]
    attr = keys + ["品牌", "年份", "季节", "吊牌价"]
    parts = []

    rec = piv[piv["数据来源"] == SRC_RECEIPT] if "数据来源" in piv.columns else piv
    if rec is not None and not rec.empty:
        g = _assemble_pivot(
            rec, level_keys=keys, attr_keys=keys, out_columns=PRODUCT_PIVOT_COLUMNS,
            advice_label=RECEIPT_ADVICE_LABEL, carry_filter=(CARRY_ADVICE,), inv_filter=None,
            attr_source=detail, include_tax=include_tax, stage=stage,
        )
        if not g.empty:
            # 原脚本：款号收款透视删掉货款分摊金额为 0 的行
            g = g[pd.to_numeric(g["货款分摊金额"], errors="coerce").fillna(0) != 0]
        if not g.empty:
            g["数据来源"] = SRC_RECEIPT
            parts.append(g)

    ref = piv[piv["数据来源"] == SRC_REFUND] if "数据来源" in piv.columns else pd.DataFrame()
    if ref is not None and not ref.empty:
        g = _assemble_pivot(
            ref, level_keys=keys, attr_keys=keys, out_columns=PRODUCT_PIVOT_COLUMNS,
            advice_label=REFUND_ADVICE_LABEL, carry_filter=(OFFSET_COST_ADVICE,),
            inv_filter=(OFFSET_SHIP_ADVICE,), attr_source=detail, include_tax=include_tax,
            stage=stage,
        )
        if not g.empty:
            # match 阶段发货成本恒 0，零行判定用标准成本（look_sku L1829/L2403）
            zero_col = "标准成本" if stage == "match" else "发货成本"
            zero = (
                (pd.to_numeric(g[zero_col], errors="coerce").fillna(0) == 0)
                & (pd.to_numeric(g["货款分摊金额"], errors="coerce").fillna(0) == 0)
                & (pd.to_numeric(g["订单结算金额"], errors="coerce").fillna(0) == 0)
            )
            g = g[~zero]
        if not g.empty:
            g["数据来源"] = SRC_REFUND
            parts.append(g)

    if not parts:
        return pd.DataFrame(columns=list(PRODUCT_PIVOT_COLUMNS))
    out = pd.concat(parts, ignore_index=True)
    # 多店一簿逐店映射编码（收退两侧合并后按店铺名称回填）
    cmap = _shop_code_map(detail, "")
    out["店铺编码"] = cmap.reindex(out["店铺名称"].astype("string").fillna("")).fillna("").values
    for c in PRODUCT_PIVOT_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    return out.loc[:, list(PRODUCT_PIVOT_COLUMNS) + ["数据来源"] + list(BP_COLUMNS)]


def pivot_match(detail: pd.DataFrame, *, stage: str = "match") -> pd.DataFrame:
    """匹配透视（原 cost_match 的 df_pivot，14 列，金额已按收/退款定号）。

    stage="cost" 时分组键用 apply_cost_stage 重写后的占位符（A3），不再 fillna。
    列集裁切（14 列无店铺编码 vs match 15 列）由 exporters 按阶段常量执行。
    """
    return build_match_pivot(detail, stage=stage)


def chuanyun_template(detail: pd.DataFrame, *, period: str = "") -> pd.DataFrame:
    """氚云导入模板：店铺名称 / 业务类别 / 资金方向 / 金额（原 cost_match.chuanyun）。

    首期只导出 Excel 交人工上传（用户决策：氚云 API 二期再接）。
    """
    if detail is None or detail.empty:
        return pd.DataFrame(columns=list(CHUANYUN_COLUMNS))

    out = detail.copy()
    if "业务类别" not in out.columns:
        out["业务类别"] = "货款"

    rows = []
    for direction, col, label in (
        (SRC_RECEIPT, "收款", "收款"),
        (SRC_REFUND, "退款", "付款"),
    ):
        if "数据来源" in out.columns:
            sub = out[out["数据来源"] == direction]
        else:
            sub = out
        if sub.empty:
            continue
        if col not in sub.columns:
            continue
        g = sub.groupby(["店铺名称", "业务类别"], as_index=False, dropna=False)[col].sum()
        g = g[g[col] > 0]
        if g.empty:
            continue
        g = g.rename(columns={col: "金额"})
        g["资金方向"] = label
        rows.append(g)

    if not rows:
        return pd.DataFrame(columns=list(CHUANYUN_COLUMNS))
    combined = pd.concat(rows, ignore_index=True)
    for c in CHUANYUN_COLUMNS:
        if c not in combined.columns:
            combined[c] = ""
    return combined.loc[:, list(CHUANYUN_COLUMNS)]


def _safe_int(value: Any, default: int = 0) -> int:
    """安全转 int：None / NaN / NaT / 非数值串回落 default。

    不能用 int(v or 0)：float('nan') 是 truthy，or 不兜底，int(nan) 抛 ValueError。
    真实库里未匹配行没有吊牌价/成本，透视后就是 NaN。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        if value != value:  # NaN
            return default
        return int(value)
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _safe_text(value: Any) -> str:
    """安全转 str：None / NaN / NaT → 空串（不产生 'nan' 字面量）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _safe_bp(value: Any) -> int | None:
    """毛利率基点：None/NaN → None（UI 显示 '-'，不是 0%）。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def pivot_to_db_rows(
    kind: str,
    pivot: pd.DataFrame,
    *,
    batch_id: str,
    period: str,
    platform: str = "",
    direction: str = "",
    shop_code: str = "",
) -> list[dict]:
    """透视表 → SQLite 行（金额转分、毛利率转基点）。

    shop_code 是兜底：明细缺「店铺编码」列时 _shop_code_of 会返回空串，
    入库就成了 shop_code='' 的孤儿行。而 query_pivot 的「每店最新批次」子查询
    按 (period, shop_code) 分组，'' 会被当成一家独立店铺选中 → 管理页款号透视
    混进几千行脏数据（2026-11-14 实测 23964 行）。这里必须保证非空。
    """
    if pivot is None or pivot.empty:
        return []
    out: list[dict] = []
    for rec in pivot.to_dict(orient="records"):
        row: dict = {
            "batch_id": batch_id,
            "period": period,
            "platform": platform,
            "shop_code": _safe_text(rec.get("店铺编码")) or shop_code,
            "shop_name": _safe_text(rec.get("店铺名称")),
            "direction": _safe_text(rec.get("数据来源")) or direction,
            "advice": _safe_text(rec.get("处理意见")),
            "allocated_fen": _safe_int(rec.get("货款分摊金额")),
            "settle_fen": _safe_int(rec.get("订单结算金额")),
            "qty": _safe_int(rec.get("数量")),
            "ship_cost_fen": _safe_int(rec.get("发货成本")),
            "std_cost_fen": _safe_int(rec.get("标准成本")),
            "gross_bill_fen": _safe_int(rec.get("账单结算口径毛利")),
            "gross_order_fen": _safe_int(rec.get("订单结算口径毛利")),
            "offset_ship_qty": _safe_int(rec.get("冲发出商品数量")),
            "offset_ship_fen": _safe_int(rec.get("冲发出商品")),
        }
        row["gross_bill_bp"] = _safe_bp(rec.get("账单结算口径毛利率bp"))
        row["gross_order_bp"] = _safe_bp(rec.get("订单结算口径毛利率bp"))
        if kind == "product":
            row.update({
                "style_no": _safe_text(rec.get("款号")),
                "brand": _safe_text(rec.get("品牌")),
                "year_tag": _safe_text(rec.get("年份")),
                "season": _safe_text(rec.get("季节")),
                "tag_price_fen": _safe_int(rec.get("吊牌价")),
            })
        out.append(row)
    return out
