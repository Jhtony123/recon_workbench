"""抖音适配器（承接 bill_arr.dy_arr，约 283 行）。

关键口径（从原脚本迁移，保持不变）：
  · 订单号/子订单号/商品ID/售后编号 首字符是制表符类前缀，需 slice 去掉
  · 销售货款 = 货款应结 + 订单实付应结 + 实际平台补贴_运费 + 实际平台补贴
              + 实际达人补贴 + 实际抖音支付补贴 + 实际抖音月付营销补贴
              + 以旧换新抵扣 + 银行补贴 + 政府补贴平台垫资
  · 销售货款 < 0 时：销售货款置 0，销售退款取销售货款（负数）
  · 备注含「普通退款转赔付」或「打款,订单号」→ 销售退款按动账方向取动账金额
  · 账期过滤用「动账时间」
  · 后期账单新增列（货款应结等）缺失时补 0，保证旧账单可跑

业务类别拆分（原 dy_arr 汇总表 = 各组件列求和，2026-08 真实账单逐分校验）：
  原脚本**不是**给每行打一个业务类别标签，而是先算出 11 个组件列，再按列求和
  拼成「汇总」sheet 的 10 个交易类型。本适配器按同一口径把一行原始账单**展开成
  多行标准账单**（每个非零组件一行），货款行喂 SKU 匹配，其余行只进汇总/氚云模板。
  校验依据（DY014，基准 *_账单整理.xlsx）：
    销售货款 1800.26 / 平台费用 -60.81 / 货款扣罚服务费 -10 / 提现 -10000
    → 汇总四行逐分一致，且「金额差异」列合计为 0。
"""
from __future__ import annotations

import pandas as pd

from ..config import BIZ_GOODS
from ..money import to_fen
from .base import (
    STD_BIZ_CATEGORY,
    STD_FUND_DATE,
    STD_ORDER_NO,
    STD_RECEIPT,
    STD_REFUND,
    BillAdapter,
    BillRow,
)

# 后期账单新增列，缺失时补 0（原 add_column_if_not_exists）
OPTIONAL_NUMERIC_COLUMNS = (
    "货款应结", "订单实付应结", "以旧换新抵扣",
    "政府补贴平台垫资", "银行补贴", "平台服务费", "渠道分成",
    "招商服务费", "站外推广费", "其他分成", "佣金", "服务商佣金",
)
OPTIONAL_TEXT_COLUMNS = ("商品名称", "达人ID", "达人名称")

# 销售货款的加项（原 dy_arr 的销售货款公式）
SALES_COMPONENTS = (
    "货款应结", "订单实付应结", "实际平台补贴_运费", "实际平台补贴",
    "实际达人补贴", "实际抖音支付补贴", "实际抖音月付营销补贴",
    "以旧换新抵扣", "银行补贴", "政府补贴平台垫资",
)

# 平台费用的加项（原 dy_arr：平台服务费+渠道分成+招商服务费+站外推广费+其他分成）
PLATFORM_FEE_COMPONENTS = (
    "平台服务费", "渠道分成", "招商服务费", "站外推广费", "其他分成",
)

# 平台佣金的加项（原 dy_arr：佣金+服务商佣金）
COMMISSION_COMPONENTS = ("佣金", "服务商佣金")

# 汇总表交易类型顺序（原 res_data['交易类型']）
SUMMARY_CATEGORIES: tuple[str, ...] = (
    BIZ_GOODS, "平台服务费", "运费险", "货款扣罚服务费", "账扣费用",
    "佣金", "罚款", "小额打款", "保证金", "提现",
)

# 汇总类别 → 组件列名（货款特殊：收款取销售货款、退款取 -销售退款）
CATEGORY_COMPONENT_COLUMN: dict[str, str] = {
    "平台服务费": "平台费用",
    "运费险": "运费险",
    "货款扣罚服务费": "货款扣罚服务费",
    "账扣费用": "账扣费用",
    "佣金": "平台佣金",
    "罚款": "平台罚款",
    "小额打款": "小额打款",
    "保证金": "保证金",
    "提现": "提现",
}

