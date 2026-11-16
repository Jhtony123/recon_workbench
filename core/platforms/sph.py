"""视频号适配器（承接 bill_arr.wechat_vd_arr，约 138 行）。

关键口径：
  · 单源 xlsx 资金流水；「记账时间」剥反引号后取日期，账期过滤用它
  · 动帐类型 → type_dict 映射业务类别，未命中 → 「未匹配」（显式暴露）
  · 收款=收支类型'收入'的收支金额，退款='支出'的收支金额（金额本身为正）
  · 明细 8 列：流水单号/资金日期/动帐类型/实际业务类型/收款/退款/关联订单号/详情
  · 无硬对平门；自检靠未匹配占比 + 透视合计 vs 原始收支合计
"""
from __future__ import annotations

import pandas as pd

from ..config import BIZ_GOODS
from .base import (
    STD_BIZ_CATEGORY,
    STD_FUND_DATE,
    STD_ORDER_NO,
    STD_RECEIPT,
    STD_REFUND,
    BillAdapter,
)

REQUIRED_COLUMNS = ("流水单号", "记账时间", "动帐类型", "收支类型", "收支金额", "关联订单号", "详情")

TYPE_DICT = {
    "订单结算": BIZ_GOODS, "订单支付": BIZ_GOODS, "订单退款": BIZ_GOODS,
    "订单交易｜先用后付": BIZ_GOODS,
    "运费险": "运费险", "技术服务费": "技术服务费",
    "回补极速退款垫资": BIZ_GOODS, "极速退款垫资": BIZ_GOODS,
    "达人佣金": "达人佣金", "提现": "提现", "充值": "充值",
    "平台现金券优惠": BIZ_GOODS, "平台优惠补贴": BIZ_GOODS,
    "平台赔付": "平台赔付", "服务充值账户充值": "服务充值",
}

DETAIL_COLUMNS = ("流水单号", "资金日期", "动帐类型", "实际业务类型", "收款", "退款", "关联订单号", "详情")


class SphAdapter(BillAdapter):
    platform = "sph"
    display_name = "视频号"
    bill_type = "视频号"
    file_patterns = (".xlsx",)
    default_read_options = {"dtype": {"关联订单号": "str", "流水单号": "str", "关联售后单号": "str"}}

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        out["biz_code"] = dict(TYPE_DICT)
        return out

    def detail_frame(self, ctx) -> pd.DataFrame:
        frames = self.read(ctx)
        df = pd.concat(frames, ignore_index=True)
        self.require_columns(df, REQUIRED_COLUMNS, self.display_name)
        df["记账时间"] = df["记账时间"].astype("str").map(str.strip).str.replace("`", "", regex=False)
        df["资金日期"] = pd.to_datetime(df["记账时间"], errors="coerce").dt.floor("d")
        mapping = self.biz_code_map() or TYPE_DICT
        df["实际业务类型"] = df["动帐类型"].map(lambda x: mapping.get(str(x).strip())).fillna("未匹配")
        amt = pd.to_numeric(df["收支金额"], errors="coerce").fillna(0.0)
        df["收款"] = amt.where(df["收支类型"] == "收入", 0.0)
        df["退款"] = amt.where(df["收支类型"] == "支出", 0.0)
        if ctx.s_date and ctx.e_date:
            d = df["资金日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        out = df.loc[:, list(DETAIL_COLUMNS)].copy()
        out["资金日期"] = out["资金日期"].dt.strftime("%Y-%m-%d").fillna("")
        for c in ("流水单号", "关联订单号", "详情"):
            out[c] = out[c].astype("str").replace("nan", "")
        return out.reset_index(drop=True)

    def parse(self, ctx) -> list:
        detail = self.detail_frame(ctx)
        if detail.empty:
            return []
        std = pd.DataFrame()
        std[STD_ORDER_NO] = detail["关联订单号"]
        std[STD_FUND_DATE] = detail["资金日期"]
        std[STD_BIZ_CATEGORY] = detail["实际业务类型"]
        std[STD_RECEIPT] = detail["收款"]
        std[STD_REFUND] = detail["退款"]
        return self.bill_rows(ctx, std)

    def export_books(self, ctx) -> list[dict]:
        detail = self.detail_frame(ctx)
        pivot = self.pivot_from_detail(detail, ctx, "视频号账单",
                                       category_col="实际业务类型", receipt_col="收款", refund_col="退款")
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "透视", "df": pivot, "kind": "yuan"},
                {"name": "视频号账单整理", "df": detail, "kind": "yuan"},
            ],
        }]
