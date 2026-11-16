"""快手适配器（承接 bill_arr.ks_arr，约 419 行）。

关键口径：
  · 双源：资金账单 csv（gb18030）+ 订单流水 xlsx（「订单」子目录或 extra_files.order）
  · 资金账单先按 业务类型 type_dict + 描述关键字链分类，账期过滤用「入账时间」
  · **硬门**：资金账单「交易货款」净现金流 == 订单流水净现金流（分位），
    不等 → 原脚本导出两 sheet 后 exit()；插件抛 BillParseError（带差额诊断）
  · 对平后：账单剔除交易货款，货款部分改用**订单流水**按 10 个组件列
    各自 pivot（线上订单号+实际结算时间）展开 —— 与抖音类似的组件展开，
    但快手组件来自订单流水而非资金账单
  · 收款=账务方向'收'的发生额，退款='支'的发生额（发生额本身为正）
"""
from __future__ import annotations

import glob
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

REQUIRED_COLUMNS = ("关联业务单号", "入账时间", "账务方向", "发生额（元）", "业务类型", "描述")

TYPE_DICT = {"货款结算": "交易货款", "结算后退款": "交易货款", "余额提现": "提现"}

# 描述关键字链（顺序即口径，原 note_find）
NOTE_RULES: tuple[tuple[str, str], ...] = (
    ("退货补运费", "运费险"),
    ("集运扣款", "物流费"),
    ("商责", "订单赔付"),
    ("退款-快手电商-", "小额打款"),
    ("快手小店退款", "交易货款"),
    ("分账追回-超售后期-平台服务费", "技术服务费"),
    ("分账追回-超售后期-cps推广者三方分佣户", "推广佣金"),
    ("快手商品赔付-现金补偿", "小额打款"),
    ("发货异常用户体验赔付", "订单赔付"),
    ("小额打款", "小额打款"),
    ("违约金扣款", "违约金"),
    ("违约金罚单扣款", "违约金"),
)

UNICOL = ["线上订单号", "资金发生日期", "业务类别", "收款", "退款"]

# 订单流水组件列：(原始列, 组件名, 方向)  方向 receipt=收款 / refund=退款
# 组件名即明细列名（原 df_order['货款退款'] 独立列）；业务类别另映射（货款退款→货款）
ORDER_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("合计收入(元)", "货款", "receipt"),
    ("订单退款(元)", "货款退款", "refund"),
    ("支付营销回退（元）", "营销费用", "receipt"),
    ("技术服务费(元)", "技术服务费", "refund"),
    ("预售增收技术服务费（元）", "预售技术服务费", "refund"),
    ("达人佣金(元)", "达人佣金", "refund"),
    ("团长佣金(元)", "团长佣金", "refund"),
    ("快赚客佣金(元)", "快赚客佣金", "refund"),
    ("服务商佣金(元)", "服务商佣金", "refund"),
    ("其他收费", "其他收费", "refund"),
)


