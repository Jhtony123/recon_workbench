"""结果 Excel 导出（承接各模块 to_excel 段 + cost_match.chuanyun 模板）。

输出与原脚本保持一致的 6 sheet 结构，便于财务对照验收：
  店铺数据透视 / 款号数据透视 / 匹配透视 / 收款匹配 / 退款匹配 / 已匹配的巨益单号

差异（相对原脚本）：
  · 输出目录带时间戳，不再 rmtree 覆盖历史结果
  · 金额从「分」还原为元（两位小数）后再写 Excel，财务看到的是元
  · 用 with 语法管理 ExcelWriter（原脚本 writer.save() 在 pandas 2.0+ 已移除）
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from .config import output_dir
from .money import from_fen

# 需要由分转元展示的列（明细表）
DETAIL_MONEY_COLUMNS = (
    "货款金额", "货款分摊金额", "结算金额", "标准成本单价", "标准成本",
    "采购单价", "发货成本", "吊牌价",
)
PIVOT_MONEY_COLUMNS = (
    "货款分摊金额", "订单结算金额", "发货成本", "标准成本",
    "账单结算口径毛利", "订单结算口径毛利", "冲发出商品", "吊牌价",
)
# 匹配透视用的是明细列名「结算金额」（不是「订单结算金额」），需单独转元
MATCH_PIVOT_MONEY_COLUMNS = ("货款分摊金额", "结算金额", "发货成本", "标准成本", "吊牌价")

SHEET_SHOP = "店铺数据透视"
SHEET_PRODUCT = "款号数据透视"
SHEET_MATCH = "匹配透视"
SHEET_RECEIPT = "收款匹配"
SHEET_REFUND = "退款匹配"
SHEET_OMS = "已匹配的巨益单号"
SHEET_CHUANYUN = "氚云导入模板"

# 基准文件（影刀 look_sku.result / B2B_match 产出）的列序，导出时严格对齐。
# 2026-11-17 影刀10月基准实测：三张透视第2列都有「店铺编码」（shop_pivot_col /
# product_pivot_col / unicolumns 均含）。11-14 曾把它当内部列裁掉——那是错的，
# 内部列只有「…毛利率bp」与「数据来源」（店铺/款号透视）。
SHOP_SHEET_COLUMNS = (
    "店铺名称", "店铺编码", "处理意见", "货款分摊金额", "订单结算金额", "数量",
    "发货成本", "标准成本", "账单结算口径毛利", "账单结算口径毛利率",
    "订单结算口径毛利", "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)
PRODUCT_SHEET_COLUMNS = (
    "店铺名称", "店铺编码", "处理意见", "款号", "品牌", "年份", "季节", "吊牌价",
    "货款分摊金额", "订单结算金额", "数量", "发货成本", "标准成本",
    "账单结算口径毛利", "账单结算口径毛利率", "订单结算口径毛利",
    "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)
# 匹配透视基准列序（look_sku unicolumns，L1562/L2042）：店铺编码在第2列，
# 品类/年份在款号维度里，数据来源在末列。
MATCH_SHEET_COLUMNS = (
    "店铺名称", "店铺编码", "款号", "处理意见", "品类", "年份", "季节", "品牌",
    "吊牌价", "数量", "货款分摊金额", "结算金额", "标准成本", "发货成本", "数据来源",
)
# 收款/退款匹配基准列序（影刀10月全平台实测均 31 列，尾列=处理意见）。
# 2026-11-18 用户确认：数据来源/店铺编码/成本缺失原因 是插件内部字段
# （分摊分组/成本诊断用，保留在底稿 csv），不得漏进 Excel——原 spec B3
# 「附加列白名单」作废，明细与透视一样严格裁列。
DETAIL_SHEET_COLUMNS = (
    "账单周期", "订单编号", "订单日期", "货款金额", "货款分摊金额", "id", "单据类型",
    "存货异动时间", "单号", "sku", "款号", "商品名称", "品类", "年份", "季节", "品牌",
    "吊牌价", "数量", "店铺名称", "结算金额", "退款类型", "退款状态", "标准成本单价",
    "标准成本", "采购单价", "发货成本", "巨益单据匹配状态", "历史对账记录", "对账周期",
    "是否秒退判断", "处理意见",
)

# ------------------------------------------------------------------ cost 阶段列集
# spec C4/Q5：成本匹配阶段透视**无店铺编码**（13/18/14），明细 32=31+特殊情况说明。
# 2026-11-18 单元格级基准实测（probe_cost_cells/3/4）确认列头与列序。
# 常量定义了**必须接线**（11-14/11-17 两次同类缺陷教训），回归断言守着。
COST_SHOP_SHEET_COLUMNS = (
    "店铺名称", "处理意见", "货款分摊金额", "订单结算金额", "数量",
    "发货成本", "标准成本", "账单结算口径毛利", "账单结算口径毛利率",
    "订单结算口径毛利", "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)
COST_PRODUCT_SHEET_COLUMNS = (
    "店铺名称", "处理意见", "款号", "品牌", "年份", "季节", "吊牌价",
    "货款分摊金额", "订单结算金额", "数量", "发货成本", "标准成本",
    "账单结算口径毛利", "账单结算口径毛利率", "订单结算口径毛利",
    "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)
COST_MATCH_SHEET_COLUMNS = (
    "店铺名称", "款号", "处理意见", "品类", "年份", "季节", "品牌",
    "吊牌价", "数量", "货款分摊金额", "结算金额", "标准成本", "发货成本", "数据来源",
)
COST_DETAIL_SHEET_COLUMNS = DETAIL_SHEET_COLUMNS + ("特殊情况说明",)

# cost 阶段采购单价展示列走原始精度（A2），不经 _fen_to_yuan 量化。
# 内部计算列「采购单价」存分，出口用「采购单价_raw」覆盖展示。
from .cost import PURCHASE_RAW_COLUMN, SPECIAL_NOTE_COLUMN  # noqa: E402


def _excel_engine() -> str:
    """挑一个可用的 Excel 写引擎。

    2026-11-14 实测：本机运行时 Python 只有 openpyxl，**xlsxwriter 与 pyarrow/fastparquet
    都没装**，而原代码硬编码 engine="xlsxwriter" → 账单整理导出必然 ImportError 失败。
    财务机器上装什么不该由插件假设，所以这里按可用性回落：xlsxwriter → openpyxl。
    """
    import importlib.util

    for engine in ("xlsxwriter", "openpyxl"):
        if importlib.util.find_spec(engine):
            return engine
    raise ImportError(
        "缺少 Excel 写引擎：请安装 openpyxl 或 xlsxwriter（pip install openpyxl）"
    )


def _fen_to_yuan(df: pd.DataFrame, cols) -> pd.DataFrame:
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    out = df.copy()

    def _conv(v):
        # NaN/None 保持空白（基准里未匹配行吊牌价/采购单价、补录行货款金额是空白，
        # 不是 0）。from_fen(None) 会归 0，必须先拦。2026-11-17 影刀10月基准实测。
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "nat"):
            return None
        return float(from_fen(v))

    for c in cols:
        if c in out.columns:
            out[c] = out[c].map(_conv)
    return out


def build_result_dir(period: str, shop_code: str, platform: str) -> Path:
    """结果目录：{period}_{platform}_{shop_code}_{时间戳}（不覆盖历史）。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = lambda s: "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|')
    d = output_dir() / f"{safe(period)}_{safe(platform)}_{safe(shop_code)}_{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_match_result(
    save_dir: Path | str,
    *,
    file_name: str,
    pivot_shop: pd.DataFrame,
    pivot_product: pd.DataFrame,
    pivot_match: pd.DataFrame,
    receipt: pd.DataFrame,
    refund: pd.DataFrame,
    oms_matched: pd.DataFrame,
    chuanyun: pd.DataFrame | None = None,
) -> Path:
    """导出 6 sheet 对账结果（金额转元）。返回文件绝对路径。"""
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / file_name
    engine = _excel_engine()

    with pd.ExcelWriter(str(path), engine=engine) as writer:
        # 店铺/款号透视严格按基准列序裁列：内部基点列（…毛利率bp）与「店铺编码」
        # 「数据来源」不得漏进 Excel，否则财务逐列对照时列数与列序都对不上。
        _write(writer, pivot_shop, SHEET_SHOP, PIVOT_MONEY_COLUMNS, columns=SHOP_SHEET_COLUMNS)
        _write(writer, pivot_product, SHEET_PRODUCT, PIVOT_MONEY_COLUMNS, columns=PRODUCT_SHEET_COLUMNS)
        # 2026-11-17 影刀10月基准实测：匹配透视「结算金额」整列放大100倍。
        # 根因：这里误传 PIVOT_MONEY_COLUMNS（含「订单结算金额」、不含「结算金额」），
        # 定义好的 MATCH_PIVOT_MONEY_COLUMNS 从未接线——与 11-14 列序常量同类缺陷。
        _write(writer, pivot_match, SHEET_MATCH, MATCH_PIVOT_MONEY_COLUMNS, columns=MATCH_SHEET_COLUMNS)
        # 2026-11-18：明细同样按基准 31 列严格裁列（内部字段只留底稿 csv）。
        _write(writer, receipt, SHEET_RECEIPT, DETAIL_MONEY_COLUMNS, columns=DETAIL_SHEET_COLUMNS)
        _write(writer, refund, SHEET_REFUND, DETAIL_MONEY_COLUMNS, columns=DETAIL_SHEET_COLUMNS)
        _write(writer, oms_matched, SHEET_OMS, ())
        if chuanyun is not None:
            _write(writer, chuanyun, SHEET_CHUANYUN, ("金额",))
    return path


