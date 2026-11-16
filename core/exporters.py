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

# 基准文件（影刀 cost_match 产出）的列序，导出时严格对齐，便于财务逐列对照
SHOP_SHEET_COLUMNS = (
    "店铺名称", "处理意见", "货款分摊金额", "订单结算金额", "数量", "发货成本",
    "标准成本", "账单结算口径毛利", "账单结算口径毛利率", "订单结算口径毛利",
    "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)
PRODUCT_SHEET_COLUMNS = (
    "店铺名称", "处理意见", "款号", "品牌", "年份", "季节", "吊牌价",
    "货款分摊金额", "订单结算金额", "数量", "发货成本", "标准成本",
    "账单结算口径毛利", "账单结算口径毛利率", "订单结算口径毛利",
    "订单结算口径毛利率", "冲发出商品数量", "冲发出商品",
)


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
    for c in cols:
        if c in out.columns:
            out[c] = out[c].map(lambda v: float(from_fen(v)))
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
        _write(writer, pivot_match, SHEET_MATCH, PIVOT_MONEY_COLUMNS)
        _write(writer, receipt, SHEET_RECEIPT, DETAIL_MONEY_COLUMNS)
        _write(writer, refund, SHEET_REFUND, DETAIL_MONEY_COLUMNS)
        _write(writer, oms_matched, SHEET_OMS, ())
        if chuanyun is not None:
            _write(writer, chuanyun, SHEET_CHUANYUN, ("金额",))
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


def _write(writer, df: pd.DataFrame, sheet: str, money_cols, columns=None) -> None:
    """写一个 sheet。columns 给定时按其**严格裁列并重排**（对齐基准列序）。

    2026-11-14：SHOP_SHEET_COLUMNS / PRODUCT_SHEET_COLUMNS 定义后就没人用过，
    于是透视 df 里的内部列（「账单结算口径毛利率bp」等基点列、店铺编码）
    会原样漏进 Excel，财务看到的列比基准多出两列且夹在中间。
    """
    if df is None or df.empty:
        pd.DataFrame({sheet: []}).to_excel(writer, sheet_name=sheet[:31], index=False)
        return
    out = _fen_to_yuan(df, money_cols) if money_cols else df.copy()
    if columns:
        for c in columns:
            if c not in out.columns:
                out[c] = ""
        out = out.loc[:, list(columns)]
    out.to_excel(writer, sheet_name=sheet[:31], index=False)
