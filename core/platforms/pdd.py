"""拼多多适配器（承接 bill_arr.pdd_arr，约 157 行）。

关键口径：
  · 业务描述按 | 分割取首段为业务编码 → transaction_dict 映射业务类别
  · 商户订单号：备注里能匹配到「6位-15位」动态单号时用该单号（原 sepcial_type_adj）
  · 资金发生日期来自「发生时间」
  · 收款 = 收入金额（+元），退款 = -支出金额（-元）
  · 含「#支出合计」的汇总尾行需去掉
"""
from __future__ import annotations

import re

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

REQUIRED_COLUMNS = ("商户订单号", "发生时间", "业务描述", "收入金额（+元）", "支出金额（-元）", "备注")

# 备注里的动态单号：6位-15位（原 sepcial_type_adj 的正则）
DYNAMIC_ORDER_RE = re.compile(r".*?(\d{6}-\d{15}).*?")

UNMATCHED = "未匹配"


class PddAdapter(BillAdapter):
    platform = "pdd"
    display_name = "拼多多"
    file_patterns = (".csv",)
    default_read_options = {"encoding": "gb18030", "skiprows": 4}

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        out["biz_code"] = dict(PDD_BIZ_CODE_FALLBACK)
        return out

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        self.require_columns(out, list(REQUIRED_COLUMNS), self.display_name)

        # 去掉含「#支出合计」的汇总尾行（原脚本 drop tail 4）
        hit = out.apply(lambda r: r.astype(str).str.contains("#支出合计").any(), axis=1)
        if hit.any():
            idx = out.index[hit][0]
            out = out.loc[:idx].iloc[:-1] if idx > 0 else out.iloc[0:0]

        out["备注"] = (
            out["备注"].astype("string").fillna("")
            .str.replace("（", "(", regex=False)
            .str.replace("）", ")", regex=False)
        )
        out["商户订单号"] = out["商户订单号"].astype("string").fillna("").str.strip()

        parts = out["业务描述"].astype("string").fillna("").str.split("|", expand=True)
        out["业务编码"] = parts[0].str.strip() if 0 in parts.columns else ""

        out["资金发生日期"] = pd.to_datetime(
            out["发生时间"].astype("string").fillna("").str.strip(), errors="coerce"
        ).dt.strftime("%Y-%m-%d").fillna("")

        out["收入金额（+元）"] = pd.to_numeric(out["收入金额（+元）"], errors="coerce").fillna(0.0)
        out["支出金额（-元）"] = pd.to_numeric(out["支出金额（-元）"], errors="coerce").fillna(0.0)
        return out

    def _prepared(self, df: pd.DataFrame) -> pd.DataFrame:
        """清洗后的公共加工：业务类别 / 线上订单号 / 收款 / 退款（元）。

        normalize()（统一五元）与 detail_frame()（基准 8 列明细）共用，
        保证两条出口的分类与单号口径完全一致。
        """
        out = df.copy()

        mapping = self.biz_code_map() or PDD_BIZ_CODE_FALLBACK
        out[STD_BIZ_CATEGORY] = (
            out["业务编码"].map(lambda x: mapping.get(str(x).strip(), UNMATCHED)).fillna(UNMATCHED)
        )

        # 备注里的动态单号优先（原 sepcial_type_adj）
        def pick_order(row) -> str:
            note = str(row.get("备注") or "")
            m = DYNAMIC_ORDER_RE.search(note)
            if m:
                return m.group(1)
            return str(row.get("商户订单号") or "").replace("nan", "").strip()

        out["线上订单号"] = out.apply(pick_order, axis=1).astype("string").fillna("")
        out[STD_RECEIPT] = out["收入金额（+元）"]
        # 基准明细「退款」= -支出金额（支出列本身为负 → 退款为正），与五元口径同值
        out[STD_REFUND] = out["支出金额（-元）"].abs()
        return out

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._prepared(df)
        res = pd.DataFrame()
        res[STD_ORDER_NO] = out["线上订单号"]
        res[STD_FUND_DATE] = out["资金发生日期"]
        res[STD_BIZ_CATEGORY] = out[STD_BIZ_CATEGORY]
        res[STD_RECEIPT] = out[STD_RECEIPT]
        res[STD_REFUND] = out[STD_REFUND]

        for c in ("__source_file__", "__source_row__"):
            if c in out.columns:
                res[c] = out[c].values
        return res

    # ---------------- 基准格式出口（原 pdd_arr 的 res_df / res_pivot）

    #: 基准「拼多多账单」sheet 的 8 列
    BASE_DETAIL_COLUMNS = ("线上订单号", "资金发生日期", "账务类型", "业务类别",
                           "收款", "退款", "备注", "业务描述")

    def detail_frame(self, ctx) -> pd.DataFrame:
        frames = [self.clean(f) for f in self.read(ctx)]
        if not frames:
            return pd.DataFrame(columns=list(self.BASE_DETAIL_COLUMNS))
        out = self._prepared(pd.concat(frames, ignore_index=True))
        if ctx.s_date and ctx.e_date:
            d = pd.to_datetime(out["资金发生日期"], errors="coerce")
            out = out[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        for c in self.BASE_DETAIL_COLUMNS:
            if c not in out.columns:
                out[c] = ""
        detail = out.loc[:, list(self.BASE_DETAIL_COLUMNS)].copy()
        # 基准口径：退款 = -支出金额（不取 abs；支出列出现正值时保持负退款，与原脚本一致）
        detail["退款"] = -out["支出金额（-元）"]
        detail["线上订单号"] = detail["线上订单号"].astype(str).str.replace("nan", "", regex=False)
        detail["备注"] = detail["备注"].astype(str).replace("nan", "")
        return detail.reset_index(drop=True)

    def export_books(self, ctx) -> list[dict]:
        """基准三 sheet：氚云导入模板 / 拼多多账单透视 / 拼多多账单（金额元）。"""
        detail = self.detail_frame(ctx)
        pivot = self.pivot_from_detail(detail, ctx, "拼多多账单")
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "拼多多账单透视", "df": pivot, "kind": "yuan"},
                {"name": "拼多多账单", "df": detail, "kind": "yuan"},
            ],
        }]


# 业务编码兜底表（原 pdd_arr 的 transaction_dict；规则表可覆盖）
PDD_BIZ_CODE_FALLBACK: dict[str, str] = {
    "0010002": BIZ_GOODS, "0010005": BIZ_GOODS,
    "0020002": BIZ_GOODS, "0020005": BIZ_GOODS,
    "0030001": "技术服务费", "0030002": "技术服务费", "0030003": "技术服务费",
    "0030005": "技术服务费", "0030011": "技术服务费", "0030015": "技术服务费",
    "0040001": "小额打款", "0130005": "小额打款",
    "0040002": "售后费用", "0040003": "售后费用", "0040004": "售后费用",
    "0040005": "售后费用", "0040006": "售后费用", "0040007": "售后费用",
    "0140002": "售后费用",
    "0040008": "账扣",
    "0050002": "消费者体验提升计划",
    "0060001": "多多进宝",
    "0060002": "跨店满返",
    "0070001": "店铺保证金",
    "0070003": "推广充值", "0070004": "推广充值",
    "0080001": "提现",
    "0090001": "补贴",
    "0110001": "服务消费",
}
