"""B2B 渠道 SKU 对账（爱库存 / 唯品会）——承接影刀 look_sku.B2B_match（L2031-2520）。

与 B2C 主链的本质区别（业务 SSOT：skills/recon_assist/references/sku-match-methodology.md §8）：
  · 不查 OMS 出入库、不做订单匹配 / 秒退 / 额外入库 / 分摊；
  · 账单自带 sku/数量，逐行直接给处理意见：收款→结转成本，退款→冲成本；
  · 「已匹配的巨益单号」为空表 —— 不参与数据库回传与状态闭环；
  · 唯品会 FR 品牌：标准成本 = 结算金额（收退两侧，L2217-2229），毛利为 0（代销口径）。

输入不是 bill_goods（五元无 sku/数量），而是账单整理批次的**明细 sheet**：
  · 爱库存：`爱库存_*_账单整理.xlsx` → 「爱库存账单整理」（RES_COL 14 列，元）
  · 唯品会：`唯品会_*_账单整理.xlsx` → 「唯品会账单整理」（RES_COL 16 列，元）
金额读入即转「分」int，与全插件口径一致；导出时由 exporters 转回元。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from . import cost, db, matching, pivots
from .config import (
    ADVICE_CARRY,
    ADVICE_OFFSET_COST,
    BIZ_GOODS,
    DOC_DELIVERY,
    DOC_RETURN,
    SRC_RECEIPT,
    SRC_REFUND,
)
from .errors import DataSourceEmpty, ReconError
from .money import to_fen

# B2B 明细 sheet 名（与 platforms/akc.py、vip.py export_books 一致）
DETAIL_SHEET = {"akc": "爱库存账单整理", "vip": "唯品会账单整理"}


def find_detail_frames(batch_dir: Path, platform: str) -> list[tuple[Path, pd.DataFrame]]:
    """在账单整理批次目录里找全部明细 sheet（唯品会多旬 → 多份，逐份处理）。

    返回 [(excel路径, 明细df)]，按文件名排序保证旬序稳定。
    """
    sheet = DETAIL_SHEET.get(platform)
    if not sheet:
        raise ReconError(f"平台 {platform} 不是 B2B 渠道", detail={"hint": "B2B 仅 akc/vip"})
    out: list[tuple[Path, pd.DataFrame]] = []
    for p in sorted(Path(batch_dir).glob("*.xlsx")):
        if p.name.startswith("~$") or "账单整理" not in p.name:
            continue
        try:
            xl = pd.ExcelFile(p)
        except Exception:
            continue
        if sheet not in xl.sheet_names:
            continue
        df = xl.parse(sheet, dtype={"销售订单号": "str", "sku": "str", "款号": "str"})
        out.append((p, df))
    return out


def _side_frame(df: pd.DataFrame, *, direction: str, period_cn: str) -> pd.DataFrame:
    """单侧（收款/退款）明细 → 32+2 标准列（原 B2B_match L2057-2126 逐列赋值）。"""
    col = "收款" if direction == SRC_RECEIPT else "退款"
    sub = df[(pd.to_numeric(df[col], errors="coerce").fillna(0) > 0)
             & (df["业务类别"].astype("string").fillna("") == BIZ_GOODS)].copy()
    cols = list(matching.DETAIL_OUTPUT_COLUMNS)
    if sub.empty:
        return pd.DataFrame(columns=cols)

    amount_fen = [to_fen(v) for v in sub[col].tolist()]
    out = pd.DataFrame(index=sub.index)
    out["账单周期"] = period_cn
    # 原 B2B_match L2061/L2097：订单编号 = 销售订单号 原样（NaN 保持空白）。
    # 「无订单号」占位只在 B2C bill_arrange（L475/L497）有，B2B 没有——
    # 2026-11-17 影刀10月基准实测：VIP 退款空订单号是空白单元格，不是「无订单号」。
    out["订单编号"] = sub["销售订单号"].where(sub["销售订单号"].notna(), None)
    out["订单编号"] = [None if v is None or (isinstance(v, float) and v != v) else str(v).strip() or None
                   for v in out["订单编号"].tolist()]
    out["订单日期"] = sub.get("业务发生时间", pd.Series("", index=sub.index)).astype("string").fillna("").str[:10]
    out["货款金额"] = amount_fen
    out["货款分摊金额"] = amount_fen          # B2B 无分摊：逐行全额
    out["id"] = ""
    out["单据类型"] = DOC_DELIVERY if direction == SRC_RECEIPT else DOC_RETURN
    out["存货异动时间"] = ""
    out["单号"] = ""
    out["sku"] = sub["sku"].astype("string").fillna("").str.strip()
    out["款号"] = sub.get("款号", pd.Series("", index=sub.index)).astype("string").fillna("").str.strip()
    out["商品名称"] = ""
    out["品类"] = ""
    out["年份"] = ""
    out["季节"] = ""
    out["品牌"] = sub.get("品牌", pd.Series("", index=sub.index)).astype("string").fillna("").str.strip()
    out["吊牌价"] = ""
    # 数量先 abs（原 L2194；vip 退款行数量为负，透视侧再统一取负）
    out["数量"] = [abs(int(v)) for v in pd.to_numeric(sub["数量"], errors="coerce").fillna(0)]
    out["店铺名称"] = sub["店铺名称"].astype("string").fillna("")
    out["店铺编码"] = sub.get("店铺编码", pd.Series("", index=sub.index)).astype("string").fillna("")
    out["结算金额"] = amount_fen              # 原 L2074：结算金额 = 货款金额
    out["退款类型"] = ""
    out["退款状态"] = ""
    out["标准成本单价"] = 0
    out["标准成本"] = 0
    out["采购单价"] = 0
    out["发货成本"] = 0
    out["巨益单据匹配状态"] = ""               # B2B 无 OMS 匹配状态（原脚本空串）
    out["历史对账记录"] = ""
    out["对账周期"] = ""
    out["是否秒退判断"] = ""
    out["处理意见"] = ADVICE_CARRY if direction == SRC_RECEIPT else ADVICE_OFFSET_COST
    out["数据来源"] = direction
    return out.loc[:, cols].reset_index(drop=True)


def match_b2b_frame(
    df: pd.DataFrame,
    *,
    platform: str,
    period_cn: str,
    use_db: bool = True,
) -> dict[str, Any]:
    """一份 B2B 明细 df → 匹配产物（rec/ref/detail/透视/空回传表）。

    产品信息：查 AI_product_info 补 商品名称/品类/年份/季节/吊牌价/标准成本单价；
    款号仅爱库存从字典回填（原 L2180/2197 特判；vip 保留账单自带款号与品牌）。
    标准成本 = 数量 × 标准成本单价；vip FR 覆盖为 结算金额（收退两侧）。
    发货成本 = 标准成本（B2B 无成本表阶段，原脚本 force_std_as_ship 同口径）。
    """
    rec = _side_frame(df, direction=SRC_RECEIPT, period_cn=period_cn)
    ref = _side_frame(df, direction=SRC_REFUND, period_cn=period_cn)
    parts = [p for p in (rec, ref) if not p.empty]
    detail = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=list(matching.DETAIL_OUTPUT_COLUMNS))

    product_df = pd.DataFrame()
    if use_db and not detail.empty:
        skus = [s for s in detail["sku"].astype("string").fillna("").unique().tolist() if s]
        if skus:
            try:
                product_df = db.fetch_product_info(skus)
            except ReconError:
                product_df = pd.DataFrame()   # 字典查不到 → 占位继续（Q7 口径）

    if not detail.empty:
        keep_style = detail["款号"].copy()
        keep_brand = detail["品牌"].copy()
        detail = cost.enrich_from_db(detail, product_df, None, is_b2b=True)
        if platform == "vip":
            # vip 款号/品牌以账单为准（款号前缀规则覆盖品牌，顺序敏感，账单层已算好）
            detail["款号"] = keep_style.where(keep_style.astype("string").fillna("") != "", detail["款号"])
            detail["品牌"] = keep_brand.where(keep_brand.astype("string").fillna("") != "", detail["品牌"])
        # 标准成本 = 数量 × 标准成本单价（分 int）
        qty = [int(v) for v in pd.to_numeric(detail["数量"], errors="coerce").fillna(0)]
        unit = [int(v) for v in pd.to_numeric(detail["标准成本单价"], errors="coerce").fillna(0)]
        detail["标准成本"] = [u * q for u, q in zip(unit, qty)]
        if platform == "vip":
            fr = detail["品牌"].astype("string").fillna("") == "FR"
            detail.loc[fr, "标准成本"] = detail.loc[fr, "结算金额"]
        # SKU 对账阶段发货成本恒 0（原 B2B_match L2080/L2116 初始 0.00，全程不回填）。
        # 「FR 发货成本=标准成本」是 cost_match **成本匹配阶段**的特例（方法论 §8.2），
        # 不能提前到本阶段——2026-11-17 影刀10月基准实测：AKC/VIP 明细发货成本全 0。
        rec = detail[detail["数据来源"] == SRC_RECEIPT].copy()
        ref = detail[detail["数据来源"] == SRC_REFUND].copy()

    p_shop = pivots.pivot_shop(detail)
    p_product = pivots.pivot_product(detail)
    p_match = pivots.pivot_match(detail)
    oms_matched = pd.DataFrame(columns=["id", "账单周期", "数量"])   # B2B 不回传

    stats = {
        "receipt_rows": int(len(rec)),
        "refund_rows": int(len(ref)),
        "matched_rows": 0,
        "unmatched_rows": 0,
        "instant_refund_rows": 0,
        "writeback_rows": 0,
        "receipt_gross_fen": int(sum(rec["货款金额"].tolist())) if not rec.empty else 0,
        "refund_gross_fen": int(sum(ref["货款金额"].tolist())) if not ref.empty else 0,
        "b2b": True,
    }
    return {
        "receipt": rec, "refund": ref, "detail": detail,
        "pivot_shop": p_shop, "pivot_product": p_product, "pivot_match": p_match,
        "oms_matched": oms_matched, "stats": stats,
    }


def load_b2b_inputs(batch_dir: Path, platform: str) -> list[tuple[Path, pd.DataFrame]]:
    """读批次明细；一份都没有 → DataSourceEmpty（先跑账单整理）。"""
    frames = find_detail_frames(batch_dir, platform)
    if not frames:
        raise DataSourceEmpty(
            "批次目录里没有 B2B 明细 sheet，请先执行「账单整理」",
            detail={"batch_dir": str(batch_dir), "expect_sheet": DETAIL_SHEET.get(platform)},
        )
    return frames