def _cost_detail_for_export(df: pd.DataFrame, *, tag_decimals: int = 4) -> pd.DataFrame:
    """cost 阶段明细出口变换（spec C2/A2/A5，2026-11-18 单元格级基准实测）：

    · 采购单价展示列 ← PURCHASE_RAW_COLUMN 原始精度（不经分→元量化）；
    · 吊牌价：分 → 元**文本**（基准是 '688.0000' 文本列；AKC 为 1 位小数），
      「无吊牌价」占位原样；
    · id 空值 → 空白单元格（基准实测 NoneType；A5 原记录「字面串 None」是
      pandas 读表假象，已修正）；
    · 其余空串/NaN 文本列 → None（空白），与基准一致。
    """
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    # 货款金额：补录/秒退未命中行**保持空白**（2026-11-18 单元格级基准实测：
    # HYK 收 11/退 16、KS001 7、DY020 3 行基准是 NoneType 空白，不是 0）。
    # 空值由 _fen_to_yuan 归 None → 空白单元格；不得 fillna(0)（会改变基准空态，
    # 退款侧全空白时合计 None vs 0.0 被回归误判 DIFF）。
    # 采购单价展示列 ← raw 原始精度（A2）在 _write(raw_overrides) 统一执行。
    if "吊牌价" in out.columns:
        def _tag(v):
            if v is None:
                return None
            if isinstance(v, str):
                s = v.strip()
                if not s or s.lower() in ("nan", "none", "nat"):
                    return None
                return s            # 「无吊牌价」等占位文本原样
            try:
                if pd.isna(v):
                    return None
            except (TypeError, ValueError):
                pass
            return f"{float(from_fen(v)):.{tag_decimals}f}"
        out["吊牌价"] = out["吊牌价"].map(_tag)
    if "id" in out.columns:
        out["id"] = out["id"].map(
            lambda v: None if v is None or (isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "nat")) else v)
    # 其余文本列：空串 → 空白（基准里 单号/历史对账记录/对账周期 等空值是 None）
    for c in out.columns:
        if c in ("吊牌价", "id", PURCHASE_RAW_COLUMN):
            continue
        if out[c].dtype == object:
            out[c] = out[c].map(
                lambda v: None if isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "nat") else v)
    return out


