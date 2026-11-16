"""好衣库适配器（承接 bill_arr.hyk_arr，约 238 行）。

关键口径：
  · 单源 csv（gb18030），**所有列名带反引号前缀**（`订单号），清洗剥掉
  · 组件列：销售货款=货款>=0 / 销售退款=货款<0（负数）/ 小额打款=售后补偿 /
    平台费用=16 项费用列之和；现金流=四者之和；差异=实际结算金额-现金流（自检列）
  · 透视固定 18 类（货款/小额打款/16 费用类），全 0 行删除
  · **硬门**：明细实际结算金额合计 == 透视现金流合计（round 2），不等原脚本 exit()
  · 账期过滤用「结算时间」→资金日期
  · 原脚本 bug：多文件时资金日期取自最后一个文件（data_files 而非 df）；
    插件按 df 自身列解析（有意修正，单文件场景无差异）
"""
from __future__ import annotations

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

# 16 项平台费用组件（顺序即透视类别顺序）
FEE_COMPONENTS: tuple[str, ...] = (
    "技术服务费", "运营服务费", "专享优惠", "信息增值服务费", "直播服务费",
    "推广活动费", "快递信息服务费", "营销费用", "佣金补偿", "优惠活动金额",
    "优惠券金额", "满减金额", "进口费用", "运费", "罚款金额", "赔付金额",
)

# 原始列（带 ` 前缀）→ 清洗后列名
_RENAME = {
    "`子订单号": "子订单号", "`订单号": "订单号", "`下单时间": "下单时间",
    "`商品数量": "商品数量", "`货号": "货号", "`条形码": "条形码",
    "`结算时间": "资金日期", "`实际结算金额（元）": "实际结算金额",
    "`货款（元）": "货款", "`技术服务费（元）": "技术服务费",
    "`运营服务费（元）": "运营服务费", "`信息增值服务费（元）": "信息增值服务费",
    "`专享优惠（元）": "专享优惠", "`直播服务费（元）": "直播服务费",
    "`推广活动费（元）": "推广活动费", "`快递信息服务费（元）": "快递信息服务费",
    "`营销费用（元）": "营销费用", "`佣金补偿（元）": "佣金补偿",
    "`优惠活动金额（元）": "优惠活动金额", "`优惠券金额（元）": "优惠券金额",
    "`满减金额（元）": "满减金额", "`进口费用": "进口费用",
    "`售后补偿（元）": "售后补偿", "`运费（元）": "运费",
    "`罚款金额（元）": "罚款金额", "`赔付金额（元）": "赔付金额",
    "`商品名称": "商品名称", "`商品规格": "商品规格", "`品牌名称": "品牌名称",
    "`入账模式": "入账模式", "`结算id": "结算id", "`流水创建时间": "流水创建时间",
    "`业务流水号": "业务流水号", "`结算账户": "结算账户", "`创建类型": "创建类型",
    "`结算状态": "结算状态", "`备注": "备注", "`扣费金额（元）": "扣费金额",
}

UNI_COLUMNS = (
    "子订单号", "订单号", "下单时间", "商品数量", "商品名称", "商品规格", "品牌名称", "货号",
    "条形码", "入账模式", "结算id", "流水创建时间", "资金日期", "业务流水号", "结算账户", "创建类型",
    "实际结算金额", "货款", "技术服务费", "运营服务费", "专享优惠", "信息增值服务费", "直播服务费",
    "推广活动费", "快递信息服务费", "营销费用", "佣金补偿", "优惠活动金额", "优惠券金额", "满减金额",
    "进口费用", "售后补偿", "运费", "罚款金额", "赔付金额", "扣费金额", "备注", "结算状态",
    "销售货款", "销售退款", "小额打款", "平台费用", "现金流", "差异",
)

_NUM_COLS = ("实际结算金额", "货款", "售后补偿", "扣费金额") + FEE_COMPONENTS


