"""小红书适配器（承接 bill_arr.xhs_arr 结算单口径，约 176 行）。

⚠️ 基准（8 月氚云导入成功文件）用的是 **xhs_arr 结算单**口径，不是 xhs_daily 日账单：
  sheet = 氚云导入模板 / 小红书账单透视 / 小红书账单整理 / 小红书其他费用整理。

关键口径：
  · 单源 xlsx，双 sheet：「订单结算」+「其他服务款」（「总览」忽略）
  · 订单结算：货款 = 商品实付/实退 + 平台优惠补贴（**不含政府券**，与日账单口径不同）；
    交易类型'结算入账'→货款收款，'退款'→货款退款（保留负号）；
    金额差异 = 动账金额 - 货款收款 - 货款退款 - 平台佣金 - 买手佣金（自检列）
  · 透视固定三类：货款 / 平台佣金 / 买手佣金（退款侧取 abs）
  · 其他服务款：业务类别=业务款项原文；收款=动账金额>=0，退款=<0（负数）；
    透视 groupby 后退款取 abs；**其他服务款不做账期过滤前的透视合并顺序**：
    先过滤再透视（原脚本如此）
  · 账期过滤用「结算时间」
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from ..config import BIZ_GOODS
from ..errors import BillParseError
from .base import (
    STD_BIZ_CATEGORY,
    STD_FUND_DATE,
    STD_ORDER_NO,
    STD_RECEIPT,
    STD_REFUND,
    BillAdapter,
)

SALE_COLUMNS = ("资金日期", "订单编号", "售后单号", "下单时间", "完成时间", "结算时间",
                "数据来源", "动账金额", "货款收款", "货款退款", "平台佣金", "买手佣金", "金额差异")
EXPENSE_COLUMNS = ("资金日期", "订单编号", "业务类别", "业务单号", "结算时间",
                   "数据来源", "动账金额", "收款", "退款", "备注")


class XhsAdapter(BillAdapter):
    platform = "xhs"
    display_name = "小红书"
    bill_type = "小红书结算单"
    file_patterns = (".xlsx",)
    default_read_options = {}
    # 原 xhs_arr 账期内无数据也导出全零透视簿（XHS002 8月基准实证：
    # 结算时间全在 7 月 → 明细 0 行，但透视仍有 货款/平台佣金/买手佣金 三行全 0）。
    export_when_empty = True

    def _read_sheet(self, ctx, sheet: str, dtype: dict) -> pd.DataFrame:
        files = self.collect_files(ctx)
        if not files:
            raise BillParseError("未找到小红书结算单文件",
                                 detail={"searched": list(ctx.file_paths or []),
                                         "hint": "小红书是 xlsx 结算单（含 订单结算/其他服务款 两个 sheet）"})
        frames = []
        for f in files:
            try:
                d = pd.read_excel(f, index_col=None, sheet_name=sheet, dtype=dtype)
            except ValueError:
                continue  # 该文件没有此 sheet（原脚本同口径：跳过）
            d["__source_file__"] = os.path.basename(f)
            frames.append(d)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _sale_frame(self, ctx) -> pd.DataFrame:
        df = self._read_sheet(ctx, "订单结算",
                              {"结算时间": "str", "下单时间": "str", "完成时间": "str",
                               "订单号": "str", "售后单号": "str"})
        if df.empty:
            return pd.DataFrame(columns=list(SALE_COLUMNS))
        self.require_columns(df, ("结算时间", "交易类型", "动账金额", "商品实付/实退",
                                  "平台优惠补贴", "平台佣金", "买手佣金", "订单号"), "小红书订单结算")
        df["资金日期"] = pd.to_datetime(df["结算时间"].astype("str").str.strip(), errors="coerce").dt.floor("d")
        df["数据来源"] = "订单结算"
        df["订单编号"] = df["订单号"]
        for c in ("商品实付/实退", "平台优惠补贴", "平台佣金", "买手佣金", "动账金额"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        df["货款"] = df["商品实付/实退"] + df["平台优惠补贴"]
        df["货款收款"] = np.where(df["交易类型"] == "结算入账", df["货款"], 0.0)
        df["货款退款"] = np.where(df["交易类型"] == "退款", df["货款"], 0.0)
        df["金额差异"] = df["动账金额"] - df["货款收款"] - df["货款退款"] - df["平台佣金"] - df["买手佣金"]
        if ctx.s_date and ctx.e_date:
            d = df["资金日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        out = df.loc[:, list(SALE_COLUMNS)].copy()
        out["资金日期"] = out["资金日期"].dt.strftime("%Y-%m-%d").fillna("")
        return out.replace("nan", "").reset_index(drop=True)

    def _expense_frame(self, ctx) -> pd.DataFrame:
        df = self._read_sheet(ctx, "其他服务款", {"结算时间": "str", "订单号": "str"})
        if df.empty:
            return pd.DataFrame(columns=list(EXPENSE_COLUMNS))
        self.require_columns(df, ("结算时间", "业务款项", "业务单号", "动账金额", "订单号"), "小红书其他服务款")
        df["资金日期"] = pd.to_datetime(df["结算时间"].astype("str").str.strip(), errors="coerce").dt.floor("d")
        df["动账金额"] = pd.to_numeric(df["动账金额"], errors="coerce").fillna(0.0)
        df["收款"] = np.where(df["动账金额"] >= 0, df["动账金额"], 0.0)
        df["退款"] = np.where(df["动账金额"] < 0, df["动账金额"], 0.0)
        df["业务类别"] = df["业务款项"]
        df["数据来源"] = "其他服务费"
        df["订单编号"] = df["订单号"]
        if "备注" not in df.columns:
            df["备注"] = ""
        if ctx.s_date and ctx.e_date:
            d = df["资金日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        out = df.loc[:, list(EXPENSE_COLUMNS)].copy()
        out["资金日期"] = out["资金日期"].dt.strftime("%Y-%m-%d").fillna("")
        return out.replace("nan", "").reset_index(drop=True)

    def _pivot(self, ctx, sale: pd.DataFrame, expense: pd.DataFrame) -> pd.DataFrame:
        revenue = float(sale["货款收款"].sum()) if not sale.empty else 0.0
        sale_return = abs(float(sale["货款退款"].sum())) if not sale.empty else 0.0
        pc = abs(float(sale["平台佣金"].sum())) if not sale.empty else 0.0
        bc = abs(float(sale["买手佣金"].sum())) if not sale.empty else 0.0
        sales_pivot = pd.DataFrame({
            "业务类别": ["货款", "平台佣金", "买手佣金"],
            "收款": [revenue, 0.0, 0.0],
            "退款": [sale_return, pc, bc],
        })
        sales_pivot["现金流"] = sales_pivot["收款"] - sales_pivot["退款"]
        if not expense.empty:
            g = expense.groupby("业务类别", as_index=False, sort=True)[["收款", "退款"]].sum()
            g["退款"] = g["退款"].abs()
            g["现金流"] = g["收款"] - g["退款"]
        else:
            g = pd.DataFrame(columns=["业务类别", "收款", "退款", "现金流"])
        pivot = pd.concat([sales_pivot, g], ignore_index=True)
        pivot["店铺名称"] = ctx.shop_name or ctx.shop_code
        pivot["店铺编码"] = ctx.shop_code
        pivot["账单周期"] = ctx.period_cn
        pivot["账单类型"] = "小红书结算单"
        return pivot.loc[:, ["店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流"]]

    def parse(self, ctx) -> list:
        sale = self._sale_frame(ctx)
        expense = self._expense_frame(ctx)
        parts = []
        if not sale.empty:
            # 货款行：收款/退款拆开（退款原为负 → 取 abs 进统一口径）
            s = pd.DataFrame()
            s[STD_ORDER_NO] = sale["订单编号"]
            s[STD_FUND_DATE] = sale["资金日期"]
            s[STD_BIZ_CATEGORY] = BIZ_GOODS
            s[STD_RECEIPT] = sale["货款收款"]
            s[STD_REFUND] = sale["货款退款"].abs()
            parts.append(s)
            for col, cat in (("平台佣金", "平台佣金"), ("买手佣金", "买手佣金")):
                sub = sale[sale[col] != 0]
                if sub.empty:
                    continue
                s = pd.DataFrame()
                s[STD_ORDER_NO] = sub["订单编号"]
                s[STD_FUND_DATE] = sub["资金日期"]
                s[STD_BIZ_CATEGORY] = cat
                s[STD_RECEIPT] = 0.0
                s[STD_REFUND] = sub[col].abs()
                parts.append(s)
        if not expense.empty:
            s = pd.DataFrame()
            s[STD_ORDER_NO] = expense["订单编号"]
            s[STD_FUND_DATE] = expense["资金日期"]
            s[STD_BIZ_CATEGORY] = expense["业务类别"]
            s[STD_RECEIPT] = expense["收款"]
            s[STD_REFUND] = expense["退款"].abs()
            parts.append(s)
        if not parts:
            return []
        return self.bill_rows(ctx, pd.concat(parts, ignore_index=True))

    def export_books(self, ctx) -> list[dict]:
        sale = self._sale_frame(ctx)
        expense = self._expense_frame(ctx)
        pivot = self._pivot(ctx, sale, expense)
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "小红书账单透视", "df": pivot, "kind": "yuan"},
                {"name": "小红书账单整理", "df": sale, "kind": "yuan"},
                {"name": "小红书其他费用整理", "df": expense, "kind": "yuan"},
            ],
        }]