def _cost_pivot_tag_text(df: pd.DataFrame, *, tag_decimals: int = 4) -> pd.DataFrame:
    """cost 阶段透视出口：吊牌价列同明细口径转元文本（占位原样）。"""
    if df is None or df.empty or "吊牌价" not in df.columns:
        return df
    out = df.copy()

    def _tag(v):
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s and s.lower() not in ("nan", "none", "nat") else None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return f"{float(from_fen(v)):.{tag_decimals}f}"

    out["吊牌价"] = out["吊牌价"].map(_tag)
    return out


def export_cost_result(
    save_dir: Path | str,
    *,
    file_name: str,
    pivot_shop: pd.DataFrame,
    pivot_product: pd.DataFrame,
    pivot_match: pd.DataFrame,
    receipt: pd.DataFrame,
    refund: pd.DataFrame,
    oms_matched: pd.DataFrame,
    platform: str = "",
) -> Path:
    """导出 cost 阶段 6 sheet（spec C4）。列集 13/18/14 + 明细 32（含特殊情况说明）。

    与 export_match_result 的区别：
      · 透视无店铺编码（COST_*_SHEET_COLUMNS 严格裁列，常量已接线）；
      · 明细采购单价走原始精度、吊牌价元文本、id 空值空白（_cost_detail_for_export）；
      · 金额列转元时**排除采购单价**（已是元域 raw）与吊牌价（已文本化）。
    AKC 吊牌价文本 1 位小数（基准 '1118.0' 实测），其余平台 4 位（'688.0000'）。
    """
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / file_name
    tag_dec = 1 if platform == "akc" else 4
    detail_money = tuple(c for c in DETAIL_MONEY_COLUMNS if c not in ("采购单价", "吊牌价"))
    # 透视吊牌价已由 _cost_pivot_tag_text 转元文本，不能再过 _fen_to_yuan
    # （from_fen('688.0000') 会归 0 —— HYK 冒烟实测吊牌价合计归零的根因）。
    pivot_money = tuple(c for c in PIVOT_MONEY_COLUMNS if c != "吊牌价")
    match_pivot_money = tuple(c for c in MATCH_PIVOT_MONEY_COLUMNS if c != "吊牌价")

    rec = _cost_detail_for_export(receipt, tag_decimals=tag_dec)
    ref = _cost_detail_for_export(refund, tag_decimals=tag_dec)
    p_product = _cost_pivot_tag_text(pivot_product, tag_decimals=tag_dec)
    p_match = _cost_pivot_tag_text(pivot_match, tag_decimals=tag_dec)

    with pd.ExcelWriter(str(path), engine=_excel_engine()) as writer:
        _write(writer, pivot_shop, SHEET_SHOP, pivot_money, columns=COST_SHOP_SHEET_COLUMNS)
        _write(writer, p_product, SHEET_PRODUCT, pivot_money, columns=COST_PRODUCT_SHEET_COLUMNS)
        _write(writer, p_match, SHEET_MATCH, match_pivot_money, columns=COST_MATCH_SHEET_COLUMNS)
        _write(writer, rec, SHEET_RECEIPT, detail_money, columns=COST_DETAIL_SHEET_COLUMNS,
               raw_overrides={"采购单价": PURCHASE_RAW_COLUMN})
        _write(writer, ref, SHEET_REFUND, detail_money, columns=COST_DETAIL_SHEET_COLUMNS,
               raw_overrides={"采购单价": PURCHASE_RAW_COLUMN})
        _write(writer, oms_matched, SHEET_OMS, ())
    return path


