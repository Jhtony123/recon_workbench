"""SKU 出入库匹配（承接 look_sku.result 的收款/退款匹配段 + 秒退判断）。

核心口径（从原脚本迁移，逐条保持）：
  1. 收款侧：账单货款收款 left join OMS 出库单（B2C配货单），按订单编号
     · 一对多时重复行货款金额置 0（防重复计金额）—— 原 duplicated('订单编号') 逻辑
     · 有单号 → 已匹配；无单号 → 未匹配
  2. 退款侧两段匹配：
     · 第一段 left join OMS 入库单（B2C退换货通知单）
     · 未匹配的第二段再 left join 退款单（退款单先按 单号+sku+结算金额 去重）
  3. 秒退判断：未匹配的收款订单，去退款侧汇总货款金额，金额完全相等 → 标「是」，
     并把这部分单独再撞退款单，避免误判为「无发货」
  4. 三分处理意见：
     · 冲发出商品：入库与出库能对上
     · 冲成本：入库无对应出库
     · 成本不做处理：出库未参与收款核对

金额一律「分」的 int（core.money）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import (
    ADVICE_CARRY,
    ADVICE_NO_ACTION,
    ADVICE_OFFSET_COST,
    ADVICE_OFFSET_SHIP,
    BIZ_GOODS,
    DOC_DELIVERY,
    DOC_REFUND,
    DOC_RETURN,
    MATCHED,
    SRC_RECEIPT,
    SRC_REFUND,
    UNMATCHED,
)
from .money import from_fen, safe_int, sum_fen, to_fen

# 匹配明细标准列（与 schema.sql ext_recon_workbench_match_detail 对齐）
DETAIL_COLUMNS: tuple[str, ...] = (
    "账单周期", "订单编号", "订单日期", "货款金额", "货款分摊金额",
    "id", "单据类型", "存货异动时间", "单号", "sku", "款号", "商品名称",
    "品类", "年份", "季节", "品牌", "吊牌价", "数量", "店铺名称",
    "结算金额", "退款类型", "退款状态", "标准成本单价", "标准成本",
    "采购单价", "发货成本", "巨益单据匹配状态", "历史对账记录",
    "对账周期", "是否秒退判断", "处理意见",
)

# detail 输出列：标准 32 列 + 数据来源 + 店铺编码。
# 「数据来源」必须保留：秒退订单会同时出现在收款侧与退款侧，
# 分摊（core.allocation）按它分组，透视与入库也靠它区分方向。
# 「店铺编码」必须保留：店铺/款号透视按它分组，未匹配行没有 OMS 侧字段会缺列。
DETAIL_OUTPUT_COLUMNS: tuple[str, ...] = DETAIL_COLUMNS + ("数据来源", "店铺编码")

# 账单标准五元结构（platforms 层产物）
BILL_COLUMNS: tuple[str, ...] = ("订单编号", "资金日期", "业务类别", "收款", "退款")

# 退款单去重键（原脚本：单号+sku+结算金额）
REFUND_DEDUP_KEYS = ("单号", "sku", "结算金额")


@dataclass
class MatchResult:
    """一次匹配的完整产物。"""

    receipt: pd.DataFrame
    refund: pd.DataFrame
    oms_matched: pd.DataFrame          # 已匹配的巨益单号（回传用）
    stats: dict = field(default_factory=dict)

    @property
    def detail(self) -> pd.DataFrame:
        """收退款明细合并（DETAIL_OUTPUT_COLUMNS 顺序，含数据来源）。"""
        parts = []
        if not self.receipt.empty:
            parts.append(self.receipt)
        if not self.refund.empty:
            parts.append(self.refund)
        if not parts:
            return pd.DataFrame(columns=list(DETAIL_OUTPUT_COLUMNS))
        out = pd.concat(parts, ignore_index=True)
        for c in DETAIL_OUTPUT_COLUMNS:
            if c not in out.columns:
                out[c] = ""
        return out.loc[:, list(DETAIL_OUTPUT_COLUMNS)]


def _empty_detail() -> pd.DataFrame:
    return pd.DataFrame(columns=list(DETAIL_OUTPUT_COLUMNS))


def split_bill(bill_df: pd.DataFrame, *, period: str = "") -> dict[str, pd.DataFrame]:
    """账单标准表 → 收款 / 退款 两张透视表（原 bill_arrange 的拆分与透视）。

    只取业务类别为「货款」的行；按订单编号透视汇总。
    """
    cols = ["订单编号", "资金日期", "货款金额"]
    if bill_df is None or bill_df.empty:
        empty = pd.DataFrame(columns=cols)
        return {"收款": empty, "退款": empty, "收款订单编号": []}

    df = bill_df.copy()
    if "业务类别" in df.columns:
        df = df[df["业务类别"] == BIZ_GOODS]

    # 金额列已是「分」的 int；缺列补 0
    for c in ("收款", "退款"):
        if c not in df.columns:
            df[c] = 0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")

    df["订单编号"] = df["订单编号"].astype("string").fillna("").str.strip()
    df.loc[df["订单编号"] == "", "订单编号"] = "无订单号"

    # 收款侧
    rec = df.loc[:, ["订单编号", "资金日期", "收款"]].rename(columns={"收款": "货款金额"})
    rec = rec[rec["货款金额"] != 0]
    if rec.empty:
        rec_pivot = pd.DataFrame(columns=cols)
    else:
        rec_pivot = (
            rec.groupby("订单编号", as_index=False)["货款金额"].sum()
        )
        rec_pivot = rec_pivot.loc[:, cols] if all(c in rec_pivot.columns for c in cols) else rec_pivot

    # 退款侧（原脚本退款金额取正数存「货款金额」）
    pay = df.loc[:, ["订单编号", "资金日期", "退款"]].rename(columns={"退款": "货款金额"})
    pay = pay[pay["货款金额"] != 0]
    if pay.empty:
        pay_pivot = pd.DataFrame(columns=cols)
    else:
        pay_pivot = pay.groupby("订单编号", as_index=False)["货款金额"].sum()

    if period:
        if not rec_pivot.empty:
            rec_pivot.insert(0, "账单周期", period)
        if not pay_pivot.empty:
            pay_pivot.insert(0, "账单周期", period)

    return {
        "收款": rec_pivot,
        "退款": pay_pivot,
        "收款订单编号": rec_pivot["订单编号"].tolist() if not rec_pivot.empty else [],
    }


def _dedup_refund_orders(df: pd.DataFrame) -> pd.DataFrame:
    """退款单按 单号+sku+结算金额 去重（原脚本同名逻辑）。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(df.columns) if df is not None else [])
    keys = [k for k in REFUND_DEDUP_KEYS if k in df.columns]
    if not keys:
        return df.copy()
    return df.drop_duplicates(subset=keys).copy()


