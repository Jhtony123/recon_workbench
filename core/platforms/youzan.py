"""有赞适配器（承接 bill_arr.yz_arr，约 150 行）。

关键口径：
  · 单源 csv 对账单明细（默认 utf-8，字段带 \\t 尾巴需 strip）
  · 「类型」→ type_dict 映射；充值且名称含「缴纳保证金」→ 店铺保证金；未命中 → 未匹配
  · 收款=收入(元)，退款=支出(元)（各自为正）；账期过滤用「入账时间」
  · 明细 9 列：类型/名称/实际业务类型/支付流水号/关联单号/业务单号/资金日期/收款/退款
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

REQUIRED_COLUMNS = ("类型", "名称", "支付流水号", "关联单号", "业务单号", "入账时间", "收入(元)", "支出(元)")

TYPE_DICT = {
    "订单入账": BIZ_GOODS, "退款": BIZ_GOODS,
    "交易手续费": "运费险", "手机验证收费": "账扣",
    "消费者保障服务费-放心购": "消费者保障服务费",
    "消费者保障服务费-退货包运费": "消费者保障服务费",
    "余额支付": "余额支付", "提现": "提现", "提现\t": "提现",
    "提现手续费-快速到账": "提现手续费", "充值": "充值",
    "佣金-分销员": "佣金",
}

DETAIL_COLUMNS = ("类型", "名称", "实际业务类型", "支付流水号", "关联单号", "业务单号",
                  "资金日期", "收款", "退款")

_STRIP_COLS = ("店铺名称", "类型", "名称", "交易来源地", "账务主体", "账户类型",
               "关联单号", "渠道", "下单时间", "入账时间")


class YouzanAdapter(BillAdapter):
    platform = "youzan"
    display_name = "有赞"
    bill_type = "有赞"
    file_patterns = (".csv",)
    # index_col=False 必须：有赞 csv 行尾多 2 个空字段（21 列头 vs 23 字段），
    # 默认推断会把前两列当索引导致整表错位（2026-11-16 zip 解压实测）。
    default_read_options = {"encoding": "utf-8", "index_col": False,
                            "dtype": {"支付流水号": "str", "关联单号": "str",
                                      "下单时间": "str", "入账时间": "str", "业务单号": "str"}}

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        out["biz_code"] = dict(TYPE_DICT)
        return out

    def detail_frame(self, ctx) -> pd.DataFrame:
        frames = self.read(ctx)
        df = pd.concat(frames, ignore_index=True)
        self.require_columns(df, REQUIRED_COLUMNS, self.display_name)
        for c in _STRIP_COLS:
            if c in df.columns:
                df[c] = df[c].astype("str").str.strip()
        df["资金日期"] = pd.to_datetime(df["入账时间"], errors="coerce").dt.floor("d")
        mapping = self.biz_code_map() or TYPE_DICT
        df["实际业务类型"] = df["类型"].map(lambda x: mapping.get(str(x).strip()))
        # 充值二次分类（原 cz_adj）
        mask = (df["实际业务类型"] == "充值") & df["名称"].astype("str").str.contains("缴纳保证金", na=False)
        df.loc[mask, "实际业务类型"] = "店铺保证金"
        df["实际业务类型"] = df["实际业务类型"].fillna("未匹配")
        df["收款"] = pd.to_numeric(df["收入(元)"], errors="coerce").fillna(0.0)
        df["退款"] = pd.to_numeric(df["支出(元)"], errors="coerce").fillna(0.0)
        if ctx.s_date and ctx.e_date:
            d = df["资金日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        out = df.loc[:, list(DETAIL_COLUMNS)].copy()
        out["资金日期"] = out["资金日期"].dt.strftime("%Y-%m-%d").fillna("")
        for c in ("支付流水号", "关联单号", "业务单号"):
            out[c] = out[c].astype("str").replace("nan", "")
        return out.reset_index(drop=True)

    def parse(self, ctx) -> list:
        detail = self.detail_frame(ctx)
        if detail.empty:
            return []
        std = pd.DataFrame()
        std[STD_ORDER_NO] = detail["关联单号"]
        std[STD_FUND_DATE] = detail["资金日期"]
        std[STD_BIZ_CATEGORY] = detail["实际业务类型"]
        std[STD_RECEIPT] = detail["收款"]
        std[STD_REFUND] = detail["退款"]
        return self.bill_rows(ctx, std)

    def export_books(self, ctx) -> list[dict]:
        detail = self.detail_frame(ctx)
        pivot = self.pivot_from_detail(detail, ctx, "有赞账单",
                                       category_col="实际业务类型", receipt_col="收款", refund_col="退款")
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "透视", "df": pivot, "kind": "yuan"},
                {"name": "有赞账单整理", "df": detail, "kind": "yuan"},
            ],
        }]