def export_native_book(save_dir: Path | str, *, file_name: str, sheets: list[dict]) -> Path:
    """按影刀基准格式写一簿多 sheet（2026-11-16 基准对齐）。

    sheets: [{name, df, kind}]；kind='yuan' 原样写出（基准明细/透视，单位元），
    kind='fen' 时对收款/退款/净额等分列转元（插件统一口径 sheet）。
    sheet 名超 31 字符截断、重名加 '~'（原 clean_sheet_name 同口径）。
    """
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / file_name
    used: set[str] = set()
    with pd.ExcelWriter(str(path), engine=_excel_engine()) as writer:
        for spec in sheets or []:
            df = spec.get("df")
            name = str(spec.get("name") or "Sheet")[:31]
            while name in used:
                name = name[:-1] + "~"
            used.add(name)
            if df is None or df.empty:
                cols = list(df.columns) if df is not None else [name]
                pd.DataFrame(columns=cols).to_excel(writer, sheet_name=name, index=False)
                continue
            if spec.get("kind") == "fen":
                df = _fen_to_yuan(df, ("收款", "退款", "净额", "金额"))
            df.to_excel(writer, sheet_name=name, index=False)
    return path


def export_bill_summary(
    save_dir: Path | str,
    *,
    file_name: str,
    bill_pivot: pd.DataFrame,
    bill_detail: pd.DataFrame | None = None,
    chuanyun: pd.DataFrame | None = None,
) -> Path:
    """导出账单整理结果（原 *_账单整理.xlsx）。"""
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / file_name
    with pd.ExcelWriter(str(path), engine=_excel_engine()) as writer:
        if chuanyun is not None:
            _write(writer, chuanyun, SHEET_CHUANYUN, ("金额",))
        _write(writer, bill_pivot, "账单透视", ("收款", "退款", "现金流"))
        if bill_detail is not None:
            _write(writer, bill_detail, "账单明细", ("收款", "退款"))
    return path


def _write(writer, df: pd.DataFrame, sheet: str, money_cols, columns=None,
           raw_overrides: dict | None = None) -> None:
    """写一个 sheet。columns 给定时按其**严格裁列并重排**（对齐基准列序）。

    2026-11-14：SHOP_SHEET_COLUMNS / PRODUCT_SHEET_COLUMNS 定义后就没人用过，
    于是透视 df 里的内部列（「账单结算口径毛利率bp」等基点列、店铺编码）
    会原样漏进 Excel，财务看到的列比基准多出两列且夹在中间。
    """
    if df is None or df.empty:
        # 空表也保留真实列头（基准「已匹配的巨益单号」空表是 id/账单周期/数量 三列，
        # 不是单列占位）。2026-11-17 影刀10月基准实测。
        cols = list(columns) if columns else (list(df.columns) if df is not None and len(df.columns) else [sheet])
        pd.DataFrame(columns=cols).to_excel(writer, sheet_name=sheet[:31], index=False)
        return
    out = _fen_to_yuan(df, money_cols) if money_cols else df.copy()
    # raw_overrides：展示列 ← 内部原始精度列（cost 阶段采购单价，A2）。
    # 在裁列前覆盖，内部列（采购单价_raw）不会漏进 Excel。
    for dst, src in (raw_overrides or {}).items():
        if src in out.columns:
            out[dst] = out[src].map(lambda v: 0.0 if v is None else float(v))
    if columns:
        for c in columns:
            if c not in out.columns:
                out[c] = ""
        out = out.loc[:, list(columns)]
    out.to_excel(writer, sheet_name=sheet[:31], index=False)