class KuaishouAdapter(BillAdapter):
    platform = "kuaishou"
    display_name = "快手"
    bill_type = "快手"
    file_patterns = (".csv",)
    default_read_options = {"encoding": "gb18030",
                            "dtype": {"账务流水号": "str", "关联业务单号": "str",
                                      "入账时间": "str", "备注": "str"}}
    needs_extra_files = ("order",)

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        # 描述关键字链 NOTE_RULES 顺序即口径，留在代码（改序必回归），不进规则表
        out["biz_code"] = dict(TYPE_DICT)
        return out

    # ---------------- 资金账单

    def _bill_frame(self, ctx) -> pd.DataFrame:
        frames = self.read(ctx)
        df = pd.concat(frames, ignore_index=True)
        self.require_columns(df, REQUIRED_COLUMNS, self.display_name)
        # StringDtype 下 NaN 仍是 float，.map(str.strip) 会 TypeError（2026-11-16 实证）
        df["关联业务单号"] = df["关联业务单号"].astype("str").str.strip().replace("nan", "")
        mapping = self.biz_code_map() or TYPE_DICT
        df["业务类别"] = df["业务类型"].map(lambda x: mapping.get(str(x).strip()))

        def note_find(note, c_type):
            if pd.isna(note) or not isinstance(note, str):
                return c_type
            for kw, cat in NOTE_RULES:
                if kw in note:
                    return cat
            return c_type

        df["业务类别"] = [note_find(n, c) for n, c in zip(df["描述"], df["业务类别"])]
        df["业务类别"] = df["业务类别"].fillna("未匹配")
        df["资金发生日期"] = pd.to_datetime(df["入账时间"], errors="coerce").dt.floor("d")
        df["线上订单号"] = df["关联业务单号"].replace("nan", "")
        amt = pd.to_numeric(df["发生额（元）"], errors="coerce").fillna(0.0)
        df["收款"] = np.where(df["账务方向"] == "收", amt, 0.0)
        df["退款"] = np.where(df["账务方向"] == "支", amt, 0.0)
        if ctx.s_date and ctx.e_date:
            d = df["资金发生日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        return df.loc[:, UNICOL].reset_index(drop=True)

    # ---------------- 订单流水

    def _order_files(self, ctx) -> list[str]:
        files: list[str] = []
        # 附加源支持原始下载 zip（2026-11-16 用户需求）
        for p in self.expand_source_paths(ctx.extra_files.get("order") or []):
            p = str(p).strip().strip('"')
            if os.path.isdir(p):
                files.extend(glob.glob(os.path.join(p, "*.xlsx")))
                files.extend(glob.glob(os.path.join(p, "**", "*.xlsx"), recursive=True))
            elif os.path.isfile(p):
                files.append(p)
        if not files:
            # 自动发现：主账单目录下的「订单」子目录（影刀目录约定）
            for p in (ctx.file_paths or []):
                p = str(p).strip().strip('"')
                base = p if os.path.isdir(p) else os.path.dirname(p)
                for sub in ("订单", "订单流水"):
                    d = os.path.join(base, sub)
                    if os.path.isdir(d):
                        files.extend(glob.glob(os.path.join(d, "*.xlsx")))
        return [f for f in sorted(set(files)) if not os.path.basename(f).startswith("~$")]

    def _order_frame(self, ctx) -> pd.DataFrame:
        files = self._order_files(ctx)
        if not files:
            raise BillParseError(
                "快手缺少订单流水文件",
                detail={"hint": "快手是双源平台：资金账单 csv + 订单流水 xlsx。"
                               "请把「订单」子目录一并给出，或 extra_files={\"order\":[路径]}"},
            )
        frames = []
        for f in files:
            d = pd.read_excel(f, index_col=None, dtype={"订单号": "str", "实际结算时间": "str"})
            d["线上订单号"] = d["订单号"].astype("str").map(str.strip)
            d["资金发生日期"] = d["实际结算时间"]
            for raw, comp, _dir in ORDER_COMPONENTS:
                if raw not in d.columns:
                    raise BillParseError(
                        f"快手订单流水缺少组件列：{raw}",
                        detail={"file": os.path.basename(f),
                                "available_columns": [str(c) for c in d.columns][:50],
                                "hint": "上游新增/改名组件列时需同步 ORDER_COMPONENTS（见 platform-kuaishou.md）"},
                    )
                d[comp] = pd.to_numeric(d[raw], errors="coerce").fillna(0.0)
            frames.append(d)
        return pd.concat(frames, ignore_index=True)

    # ---------------- 合并（含硬门）

    def detail_frame(self, ctx) -> pd.DataFrame:
        bill = self._bill_frame(ctx)
        order = self._order_frame(ctx)

        sale = bill[bill["业务类别"] == "交易货款"]
        net_sale_cf = round(float(sale["收款"].sum()) - float(sale["退款"].sum()), 2)
        order_net_cf = round(float(
            order["货款"].sum() - order["货款退款"].sum() + order["营销费用"].sum()
            - order["技术服务费"].sum() - order["预售技术服务费"].sum()
            - order["达人佣金"].sum() - order["团长佣金"].sum()
            - order["快赚客佣金"].sum() - order["服务商佣金"].sum()
            - order["其他收费"].sum()), 2)
        if net_sale_cf != order_net_cf:
            raise BillParseError(
                "快手资金账单货款净额与订单流水净现金流不一致",
                detail={"bill_net_yuan": net_sale_cf, "order_net_yuan": order_net_cf,
                        "diff_yuan": round(net_sale_cf - order_net_cf, 2),
                        "hint": "原脚本此处导出两 sheet 后 exit()。先核对两源是否同账期、"
                                "订单流水是否完整（多分段文件都要给全）"},
            )

        parts = [bill[bill["业务类别"] != "交易货款"]]
        for raw, comp, direction in ORDER_COMPONENTS:
            sub = order[order[comp] > 0]
            if sub.empty:
                continue
            g = sub.groupby(["线上订单号", "资金发生日期"], as_index=False, sort=True)[comp].sum()
            # 原脚本：货款收/退两侧 pivot 的 业务类别 都是「货款」
            g["业务类别"] = "货款" if comp == "货款退款" else comp
            g["收款"] = g[comp] if direction == "receipt" else 0.0
            g["退款"] = g[comp] if direction == "refund" else 0.0
            parts.append(g.loc[:, UNICOL])
        return pd.concat(parts, ignore_index=True)

    # ---------------- 出口

    def parse(self, ctx) -> list:
        detail = self.detail_frame(ctx)
        if detail.empty:
            return []
        std = pd.DataFrame()
        std[STD_ORDER_NO] = detail["线上订单号"]
        std[STD_FUND_DATE] = pd.to_datetime(detail["资金发生日期"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
        # 账单层「货款」类即原「交易货款」展开后的组件「货款」
        std[STD_BIZ_CATEGORY] = detail["业务类别"]
        std[STD_RECEIPT] = detail["收款"]
        std[STD_REFUND] = detail["退款"]
        return self.bill_rows(ctx, std)

    def export_books(self, ctx) -> list[dict]:
        detail = self.detail_frame(ctx)
        pivot = self.pivot_from_detail(detail, ctx, "快手账单",
                                       category_col="业务类别", receipt_col="收款", refund_col="退款")
        return [{
            "infix": "", "tail": "_账单整理",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "快手账单透视", "df": pivot, "kind": "yuan"},
                {"name": "快手账单", "df": detail, "kind": "yuan"},
            ],
        }]