def _stable_sort_oms(oms_df: pd.DataFrame) -> pd.DataFrame:
    """OMS 侧 join 前按稳定键排序，消除数据库返回顺序带来的不可复现。

    原脚本 oms_sql_select 的 SQL 没有 ORDER BY，同一订单多条出库行的先后
    取决于 ADB 的返回顺序；而 _mark_duplicates / single_match 都是「取第一行」，
    于是「哪个 sku 保留货款金额」在不同时刻跑会漂移，结果无法复现。
    本项目既有口径改进（同 core.db.fetch_order_dates 的显式 ORDER BY）：
    按 订单编号 → 单号 → sku → id 排序，让结果与运行时刻无关。

    2026-08 DY023 实测：订单 6955322015214016196 有 3 条出库行、其中两条结算额
    同为 86.64，未排序时货款金额落在 …01403，排序后落在 …02002。
    """
    out = oms_df.copy()
    keys = [c for c in ("订单编号", "单号", "sku", "id") if c in out.columns]
    if not keys:
        return out
    for c in keys:
        out[c] = out[c].astype("string").fillna("")
    return out.sort_values(keys, kind="mergesort").reset_index(drop=True)


def _mark_duplicates(frame: pd.DataFrame, col: str = "订单编号") -> pd.DataFrame:
    """一对多 join 后标记重复订单（只打标记，**不改货款金额**）。

    原脚本 merge 段确实写了「标记重复 → 货款金额置 0」，但真正的置 0 发生在
    calculate_receipt_allocation 末尾：分摊算完后才 groupby transform mask duplicated。
    若在 join 阶段就提前置 0，分摊时组内首行拿到的 gross 会是 0，且「哪一行保留
    货款金额」由 join 顺序而非分摊结果决定，与基准不符。

    2026-08 DY023 实测（订单 6955322015214016196，1 笔货款 3 条出库行）：
      基准 → 货款金额留在 sku …02003 行、分摊 86.64 落在 sku …02002 行
      插件（提前置 0）→ 两列都落在 …02002 行，7 行状态错位、数量差 1
    因此这里只保留 duplicated 标记，置 0 交由 allocation.allocate 在分摊后统一做。
    """
    out = frame.copy()
    if out.empty or col not in out.columns:
        return out
    out["标记重复"] = out.duplicated(subset=[col], keep="first")
    return out


def _preserve_oms_columns(frame: pd.DataFrame) -> None:
    """保留 OMS join 带进来的 历史对账记录 / 对账周期 真值（就地修改）。

    这两列是 finalize_advice 的**判定输入**，不是展示列，绝不能覆盖成空串：
      · 历史对账记录 == '未对账' 决定「分摊≤0 → 匹配状态回落未匹配」能否成立
      · 对账周期 决定 _compare_periods(账单周期, 对账周期) 的跨期识别

    2026-08 DY023 实测缺陷：join 后被写成空串 → 回落分支永不成立，2 行该判
    「未匹配 / 成本不做处理」的行留在「已匹配 / 结转成本」，店铺透视结转成本
    多算 280.36 元 / 2 件，并连锁把退款侧 2 行「冲发出商品」翻成「冲成本」。

    left join 未命中时这两列自然是 NaN，统一规整为空串（与 _is_blank 同义）。
    """
    for c in ("历史对账记录", "对账周期"):
        if c in frame.columns:
            frame[c] = frame[c].astype("string").fillna("")
        else:
            frame[c] = ""


def match_receipt(
    receipt: pd.DataFrame,
    oms_out: pd.DataFrame,
    *,
    period: str = "",
    shop_name: str = "",
    shop_code: str = "",
) -> pd.DataFrame:
    """收款侧匹配：账单货款收款 left join OMS 出库单（B2C配货单）。"""
    if receipt is None or receipt.empty:
        return _empty_detail()

    left = receipt.copy()
    left["订单编号"] = left["订单编号"].astype("string").fillna("").str.strip()

    if oms_out is None or oms_out.empty:
        merged = left.copy()
        for c in ("id", "单据类型", "存货异动时间", "单号", "sku", "款号", "商品名称",
                  "品类", "年份", "季节", "品牌", "吊牌价", "数量", "店铺名称",
                  "结算金额", "退款类型", "退款状态", "订单日期"):
            merged[c] = ""
    else:
        right = _stable_sort_oms(oms_out)
        right["订单编号"] = right["订单编号"].astype("string").fillna("").str.strip()
        merged = pd.merge(left, right, how="left", on="订单编号", suffixes=("", "_oms"))

    merged = _mark_duplicates(merged, "订单编号")
    merged["巨益单据匹配状态"] = np.where(
        merged["单号"].notna() & (merged["单号"].astype("string").fillna("") != ""),
        MATCHED, UNMATCHED,
    )

    # 结算金额转分（ADB 回来是元）
    if "结算金额" in merged.columns:
        merged["结算金额"] = merged["结算金额"].map(lambda v: to_fen(v) if not _is_blank(v) else 0)
    else:
        merged["结算金额"] = 0

    merged["采购单价"] = 0
    merged["发货成本"] = 0
    merged["标准成本单价"] = 0
    merged["标准成本"] = 0
    merged["数据来源"] = SRC_RECEIPT
    merged["账单周期"] = period
    merged["店铺名称"] = shop_name or merged.get("店铺名称", "")
    # 店铺编码必须补齐：未匹配行没有 OMS 侧字段，缺列会让透视 KeyError
    if shop_code:
        merged["店铺编码"] = shop_code
    elif "店铺编码" not in merged.columns:
        merged["店铺编码"] = ""
    # 原脚本 L664/L794：初始空串；已匹配行强制空串，未匹配行由 mark_instant_refund
    # 判「是」或留空。不写「否」——基准里该列只有 空/是 两种取值（2026-11-17 实测）。
    merged["是否秒退判断"] = ""
    merged["处理意见"] = ADVICE_CARRY
    # 历史对账记录 / 对账周期 来自 OMS join，是 finalize_advice 的**判定输入**，
    # 绝不能覆盖成空串（2026-08 DY023 实测缺陷）：
    #   · 回落分支 (分摊≤0 & 历史对账记录=='未对账') 永不成立 → 该判「未匹配」的行留在「已匹配」
    #   · _compare_periods(账单周期, 对账周期) 拿不到对账周期 → 跨期单据无法识别
    # 2026-08 DY023 因此把 2 行错判成「结转成本」，店铺透视结转成本多 280.36 元 / 2 件。
    _preserve_oms_columns(merged)
    merged["退款类型"] = merged.get("退款类型", "")
    merged["退款状态"] = merged.get("退款状态", "")
    merged["货款分摊金额"] = merged["货款金额"]
    if "订单日期" not in merged.columns:
        merged["订单日期"] = ""

    return merged