REQUIRED_COLUMNS = ("动账时间", "订单号", "动账方向", "动账金额", "订单退款", "备注")

INBOUND = "入账"

# 明细表输出列（原 res_df 的 48 列，「佣金」在源数据里本就重名两次，保持原样）
DETAIL_COLUMNS: tuple[str, ...] = (
    "动账时间", "动帐流水号", "动账方向", "动账金额", "动账账户", "计费类型",
    "子订单号", "订单号", "售后编号", "下单时间", "商品ID", "订单类型",
    "货款应结", "订单实付应结", "运费实付", "实际平台补贴_运费", "实际平台补贴",
    "以旧换新抵扣", "政府补贴平台垫资", "实际达人补贴", "实际抖音支付补贴",
    "实际抖音月付营销补贴", "银行补贴", "订单退款", "平台服务费", "佣金", "佣金",
    "服务商佣金", "渠道分成", "招商服务费", "站外推广费", "其他分成",
    "是否免佣", "免佣金额", "备注",
    "销售货款", "销售退款", "平台费用", "运费险", "货款扣罚服务费", "账扣费用",
    "平台佣金", "平台罚款", "小额打款", "保证金", "提现", "资金变动", "金额差异",
)

# 汇总表列（原 res_pivot 的最终列序）
SUMMARY_COLUMNS: tuple[str, ...] = (
    "店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流",
)