class HykAdapter(BillAdapter):
    platform = "hyk"
    display_name = "好衣库"
    bill_type = "好衣库"
    file_patterns = (".csv",)
    default_read_options = {"encoding": "gb18030", "dtype": "str"}

    def detail_frame(self, ctx) -> pd.DataFrame:
        frames = self.read(ctx)
        cleaned = []
        for d in frames:
            d = d.rename(columns=_RENAME)
            # 兜底：上游若去掉反引号前缀也能跑
            d.columns = [str(c).lstrip("`") for c in d.columns]
            # dtype='str' 读入是 StringDtype（不是 object），旧判断会整列跳过剥反引号
            # → 资金日期 '`2026-08-31…' 解析 NaT → 账期过滤后 0 行（2026-11-16 实证）。
            # 一律 astype(str) 后剥 `，不依赖 dtype 判断。
            for c in d.columns:
                d[c] = d[c].astype("str").str.replace("`", "", regex=False)
            cleaned.append(d)
        df = pd.concat(cleaned, ignore_index=True)
        self.require_columns(df, ("订单号", "资金日期", "实际结算金额", "货款") + FEE_COMPONENTS,
                             self.display_name)
        for c in _NUM_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        # 商品数量：dtype='str' 读入是文本，基准 Excel 里是数值（合计须可比）
        if "商品数量" in df.columns:
            df["商品数量"] = pd.to_numeric(df["商品数量"], errors="coerce").fillna(0).astype("int64")
        df["资金日期"] = pd.to_datetime(df["资金日期"], errors="coerce").dt.floor("d")
        df["销售货款"] = np.where(df["货款"] >= 0, df["货款"].round(2), 0.0)
        df["销售退款"] = np.where(df["货款"] < 0, df["货款"].round(2), 0.0)
        df["小额打款"] = df["售后补偿"].round(2)
        df["平台费用"] = sum(df[c] for c in FEE_COMPONENTS).round(2)
        df["现金流"] = (df["销售货款"] + df["销售退款"] + df["小额打款"] + df["平台费用"]).round(2)
        df["差异"] = (df["实际结算金额"] - df["现金流"]).round(2)
        if ctx.s_date and ctx.e_date:
            d = df["资金日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        for c in UNI_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        out = df.loc[:, list(UNI_COLUMNS)].copy()
        out["资金日期"] = out["资金日期"].dt.strftime("%Y-%m-%d").fillna("")
        return out.replace("nan", "").reset_index(drop=True)

    def _pivot(self, ctx, detail: pd.DataFrame) -> pd.DataFrame:
        revenue = float(detail["销售货款"].sum())
        sale_return = -float(detail["销售退款"].sum())
        compensation = -float(detail["小额打款"].sum())
        cats = ["货款", "小额打款"] + list(FEE_COMPONENTS)
        receipts = [revenue] + [0.0] * (len(cats) - 1)
        refunds = [sale_return, compensation] + [-float(detail[c].sum()) for c in FEE_COMPONENTS]
        pivot = pd.DataFrame({"业务类别": cats, "收款": receipts, "退款": refunds})
        pivot["现金流"] = pivot["收款"] - pivot["退款"]
        pivot["店铺名称"] = ctx.shop_name or ctx.shop_code
        pivot["店铺编码"] = ctx.shop_code
        pivot["账单周期"] = ctx.period_cn
        pivot["账单类型"] = "好衣库账单"
        pivot = pivot.loc[:, ["店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流"]]
        # 全 0 行删除（原脚本）
        pivot = pivot[(pivot[["收款", "退款", "现金流"]] != 0).any(axis=1)].reset_index(drop=True)
        # 硬门：账单净现金流 == 整理现金流
        bill_net = round(float(detail["实际结算金额"].sum()), 2)
        pivot_net = round(float(pivot["现金流"].sum()), 2)
        if round(bill_net - pivot_net, 2) != 0:
            raise BillParseError(
                "好衣库账单净现金流与整理现金流有差异",
                detail={"bill_net_yuan": bill_net, "pivot_net_yuan": pivot_net,
                        "diff_yuan": round(bill_net - pivot_net, 2),
                        "hint": "原脚本此处 exit()。先查明细「差异」列非 0 行（新费用列未进组件公式）"},
            )
        return pivot

    def parse(self, ctx) -> list:
        detail = self.detail_frame(ctx)
        if detail.empty:
            return []
        parts = []
        # 货款（收/退）
        s = pd.DataFrame()
        s[STD_ORDER_NO] = detail["子订单号"]
        s[STD_FUND_DATE] = detail["资金日期"]
        s[STD_BIZ_CATEGORY] = BIZ_GOODS
        s[STD_RECEIPT] = detail["销售货款"]
        s[STD_REFUND] = detail["销售退款"].abs()
        parts.append(s)
        # 小额打款 + 16 费用类（组件展开，非零行）
        for col, cat in (("售后补偿", "小额打款"),) + tuple((c, c) for c in FEE_COMPONENTS):
            sub = detail[detail[col] != 0]
            if sub.empty:
                continue
            s = pd.DataFrame()
            s[STD_ORDER_NO] = sub["子订单号"]
            s[STD_FUND_DATE] = sub["资金日期"]
            s[STD_BIZ_CATEGORY] = cat
            s[STD_RECEIPT] = 0.0
            s[STD_REFUND] = sub[col].abs()
            parts.append(s)
        return self.bill_rows(ctx, pd.concat(parts, ignore_index=True))

    def export_books(self, ctx) -> list[dict]:
        detail = self.detail_frame(ctx)
        pivot = self._pivot(ctx, detail)
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "好衣库账单透视", "df": pivot, "kind": "yuan"},
                {"name": "好衣库账单整理", "df": detail, "kind": "yuan"},
            ],
        }]