def match_refund(
    refund: pd.DataFrame,
    oms_in: pd.DataFrame,
    oms_refund: pd.DataFrame,
    *,
    period: str = "",
    shop_name: str = "",
    shop_code: str = "",
) -> pd.DataFrame:
    """退款侧两段匹配：先撞入库单，未匹配的再撞退款单。"""
    if refund is None or refund.empty:
        return _empty_detail()

    left = refund.copy()
    left["订单编号"] = left["订单编号"].astype("string").fillna("").str.strip()

    detail_cols = ("id", "单据类型", "存货异动时间", "单号", "sku", "款号", "商品名称",
                   "品类", "年份", "季节", "品牌", "吊牌价", "数量", "店铺名称",
                   "结算金额", "退款类型", "退款状态", "订单日期")

    def _blank_join(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        for c in detail_cols:
            if c not in out.columns:
                out[c] = ""
        return out

    # 第一段：入库单
    if oms_in is not None and not oms_in.empty:
        right = oms_in.copy()
        right["订单编号"] = right["订单编号"].astype("string").fillna("").str.strip()
        first = pd.merge(left, right, how="left", on="订单编号", suffixes=("", "_oms"))
    else:
        first = _blank_join(left)

    first = _mark_duplicates(first, "订单编号")
    first["巨益单据匹配状态"] = np.where(
        first["单号"].notna() & (first["单号"].astype("string").fillna("") != ""),
        MATCHED, UNMATCHED,
    )

    matched_part = first[first["巨益单据匹配状态"] == MATCHED].copy()
    unmatched_part = first[first["巨益单据匹配状态"] == UNMATCHED].copy()

    # 第二段：未匹配的再撞退款单（先去重）
    refund_orders = _dedup_refund_orders(oms_refund)
    if not unmatched_part.empty and refund_orders is not None and not refund_orders.empty:
        r2 = refund_orders.copy()
        r2["订单编号"] = r2["订单编号"].astype("string").fillna("").str.strip()
        base = unmatched_part.loc[:, [c for c in ("账单周期", "订单编号", "货款金额") if c in unmatched_part.columns]]
        second = pd.merge(base, r2, how="left", on="订单编号", suffixes=("", "_oms"))
        second = _mark_duplicates(second, "订单编号")
        second["巨益单据匹配状态"] = np.where(
            second["单号"].notna() & (second["单号"].astype("string").fillna("") != ""),
            MATCHED, UNMATCHED,
        )
        unmatched_part = second

    parts = [p for p in (matched_part, unmatched_part) if p is not None and not p.empty]
    if not parts:
        return _empty_detail()
    merged = pd.concat(parts, ignore_index=True)
    merged = _blank_join(merged)

    if "结算金额" in merged.columns:
        merged["结算金额"] = merged["结算金额"].map(lambda v: to_fen(v) if not _is_blank(v) else 0)

    merged["采购单价"] = 0
    merged["发货成本"] = 0
    merged["标准成本单价"] = 0
    merged["标准成本"] = 0
    merged["数据来源"] = SRC_REFUND
    merged["账单周期"] = period
    merged["店铺名称"] = shop_name or merged.get("店铺名称", "")
    if shop_code:
        merged["店铺编码"] = shop_code
    elif "店铺编码" not in merged.columns:
        merged["店铺编码"] = ""
    # 原脚本 L700：退款侧初始空串，秒退交叉核对（L885-887）才填「是」。不写「否」。
    merged["是否秒退判断"] = ""
    # 同 match_receipt：历史对账记录 / 对账周期 必须保留 OMS join 带进来的真值
    _preserve_oms_columns(merged)
    merged["货款分摊金额"] = merged["货款金额"]

    # 处理意见不在这里定稿：原脚本（look_sku.result 1505-1517）只对
    # 单据类型 == B2C退换货通知单 给「冲成本」，其余（退款单等）留空后统一
    # replace 成「成本不做处理」。这里必须留空串交给 finalize_advice，
    # 否则 finalize 的「处理意见为空」前置条件不成立，退款单会被错判成冲成本。
    merged["处理意见"] = ""
    return merged


def _is_blank(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() in ("", "nan", "None", "NaT")


def mark_instant_refund(receipt: pd.DataFrame, refund: pd.DataFrame) -> pd.DataFrame:
    """秒退判断：未匹配的收款订单，若退款侧同订单货款金额完全相等 → 标「是」。

    原脚本：未匹配的收款订单去退款侧汇总，金额相等即视为秒退（当天买当天退），
    这部分不应算作「无发货」差异。

    ⚠️ 退款侧汇总必须**按订单取一行**，不能直接 groupby().sum()。
    原脚本在 merge 后立刻把「标记重复」行的货款金额置 0（look_sku.py 691-694），
    所以 pivot 求和 == 该单账单货款。而本项目刻意把置 0 推迟到 allocation
    （见 _mark_duplicates 注释：提前置 0 会让分摊拿到 gross=0 并打乱落位），
    于是 left join 复制出的 n 行**每行都带完整货款金额**，直接 sum 会得到
    n×货款，等式永不成立 —— 只有恰好 1 行退款的订单能侥幸命中。

    2026-11-14 京东 JD001 实测：收款侧秒退订单 27 vs 基准 32，差的 5 单退款行数
    分别为 8/2/2/6/2；漏标导致这 5 单没走 rematch_instant_refund，
    收款侧少 15 行、结算金额差 -390843 分、数量差 -22 件。
    同组各行货款金额由 merge 复制而来必然相同，故取首行与行序无关。
    """
    out = receipt.copy()
    if out.empty:
        return out
    if "是否秒退判断" not in out.columns:
        out["是否秒退判断"] = ""
    # 原脚本 L794：已匹配行强制空串（只有未匹配行才可能判「是」）
    out.loc[out["巨益单据匹配状态"] == MATCHED, "是否秒退判断"] = ""
    if refund is None or refund.empty:
        return out

    ref = refund.copy()
    ref["订单编号"] = ref["订单编号"].astype("string").fillna("").str.strip()
    ref["货款金额"] = pd.to_numeric(ref["货款金额"], errors="coerce").fillna(0).astype("int64")
    # 每单只取一行（等价于原脚本「重复行置 0 后求和」）
    ref_first = ref.drop_duplicates(subset=["订单编号"], keep="first")
    refund_sum = ref_first.set_index("订单编号")["货款金额"].to_dict()

    mask = out["巨益单据匹配状态"] == UNMATCHED
    for i in out.index[mask]:
        order_no = str(out.at[i, "订单编号"] or "")
        gross = int(out.at[i, "货款金额"] or 0)
        if order_no and refund_sum.get(order_no) == gross and gross != 0:
            out.at[i, "是否秒退判断"] = "是"
            out.at[i, "处理意见"] = ADVICE_NO_ACTION
    return out


def mark_refund_instant(receipt: pd.DataFrame, refund: pd.DataFrame) -> pd.DataFrame:
    """退款侧秒退交叉标记（原 look_sku.result L860-887）。

    收款侧已判「是」的秒退行，按两条路径传导到退款侧：
      1. 收款秒退且单据类型=退款单（重匹配命中 OMS 退款单）→ 取其「单号」集合，
         退款侧同单号行标「是」；
      2. 收款秒退且单据类型为空（重匹配未命中）→ 取其「订单编号」集合（剔空），
         退款侧同订单编号行标「是」。
    其余退款行保持空串。2026-11-17 影刀10月基准实测：HYK 退款 47 行因此标「是」，
    旧实现漏掉这段 → 退款侧秒退全空。
    """
    if refund is None or refund.empty:
        return refund
    ref = refund.copy()
    ref["是否秒退判断"] = ""
    if receipt is None or receipt.empty or "是否秒退判断" not in receipt.columns:
        return ref
    inst = receipt[receipt["是否秒退判断"].astype("string").fillna("") == "是"]
    if inst.empty:
        return ref
    doc = inst.get("单据类型", pd.Series(index=inst.index, dtype="object"))
    by_doc = inst[doc.astype("string").fillna("") == DOC_REFUND]
    set1 = {s for s in by_doc.get("单号", pd.Series(dtype="object")).astype("string").fillna("").tolist() if s}
    by_order = inst[doc.isna() | (doc.astype("string").fillna("") == "")]
    set2 = {s for s in by_order.get("订单编号", pd.Series(dtype="object")).astype("string").fillna("").tolist() if s}
    if set1 and "单号" in ref.columns:
        ref.loc[ref["单号"].astype("string").fillna("").isin(set1), "是否秒退判断"] = "是"
    if set2 and "订单编号" in ref.columns:
        ref.loc[ref["订单编号"].astype("string").fillna("").isin(set2), "是否秒退判断"] = "是"
    return ref


def rematch_instant_refund(
    receipt: pd.DataFrame,
    oms_refund: pd.DataFrame,
    *,
    period: str = "",
    shop_name: str = "",
    shop_code: str = "",
) -> pd.DataFrame:
    """秒退收款行改与 OMS 退款单重新匹配（原 look_sku.result 797-857 段）。

    原脚本把「未匹配出库单 + 货款金额与退款侧完全相等」的收款行判为秒退
    （当天买当天退、本期无发货）。这批行**不再挂在出库单上**：只保留
    [订单编号, 货款金额, 账单周期, 是否秒退判断] 四列，改与 oms_df_af（退款单，
    按 单号+sku+结算金额 去重）重新 left join，再与其余收款行 concat。

    因此基准「收款匹配」sheet 里会出现 单据类型='退款单' 且账单周期非空的行。

    2026-11-14 京东 JD001 实测：漏掉这轮重匹配时收款侧行数 1689 vs 基准 1704、
    47 行内容错位（基准独有行全部是「账单周期非空 + 单据类型=退款单」），
    结算金额合计差 -903033 分、数量差 -51 件。
    """
    if receipt is None or receipt.empty:
        return receipt
    if "是否秒退判断" not in receipt.columns:
        return receipt

    is_instant = receipt["是否秒退判断"].astype("string").fillna("") == "是"
    if not bool(is_instant.any()):
        return receipt

    rest = receipt.loc[~is_instant].copy()
    inst = receipt.loc[is_instant].copy()

    # 原脚本：秒退行只带这 4 列去 join，OMS 侧字段全部来自退款单
    keep = [c for c in ("账单周期", "订单编号", "货款金额", "是否秒退判断") if c in inst.columns]
    inst = inst.loc[:, keep]
    inst["订单编号"] = inst["订单编号"].astype("string").fillna("").str.strip()

    refund_orders = _dedup_refund_orders(oms_refund)
    if refund_orders is not None and not refund_orders.empty:
        right = _stable_sort_oms(refund_orders)
        right["订单编号"] = right["订单编号"].astype("string").fillna("").str.strip()
        merged = pd.merge(inst, right, how="left", on="订单编号", suffixes=("", "_oms"))
    else:
        merged = inst.copy()

    merged = _mark_duplicates(merged, "订单编号")
    merged["巨益单据匹配状态"] = np.where(
        merged["单号"].notna() & (merged["单号"].astype("string").fillna("") != ""),
        MATCHED, UNMATCHED,
    ) if "单号" in merged.columns else UNMATCHED

    for c in ("id", "单据类型", "存货异动时间", "单号", "sku", "款号", "商品名称",
              "品类", "年份", "季节", "品牌", "吊牌价", "数量", "店铺名称",
              "结算金额", "退款类型", "退款状态", "订单日期"):
        if c not in merged.columns:
            merged[c] = ""

    if "结算金额" in merged.columns:
        merged["结算金额"] = merged["结算金额"].map(lambda v: to_fen(v) if not _is_blank(v) else 0)
    if "数量" in merged.columns:
        merged["数量"] = [safe_int(v) for v in merged["数量"].tolist()]

    merged["采购单价"] = 0
    merged["发货成本"] = 0
    merged["标准成本单价"] = 0
    merged["标准成本"] = 0
    merged["数据来源"] = SRC_RECEIPT
    merged["账单周期"] = period
    merged["店铺名称"] = shop_name or merged.get("店铺名称", "")
    if shop_code:
        merged["店铺编码"] = shop_code
    elif "店铺编码" not in merged.columns:
        merged["店铺编码"] = ""
    # 处理意见留给 finalize_advice 定稿（与 match_refund 同理）
    merged["处理意见"] = ""
    _preserve_oms_columns(merged)
    merged["货款分摊金额"] = merged["货款金额"]
    if "订单日期" not in merged.columns:
        merged["订单日期"] = ""

    parts = [p for p in (rest, merged) if p is not None and not p.empty]
    if not parts:
        return _empty_detail()
    out = pd.concat(parts, ignore_index=True)
    for c in DETAIL_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    return out


def apply_advice(receipt: pd.DataFrame, refund: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """统一三分处理意见（成本结转口径核心）。

    · 收款已匹配 → 结转成本
    · 收款未匹配且秒退 → 成本不做处理
    · 收款未匹配非秒退 → 成本不做处理（待人工判定）
    · 退款：入库有对应出库 → 冲发出商品；无 → 冲成本
    """
    rec = receipt.copy()
    if not rec.empty:
        rec["处理意见"] = np.where(
            rec["巨益单据匹配状态"] == MATCHED, ADVICE_CARRY, ADVICE_NO_ACTION
        )
        instant = rec.get("是否秒退判断", pd.Series(index=rec.index, dtype="object")) == "是"
        rec.loc[instant, "处理意见"] = ADVICE_NO_ACTION

    ref = refund.copy()
    if not ref.empty:
        matched_in = ref["巨益单据匹配状态"] == MATCHED
        ref["处理意见"] = np.where(matched_in, ADVICE_OFFSET_SHIP, ADVICE_OFFSET_COST)
    return rec, ref


def build_oms_writeback(receipt: pd.DataFrame, refund: pd.DataFrame, *, period: str = "") -> pd.DataFrame:
    """「已匹配的巨益单号」表（回传数据库用）。

    原脚本（look_sku.py 1546）：
        df_res = df_res[(df_res["历史对账记录"] == "未对账") & (df_res["巨益单据匹配状态"] == "已匹配")]
    **两个条件缺一不可**。只过滤「已匹配」会把历史月份已经回传过（已对账）的单据
    再写一遍 bill_state/bill_period，覆盖掉它真实的对账周期。

    2026-08 回归实测：该过滤此前从未生效，因为 match_receipt/match_refund 把
    历史对账记录 覆盖成了空串（见 _preserve_oms_columns）。修复列保留后必须
    同时补上这个条件，否则修缺陷 1 反而会放大回传范围。

    列：id / 账单周期 / 数量。
    """
    parts = []
    for df in (receipt, refund):
        if df is None or df.empty or "id" not in df.columns:
            continue
        sub = df[df["巨益单据匹配状态"] == MATCHED].copy()
        if sub.empty:
            continue
        # 只回传「未对账」单据（原脚本 1546 行）
        if "历史对账记录" in sub.columns:
            hist = sub["历史对账记录"].astype("string").fillna("").str.strip()
            sub = sub[hist == "未对账"]
            if sub.empty:
                continue
        sub = sub[~sub["id"].map(_is_blank)]
        if sub.empty:
            continue
        parts.append(sub.loc[:, [c for c in ("id", "账单周期", "数量") if c in sub.columns]])
    if not parts:
        return pd.DataFrame(columns=["id", "账单周期", "数量"])
    out = pd.concat(parts, ignore_index=True)
    for c in ("id", "账单周期", "数量"):
        if c not in out.columns:
            out[c] = ""
    out["id"] = out["id"].astype("string").fillna("")
    out["数量"] = pd.to_numeric(out["数量"], errors="coerce").fillna(0).astype("int64")
    out = out.drop_duplicates(subset=["id"])
    # 原脚本 L1553：id 透视后**统一赋 bill_time**（不从明细带列——补录行账单周期为空，
    # 带列会让回传表账单周期空白）。2026-11-17 影刀10月基准实测：HYK 303 行全有账期。
    if period:
        out["账单周期"] = period
    return out


def _blank_oms_rows(
    oms_df: pd.DataFrame,
    *,
    period: str,
    shop_name: str,
    shop_code: str,
    source: str,
    keep_period: bool = False,
) -> pd.DataFrame:
    """OMS 原始行 → 标准明细行骨架（补录用）。

    原脚本（look_sku.py 890-920）把补录行的这些列显式置空/置零：
      账单周期="" / 货款金额=NaN / 货款分摊金额=NaN / 款号·商品名称·年份·季节·品牌·吊牌价=""
      发货成本·标准成本单价·标准成本·采购单价=0 / 巨益单据匹配状态="" / 是否秒退判断="" / 处理意见=""

    「账单周期=""」不是笔误，是有业务含义的：后续 res_receipt['账单周期'].fillna(bill_time)
    只填 NaN，**不填空串**，所以补录行的账单周期一路保持空 → 导出 Excel 后为空白。
    基准文件里这批行的账单周期确实是空的（2026-08 实测），必须原样保留。

    keep_period=True 用于账单侧已匹配行（账单周期 = 本次账期）。
    """
    out = pd.DataFrame(columns=list(DETAIL_OUTPUT_COLUMNS))
    if oms_df is None or oms_df.empty:
        return out

    src = oms_df.copy()
    for c in ("订单编号", "单据类型", "存货异动时间", "单号", "sku", "退款类型",
              "退款状态", "历史对账记录", "对账周期", "订单日期"):
        if c not in src.columns:
            src[c] = ""

    n = len(src)
    # 先用 Series 把行数撑起来，再赋标量列。反过来（先给空 DataFrame 赋标量）会让标量列
    # 停在 0 行，随后 Series 赋值触发 reindex，标量列整列变 NaN —— 而「账单周期为 NaN」
    # 与「为空串」在原脚本口径里不等价：fillna(bill_time) 只填 NaN，空串会一路保持空，
    # 并让 conditon_receipt_match_status 的 ~isnull() 成立，从而把匹配状态回落为未匹配。
    out["订单编号"] = src["订单编号"].astype("string").fillna("").str.strip().values
    out["订单日期"] = src["订单日期"].astype("string").fillna("").values
    out["id"] = src["id"].astype("string").fillna("").values if "id" in src.columns else [""] * n
    out["单据类型"] = src["单据类型"].astype("string").fillna("").values
    out["存货异动时间"] = src["存货异动时间"].astype("string").fillna("").values
    out["单号"] = src["单号"].astype("string").fillna("").values
    out["sku"] = src["sku"].astype("string").fillna("").values
    out["品类"] = src["品类"].astype("string").fillna("").values if "品类" in src.columns else [""] * n
    out["数量"] = [safe_int(v) for v in src.get("数量", pd.Series(dtype="object")).tolist()]
    out["结算金额"] = (
        [to_fen(v) for v in src["结算金额"].tolist()] if "结算金额" in src.columns else [0] * n
    )
    out["退款类型"] = src["退款类型"].astype("string").fillna("").values
    out["退款状态"] = src["退款状态"].astype("string").fillna("").values
    out["历史对账记录"] = src["历史对账记录"].astype("string").fillna("").values
    out["对账周期"] = src["对账周期"].astype("string").fillna("").values

    # 以下一律标量列
    out["账单周期"] = period if keep_period else ""
    out["货款金额"] = None
    out["货款分摊金额"] = None
    for c in ("款号", "商品名称", "年份", "季节", "品牌", "吊牌价"):
        out[c] = ""
    out["店铺名称"] = shop_name or ""
    out["店铺编码"] = shop_code or ""
    for c in ("标准成本单价", "标准成本", "采购单价", "发货成本"):
        out[c] = 0
    out["巨益单据匹配状态"] = MATCHED      # 原脚本补录即标已匹配，收款侧再由终判回落
    out["是否秒退判断"] = ""
    out["处理意见"] = ""
    out["数据来源"] = source
    out["标记重复"] = False
    return out.loc[:, list(DETAIL_OUTPUT_COLUMNS) + ["标记重复"]]


def supplement_oms_remainder(
    receipt: pd.DataFrame,
    refund: pd.DataFrame,
    oms: dict[str, pd.DataFrame],
    *,
    period: str = "",
    s_date: str = "",
    e_date: str = "",
    shop_name: str = "",
    shop_code: str = "",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """OMS 剩余单据补录（原 look_sku.result 890-1193 段）。

    账单只是资金流水，OMS 里还有大量「本期发了货但账单没体现」的出库单，以及
    「本期退货入库但账单没退款记录」的入库单。这批不进对账结果，成本结转就会漏。
    原脚本把它们补录进结果表，并按能否与对方单据对上给出三分处理意见：

      收款侧 ← OMS 出库单中「订单编号_sku」命中未核对入库单、且未参与过收款核对、
               且历史对账记录为「未对账」的行 → 处理意见 = 成本不做处理
      退款侧 ← 账期内（存货异动时间）且历史对账记录 ≠ 已对账、且单号未被退款核对
               用掉的入库单，再按能否与上面补录的出库单对上分派：
                 · 订单编号_sku 命中补录出库 → 冲发出商品
                 · 订单编号 命中补录出库（sku 不同）→ 冲发出商品
                 · 都不命中 → 冲成本

    补录行的「货款金额/货款分摊金额」为 None（无账单金额），分摊合计不受影响，
    因此必须在 allocation 之前调用。
    """
    stats = {"supplement_receipt": 0, "supplement_refund": 0}
    oms_in = oms.get("入库")
    oms_out = oms.get("出库")
    if oms_in is None or oms_in.empty:
        return receipt, refund, stats

    ref = refund if refund is not None else pd.DataFrame()
    rec = receipt if receipt is not None else pd.DataFrame()

    # 1) 账期内的入库单，且历史对账记录 ≠ 已对账
    df_in = oms_in.copy()
    df_in["存货变动日期"] = pd.to_datetime(df_in["存货异动时间"], errors="coerce").dt.floor("D")
    if s_date and e_date:
        df_in = df_in[
            (df_in["存货变动日期"] >= pd.Timestamp(s_date))
            & (df_in["存货变动日期"] <= pd.Timestamp(e_date))
        ]
    history = df_in.get("历史对账记录", pd.Series(index=df_in.index, dtype="object"))
    df_in = df_in[history.astype("string").fillna("").str.strip() != "已对账"]
    if df_in.empty:
        return rec, ref, stats

    # 2) 剔除已被退款核对用掉的入库单号
    used_doc_nos: list[str] = []
    if not ref.empty and "单据类型" in ref.columns:
        used = ref[ref["单据类型"].astype("string").fillna("") == DOC_RETURN]
        used_doc_nos = [
            s for s in used.get("单号", pd.Series(dtype="object")).astype("string").fillna("").unique().tolist() if s
        ]
    if used_doc_nos:
        df_in = df_in[~df_in["单号"].astype("string").fillna("").isin(used_doc_nos)]
    if df_in.empty:
        return rec, ref, stats

    in_order_sku = df_in["订单编号"].astype("string").fillna("") + "_" + df_in["sku"].astype("string").fillna("")
    in_order_sku_list = [s for s in in_order_sku.unique().tolist() if s]
    in_order_list = [s for s in df_in["订单编号"].astype("string").fillna("").unique().tolist() if s]

    # 3) 收款核对已对上发货的「订单编号_sku」，需从补录候选里排除
    checked_order_sku: list[str] = []
    if not rec.empty and "单号" in rec.columns:
        hit = rec[~rec["单号"].astype("string").fillna("").isin(["", "None", "nan"])]
        if not hit.empty:
            checked_order_sku = [
                s for s in (
                    hit["订单编号"].astype("string").fillna("") + "_" + hit["sku"].astype("string").fillna("")
                ).unique().tolist() if s
            ]

    # 4) 出库单里挑出「与未核对入库单同订单_sku、且未参与收款核对、且未对账」的行 → 补录到收款侧
    out_sku_frame = pd.DataFrame()
    out_nosku_orders: list[str] = []
    if oms_out is not None and not oms_out.empty:
        df_out = oms_out.copy()
        out_order_sku = df_out["订单编号"].astype("string").fillna("") + "_" + df_out["sku"].astype("string").fillna("")
        out_history = df_out.get("历史对账记录", pd.Series(index=df_out.index, dtype="object"))
        base_mask = (~out_order_sku.isin(checked_order_sku)) & (
            out_history.astype("string").fillna("").str.strip() == "未对账"
        )
        out_sku_frame = df_out[base_mask & out_order_sku.isin(in_order_sku_list)].copy()
        out_nosku_orders = [
            s for s in df_out[base_mask & df_out["订单编号"].astype("string").fillna("").isin(in_order_list)]
            ["订单编号"].astype("string").fillna("").unique().tolist() if s
        ]

    if not out_sku_frame.empty:
        supp_rec = _blank_oms_rows(
            out_sku_frame, period=period, shop_name=shop_name, shop_code=shop_code,
            source=SRC_RECEIPT,
        )
        supp_rec["处理意见"] = ADVICE_NO_ACTION      # 原脚本：出库未参与收款核对 → 成本不做处理
        rec = pd.concat([rec, supp_rec], ignore_index=True)
        stats["supplement_receipt"] = int(len(supp_rec))

    # 5) 入库单补录到退款侧，按能否与补录出库对上分派处理意见
    supp_in = _blank_oms_rows(
        df_in, period=period, shop_name=shop_name, shop_code=shop_code, source=SRC_REFUND,
    )
    if supp_in.empty:
        return rec, ref, stats

    supp_order_sku = supp_in["订单编号"].astype("string").fillna("") + "_" + supp_in["sku"].astype("string").fillna("")
    if not out_sku_frame.empty:
        supp_out_order_sku = (
            out_sku_frame["订单编号"].astype("string").fillna("") + "_"
            + out_sku_frame["sku"].astype("string").fillna("")
        ).unique().tolist()
        hit_sku = supp_order_sku.isin(supp_out_order_sku)
        hit_order = supp_in["订单编号"].astype("string").fillna("").isin(out_nosku_orders)
        advice = np.where(hit_sku | hit_order, ADVICE_OFFSET_SHIP, ADVICE_OFFSET_COST)
    else:
        advice = ADVICE_OFFSET_COST
    supp_in["处理意见"] = advice
    ref = pd.concat([ref, supp_in], ignore_index=True)
    stats["supplement_refund"] = int(len(supp_in))
    return rec, ref, stats


def _compare_periods(bill_period, recon_period) -> int:
    """原 look_sku.compare_dates：对账周期为空 → 0；账单周期为空 → 1；否则比大小。

    返回 0 表示「本期可以出处理意见」，1 表示「该单据属于更早账期，已在他期结转」。
    账期格式为中文标签（2026年08月-2026年08月），比较用 YYYYMM 整数。
    """
    b = "" if _is_blank(bill_period) else str(bill_period)
    r = "" if _is_blank(recon_period) else str(recon_period)
    if not r:
        return 0
    if not b:
        return 1
    try:
        return 1 if _period_int(b) > _period_int(r) else 0
    except Exception:
        return 0


def _period_int(label: str) -> int:
    year, rest = str(label).split("年", 1)
    month = rest.replace("月", "").split("-")[0]
    return int(year + month)


def finalize_advice(
    receipt: pd.DataFrame, refund: pd.DataFrame, *, period: str = ""
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """处理意见与匹配状态终判（原 look_sku.result 1429-1541 段）。

    必须在补录与分摊之后、导出之前跑。逐条对齐原脚本：
      C = compare_dates(账单周期, 对账周期)
      收款：处理意见为空 & B2C配货单 & 非秒退 & C==0 & 历史对账记录≠已对账 → 结转成本
            处理意见仍为空 → 成本不做处理
            分摊>0 & 数量>0 & 非秒退 & C==0 → 结转成本（二次修正）
            账单周期为 null | 分摊≤0 | 秒退 → 成本不做处理
            分摊≤0 & 历史对账记录==未对账 & 账单周期非 null → 匹配状态回落「未匹配」
      退款：处理意见为空 & B2C退换货通知单 & 非秒退 & C==0 & 历史对账记录≠已对账 → 冲成本
            处理意见仍为空 → 成本不做处理
            收款侧（账单周期非空 & 成本不做处理）的 订单编号_sku → 退款同键「冲成本」改「冲发出商品」
            收款侧（账单周期非空 & 结转成本）的 订单编号_sku → 退款同键「冲发出商品」改「冲成本」

    注意「账单周期为 null」与「账单周期为空串」在语义上不同：补录行的账单周期是
    空串（原脚本 fillna 只填 NaN，不填空串），空串 **不算** null，所以补录行会走
    分摊≤0 → 成本不做处理，并因「未对账」把匹配状态回落为未匹配。2026-08 基准实测一致。
    """
    rec = receipt.copy() if receipt is not None and not receipt.empty else receipt
    ref = refund.copy() if refund is not None and not refund.empty else refund

    def _c_flag(df: pd.DataFrame) -> pd.Series:
        return pd.Series(
            [
                _compare_periods(a, b)
                for a, b in zip(
                    df.get("账单周期", pd.Series(index=df.index, dtype="object")).tolist(),
                    df.get("对账周期", pd.Series(index=df.index, dtype="object")).tolist(),
                )
            ],
            index=df.index,
        )

    if rec is not None and not rec.empty:
        rec["处理意见"] = rec.get("处理意见", pd.Series(index=rec.index, dtype="object")).astype("string").fillna("")
        alloc = pd.to_numeric(rec.get("货款分摊金额"), errors="coerce").fillna(0)
        qty = pd.to_numeric(rec.get("数量"), errors="coerce").fillna(0)
        instant = rec.get("是否秒退判断", pd.Series(index=rec.index, dtype="object")).astype("string").fillna("") == "是"
        doc_type = rec.get("单据类型", pd.Series(index=rec.index, dtype="object")).astype("string").fillna("")
        history = rec.get("历史对账记录", pd.Series(index=rec.index, dtype="object")).astype("string").fillna("").str.strip()
        period_null = rec.get("账单周期", pd.Series(index=rec.index, dtype="object")).isnull()
        c0 = _c_flag(rec) == 0

        cond1 = (
            (rec["处理意见"] == "") & (doc_type == DOC_DELIVERY) & (~instant) & c0
            & (history != "已对账")
        )
        rec.loc[cond1, "处理意见"] = ADVICE_CARRY
        rec["处理意见"] = rec["处理意见"].replace("", ADVICE_NO_ACTION)

        cond2 = (alloc > 0) & (qty > 0) & (~instant) & c0
        rec.loc[cond2, "处理意见"] = ADVICE_CARRY

        cond3 = period_null | (alloc <= 0) | instant
        rec.loc[cond3, "处理意见"] = ADVICE_NO_ACTION

        fallback = (alloc <= 0) & (history == "未对账") & (~period_null)
        rec.loc[fallback, "巨益单据匹配状态"] = UNMATCHED

    if ref is not None and not ref.empty:
        ref["处理意见"] = ref.get("处理意见", pd.Series(index=ref.index, dtype="object")).astype("string").fillna("")
        doc_type = ref.get("单据类型", pd.Series(index=ref.index, dtype="object")).astype("string").fillna("")
        history = ref.get("历史对账记录", pd.Series(index=ref.index, dtype="object")).astype("string").fillna("").str.strip()
        instant = ref.get("是否秒退判断", pd.Series(index=ref.index, dtype="object")).astype("string").fillna("") == "是"
        c0 = _c_flag(ref) == 0

        cond = (
            (ref["处理意见"] == "") & (doc_type == DOC_RETURN) & (~instant) & c0
            & (history != "已对账")
        )
        ref.loc[cond, "处理意见"] = ADVICE_OFFSET_COST
        ref["处理意见"] = ref["处理意见"].replace("", ADVICE_NO_ACTION)

    if ref is not None and not ref.empty and rec is not None and not rec.empty:
        from_bill = ~rec.get("账单周期", pd.Series(index=rec.index, dtype="object")).isnull()
        key_rec = rec["订单编号"].astype("string").fillna("") + "_" + rec["sku"].astype("string").fillna("")
        key_ref = ref["订单编号"].astype("string").fillna("") + "_" + ref["sku"].astype("string").fillna("")

        # 收款「成本不做处理」→ 退款同键的冲成本改为冲发出商品
        no_action_keys = set(key_rec[from_bill & (rec["处理意见"] == ADVICE_NO_ACTION)].tolist())
        if no_action_keys:
            mask = key_ref.isin(no_action_keys) & (ref["处理意见"] == ADVICE_OFFSET_COST)
            ref.loc[mask, "处理意见"] = ADVICE_OFFSET_SHIP

        # 收款「结转成本」→ 退款同键的冲发出商品改为冲成本（原脚本变量名 df_out__ 实际用 df_out_）
        carry_keys = set(key_rec[from_bill & (rec["处理意见"] == ADVICE_CARRY)].tolist())
        if carry_keys:
            mask = key_ref.isin(carry_keys) & (ref["处理意见"] == ADVICE_OFFSET_SHIP)
            ref.loc[mask, "处理意见"] = ADVICE_OFFSET_COST

    return rec, ref


def match_all(
    bill_split: dict[str, pd.DataFrame],
    oms: dict[str, pd.DataFrame],
    *,
    period: str = "",
    s_date: str = "",
    e_date: str = "",
    shop_name: str = "",
    shop_code: str = "",
    finalize: bool = True,
) -> MatchResult:
    """完整匹配流水线：收款 + 退款 + 秒退 + OMS 剩余补录 + 处理意见终判 + 回传表。

    finalize=False：只做「匹配 + 补录」，**不做处理意见终判**，把终判留给调用方在
    货款分摊之后执行。原脚本顺序是 分摊（look_sku 1201 行）→ 终判（1429–1541 行），
    终判的 cond2/cond3/fallback 都读「货款分摊金额」；若在分摊前终判，读到的是
    未分摊的货款金额，会把分摊为 0 的行误判成「结转成本」。
    2026-08 DY023 实测：2 行因此错判，店铺透视结转成本多算 280.36 元 / 2 件，
    并连锁把退款侧 2 行「冲发出商品」翻成「冲成本」。
    """
    receipt = match_receipt(bill_split.get("收款"), oms.get("出库"), period=period,
                            shop_name=shop_name, shop_code=shop_code)
    refund = match_refund(
        bill_split.get("退款"), oms.get("入库"), oms.get("退款"),
        period=period, shop_name=shop_name, shop_code=shop_code,
    )
    receipt = mark_instant_refund(receipt, refund)
    # 秒退行改与 OMS 退款单重新匹配（原 look_sku.result 797-857）。
    # 必须在 mark_instant_refund 之后（靠「是否秒退判断」挑行）、
    # supplement_oms_remainder 之前（补录段读 rec 的「单号」排除已核对订单，
    # 秒退行重匹配后单号来自退款单，若补录在前会把这批单重复补录一遍）。
    receipt = rematch_instant_refund(
        receipt, oms.get("退款"),
        period=period, shop_name=shop_name, shop_code=shop_code,
    )
    # 退款侧秒退交叉标记（原 L860-887）：必须在 rematch 之后（靠重匹配后的单据类型/单号）
    refund = mark_refund_instant(receipt, refund)

    # OMS 剩余单据补录：必须在分摊之前，补录行货款金额为 None 不参与分摊合计
    receipt, refund, supp_stats = supplement_oms_remainder(
        receipt, refund, oms, period=period, s_date=s_date, e_date=e_date,
        shop_name=shop_name, shop_code=shop_code,
    )
    if finalize:
        receipt, refund = finalize_advice(receipt, refund, period=period)

    oms_matched = build_oms_writeback(receipt, refund, period=period)
    stats = recount(receipt, refund, oms_matched)
    stats.update(supp_stats)

    return MatchResult(
        receipt=receipt,
        refund=refund,
        oms_matched=oms_matched,
        stats=stats,
    )


def recount(receipt: pd.DataFrame, refund: pd.DataFrame,
            oms_matched: pd.DataFrame | None = None) -> dict[str, int]:
    """按当前明细重算匹配统计（终判会改匹配状态，故终判后必须重算）。"""
    rec = receipt if receipt is not None else pd.DataFrame()
    ref = refund if refund is not None else pd.DataFrame()

    def _cnt(df: pd.DataFrame, value: str) -> int:
        if df.empty or "巨益单据匹配状态" not in df.columns:
            return 0
        return int((df["巨益单据匹配状态"] == value).sum())

    instant = 0
    if not rec.empty and "是否秒退判断" in rec.columns:
        instant = int((rec["是否秒退判断"].astype("string").fillna("") == "是").sum())

    return {
        "receipt_rows": int(len(rec)),
        "refund_rows": int(len(ref)),
        "matched_rows": _cnt(rec, MATCHED) + _cnt(ref, MATCHED),
        "unmatched_rows": _cnt(rec, UNMATCHED) + _cnt(ref, UNMATCHED),
        "instant_refund_rows": instant,
        "writeback_rows": int(len(oms_matched)) if oms_matched is not None else 0,
        "receipt_gross_fen": sum_fen(rec["货款金额"].tolist()) if not rec.empty else 0,
        "refund_gross_fen": sum_fen(ref["货款金额"].tolist()) if not ref.empty else 0,
    }


def finalize_detail(detail: pd.DataFrame, *, period: str = "") -> pd.DataFrame:
    """在**合并明细**上做处理意见终判（货款分摊之后调用）。

    按「数据来源」拆成收款/退款两侧 → finalize_advice → 合回原行序。
    两侧索引互不重叠，concat 后 sort_index 即还原分摊输出的顺序。
    """
    if detail is None or detail.empty or "数据来源" not in detail.columns:
        return detail
    src = detail.get("数据来源", pd.Series(index=detail.index, dtype="object")).astype("string").fillna("")
    rec = detail[src == SRC_RECEIPT].copy()
    ref = detail[src == SRC_REFUND].copy()
    if rec.empty and ref.empty:
        return detail
    rec, ref = finalize_advice(rec, ref, period=period)
    parts = [p for p in (rec, ref) if p is not None and not p.empty]
    return pd.concat(parts, ignore_index=False).sort_index()