class DouyinAdapter(BillAdapter):
    platform = "douyin"
    display_name = "抖音"
    file_patterns = (".csv",)
    default_read_options = {
        "dtype": {
            "订单号": "str", "子订单号": "str", "售后编号": "str",
            "商品ID": "str", "备注": "str",
        }
    }

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        self.require_columns(out, list(REQUIRED_COLUMNS), self.display_name)

        # 原脚本用 str.slice_replace(0,1,'') 无条件去掉 ID 列首字符前缀。
        # 实测 2026-08 账单：订单号在 csv 里是 "'6955151008481678540"（Excel 文本保护用的
        # 单引号），也有 \t 前缀的历史账单。只切 \t 会把单引号留下，导致订单号撞不上 OMS，
        # 整店匹配 0 行。这里改为剥掉开头的引号/制表符（不做盲目 slice，避免切掉正常值）。
        for col in ("订单号", "子订单号", "商品ID", "售后编号"):
            if col in out.columns:
                out[col] = (
                    out[col].astype("string").fillna("")
                    .str.replace(r"^['\"\t]+", "", regex=True)
                    .str.strip()
                )

        for col in OPTIONAL_TEXT_COLUMNS:
            if col not in out.columns:
                out[col] = ""
        for col in OPTIONAL_NUMERIC_COLUMNS:
            if col not in out.columns:
                out[col] = 0.0

        out["备注"] = out["备注"].astype("string").fillna("")
        out["动账时间"] = pd.to_datetime(
            out["动账时间"].astype("string").fillna("").str.strip(), errors="coerce"
        ).dt.strftime("%Y-%m-%d").fillna("")

        for col in SALES_COMPONENTS + PLATFORM_FEE_COMPONENTS + COMMISSION_COMPONENTS + (
            "订单退款", "动账金额",
        ):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
            else:
                out[col] = 0.0

        return out

    # ---------------- 组件列（原 dy_arr 的 new_table 计算段）

    def _components(self, out: pd.DataFrame) -> pd.DataFrame:
        """算出原脚本的 11 个组件列（销售货款…提现），逐行、单位元。"""
        # 销售货款（各项补贴合计）
        out["销售货款"] = sum(out[c] for c in SALES_COMPONENTS)
        out["销售退款"] = out["订单退款"]
        out["平台费用"] = sum(out[c] for c in PLATFORM_FEE_COMPONENTS)
        out["平台佣金"] = sum(out[c] for c in COMMISSION_COMPONENTS)

        # 备注含「普通退款转赔付」或「打款,订单号」→ 按动账方向取动账金额
        note = out["备注"]
        special = note.str.contains("普通退款转赔付", na=False) | note.str.contains("打款,订单号", na=False)
        inbound = out["动账方向"] == INBOUND
        out.loc[special & inbound, "销售退款"] = out.loc[special & inbound, "动账金额"]
        out.loc[special & ~inbound, "销售退款"] = -out.loc[special & ~inbound, "动账金额"]

        # 销售货款 < 0：销售退款取销售货款（负数），销售货款置 0
        neg = out["销售货款"] < 0
        out.loc[neg, "销售退款"] = out.loc[neg, "销售货款"]
        out.loc[neg, "销售货款"] = 0.0

        # 备注关键字组件：罚款 / 小额打款 / 保证金 / 提现 / 运费险 / 货款扣罚服务费
        # 原脚本每个都是「关键字命中 → 入账取 +动账金额，出账取 -动账金额」，且各自有排除项
        amount = out["动账金额"]
        signed = amount.where(inbound, -amount)

        penalty_hit = (
            note.str.contains("赔付", na=False)
            & ~note.str.contains("普通退款转赔付", na=False)
            & ~note.str.contains("发货超时", na=False)
        ) | note.str.contains("催发", na=False) | note.str.contains("发票补偿金", na=False)
        out["平台罚款"] = signed.where(penalty_hit, 0.0)

        small_hit = note.str.contains("小额打款", na=False) & ~note.str.contains(
            "普通退款转赔付", na=False
        ) & ~note.str.contains("打款,订单号", na=False)
        out["小额打款"] = signed.where(small_hit, 0.0)

        out["保证金"] = signed.where(note.str.contains("保证金", na=False), 0.0)
        out["提现"] = signed.where(note.str.contains("提现", na=False), 0.0)
        out["运费险"] = signed.where(note.str.contains("保费扣除", na=False), 0.0)
        out["货款扣罚服务费"] = signed.where(note.str.contains("发货超时", na=False), 0.0)

        # 小计 → 账扣费用（原 bill_exp_：入账 = 动账金额 - |小计|，出账 = -动账金额 + |小计|）
        subtotal = (
            out["销售货款"] + out["销售退款"] + out["平台费用"] + out["平台罚款"]
            + out["小额打款"] + out["保证金"] + out["提现"] + out["平台佣金"]
            + out["运费险"] + out["货款扣罚服务费"]
        )
        out["小计"] = subtotal
        out["账扣费用"] = (amount - subtotal.abs()).where(inbound, -amount + subtotal.abs())
        out["资金变动"] = subtotal + out["账扣费用"]
        out["金额差异"] = (amount - out["资金变动"]).where(inbound, amount + out["资金变动"])
        return out

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._components(df.copy())

        order_no = out["订单号"].astype("string").fillna("")
        fund_date = out["动账时间"]
        trace = {c: out[c].values for c in ("__source_file__", "__source_row__") if c in out.columns}

        parts = [self._frame(order_no, fund_date, BIZ_GOODS,
                             out["销售货款"].clip(lower=0),
                             (-out["销售退款"]).clip(lower=0), trace)]

        # 非货款类目：原脚本汇总表只填「退款」列 = -组件合计（组件为负数 → 退款为正）
        for category, column in CATEGORY_COMPONENT_COLUMN.items():
            refund = -out[column]
            parts.append(self._frame(order_no, fund_date, category,
                                     pd.Series(0.0, index=out.index), refund, trace))

        res = pd.concat(parts, ignore_index=True)
        keep = (res[STD_RECEIPT] != 0) | (res[STD_REFUND] != 0)
        return res[keep].reset_index(drop=True)

    @staticmethod
    def _frame(order_no, fund_date, category, receipt, refund, trace) -> pd.DataFrame:
        res = pd.DataFrame()
        res[STD_ORDER_NO] = order_no.values
        res[STD_FUND_DATE] = fund_date.values
        res[STD_BIZ_CATEGORY] = category
        res[STD_RECEIPT] = receipt.values
        res[STD_REFUND] = refund.values
        for c, v in trace.items():
            res[c] = v
        return res

    # ---------------- 出口：明细组件列 + 汇总表

    def bill_rows(self, ctx, df: pd.DataFrame) -> list[BillRow]:
        """标准五元表 → BillRow（抖音：同一原始行展开成多个业务类别，各占一行）。"""
        return super().bill_rows(ctx, df)

    def component_frame(self, ctx) -> pd.DataFrame:
        """抖音明细表（原 res_df 的 48 列产物），供导出与「金额差异」自检。"""
        frames = [self.clean(f) for f in self.read(ctx)]
        if not frames:
            return pd.DataFrame()
        out = self._components(pd.concat(frames, ignore_index=True))
        out = out[
            (pd.to_datetime(out["动账时间"], errors="coerce") >= pd.Timestamp(ctx.s_date))
            & (pd.to_datetime(out["动账时间"], errors="coerce") <= pd.Timestamp(ctx.e_date))
        ] if (ctx.s_date and ctx.e_date) else out
        # 基准 res_df 的「佣金」列在源数据里本就重名两次（48 列），保持原样不去重，
        # 否则财务逐列对照时列数对不上（2026-11-16 基准对齐）。
        cols = [c for c in DETAIL_COLUMNS if c in out.columns]
        detail = out.loc[:, cols].copy()
        detail = detail.replace("nan", "")
        return detail

    def summary_frame(self, ctx) -> pd.DataFrame:
        """抖音汇总表（原 res_pivot：10 个交易类型 × 收款/退款/现金流，单位元）。"""
        detail = self.component_frame(ctx)
        if detail.empty:
            return pd.DataFrame(columns=list(SUMMARY_COLUMNS))
        revenue = float(detail["销售货款"].sum())
        values = {
            BIZ_GOODS: (revenue, -float(detail["销售退款"].sum())),
            "平台服务费": (0.0, -float(detail["平台费用"].sum())),
            "运费险": (0.0, -float(detail["运费险"].sum())),
            "货款扣罚服务费": (0.0, -float(detail["货款扣罚服务费"].sum())),
            "账扣费用": (0.0, -round(float(detail["账扣费用"].sum()), 2)),
            "佣金": (0.0, -float(detail["平台佣金"].sum())),
            "罚款": (0.0, -float(detail["平台罚款"].sum())),
            "小额打款": (0.0, -float(detail["小额打款"].sum())),
            "保证金": (0.0, -float(detail["保证金"].sum())),
            "提现": (0.0, -float(detail["提现"].sum())),
        }
        rows = []
        for category in SUMMARY_CATEGORIES:
            receipt, refund = values[category]
            if not (receipt or refund):
                continue  # 原脚本：透视表全 0 数据删除
            rows.append({
                "店铺名称": ctx.shop_name or ctx.shop_code,
                "店铺编码": ctx.shop_code,
                "账单周期": ctx.period_cn,
                "账单类型": "抖音账单",
                "业务类别": category,
                "收款": round(receipt, 2),
                "退款": round(refund, 2),
                "现金流": round(receipt - refund, 2),
            })
        return pd.DataFrame(rows, columns=list(SUMMARY_COLUMNS))

    def export_books(self, ctx) -> list[dict]:
        """基准三 sheet：氚云导入模板 / 汇总 / 抖音账单（48 列，金额元）。"""
        detail = self.component_frame(ctx)
        pivot = self.summary_frame(ctx)
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "汇总", "df": pivot, "kind": "yuan"},
                {"name": "抖音账单", "df": detail, "kind": "yuan"},
            ],
        }]
