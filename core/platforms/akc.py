"""爱库存适配器（承接 bill_arr.akc_arr，约 748 行）。

关键口径：
  · 单源 xlsx，**13 个费用 sheet** 各自归一后合并（缺 sheet 跳过，出现未知 sheet 报错）
  · 一个文件含**多家店铺**：按 店铺ID 映射店铺名称/编码/品牌（shop_info），
    透视按 店铺名称+店铺编码+业务类别 分组 —— 不用 ctx 的单店信息
  · 文件名特例：`爱库存_{账期}_账单整理.xlsx`（**不含店铺名/编码**，原脚本如此）
  · 款号：原脚本查 ADB AI_product_info；8 月基准实证 款号 == sku[:-5]，
    插件按此推导（有意去数据库依赖；sku 为空则款号为空）
  · 账期过滤用「账单日」→资金日期
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

RES_COL = ["数据来源", "店铺名称", "店铺ID", "店铺编码", "销售订单号", "业务发生时间",
           "资金日期", "业务类别", "收款", "退款", "sku", "款号", "品牌", "数量"]

ALLOWED_SHEETS = (
    "订单货款", "售后扣款", "营销费用", "退换运费无忧", "订单赔付", "违规明细",
    "申诉补款", "商家直客", "基础技术年费", "品牌推广费", "交易手续费",
    "流量雷达服务费", "二程快递费",
)

# 店铺ID → (店铺名称, 店铺编码, 品牌)；未知 ID 回落 AKC003（原 shop_info）
SHOP_BY_ID = {
    "S18770776023311001": ("爱库存_秋水伊人CHIU·SHUI女装旗舰店", "AKC003", "QS"),
    "S187707760": ("爱库存_可可尼女装旗舰店", "AKC004", "CO"),
}
SHOP_FALLBACK = ("爱库存_秋水伊人CHIU·SHUI女装旗舰店", "AKC003", "QS")


class AkcAdapter(BillAdapter):
    platform = "akc"
    display_name = "爱库存"
    bill_type = "爱库存"
    file_patterns = (".xlsx",)
    default_read_options = {}

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        # 店铺ID→「店铺名称|店铺编码|品牌」走 biz_code 命名空间 shop:（值用 | 分隔）
        out["biz_code"] = {f"shop:{k}": "|".join(v) for k, v in SHOP_BY_ID.items()}
        return out

    def _shop_by_id(self) -> dict[str, tuple[str, str, str]]:
        raw = self.prefixed_map("shop:")
        if not raw:
            return dict(SHOP_BY_ID)
        out: dict[str, tuple[str, str, str]] = {}
        for sid, v in raw.items():
            parts = str(v).split("|")
            if len(parts) == 3:
                out[sid] = (parts[0], parts[1], parts[2])
        return out or dict(SHOP_BY_ID)

    # ---------------- 读取

    def _files(self, ctx) -> list[str]:
        files = self.collect_files(ctx)
        if not files:
            raise BillParseError("未找到爱库存账单文件",
                                 detail={"searched": list(ctx.file_paths or []),
                                         "hint": "爱库存是单个 xlsx（含 13 个费用 sheet）"})
        for f in files:
            names = pd.ExcelFile(f).sheet_names
            unknown = [s for s in names if s not in ALLOWED_SHEETS]
            if unknown:
                raise BillParseError(
                    "爱库存账单包含未知工作表",
                    detail={"file": os.path.basename(f), "unknown_sheets": unknown,
                            "allowed": list(ALLOWED_SHEETS),
                            "hint": "上游新增费用 sheet 时需扩展 ALLOWED_SHEETS 与归一逻辑（见 platform-akc.md）"},
                )
        return files

    @staticmethod
    def _base(df: pd.DataFrame, source: str, *, time_col: str = "业务发生时间") -> pd.DataFrame:
        df = df.copy()
        df["资金日期"] = pd.to_datetime(df["账单日"].astype("str"), errors="coerce").dt.floor("d")
        df["业务发生时间"] = pd.to_datetime(df[time_col].astype("str"), errors="coerce").dt.floor("d")
        df["数据来源"] = source
        df["店铺名称"] = ""
        df["店铺编码"] = ""
        df["店铺ID"] = df["店铺ID"].astype("str") if "店铺ID" in df.columns else ""
        df["品牌"] = ""
        return df

    @staticmethod
    def _by_direction(df: pd.DataFrame, amount_col: str) -> pd.DataFrame:
        amt = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0).round(2)
        df["收款"] = np.where(df["收付类型"] == "商家应收", amt, 0.0)
        df["退款"] = np.where(df["收付类型"] == "商家应付", amt, 0.0)
        return df

    def _sheet_frames(self, ctx) -> pd.DataFrame:
        files = self._files(ctx)
        parts: list[pd.DataFrame] = []

        def load(sheet: str, dtype: dict | None = None):
            frames = []
            for f in files:
                try:
                    frames.append(pd.read_excel(f, index_col=None, sheet_name=sheet, dtype=dtype or {}))
                except ValueError:
                    continue
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        str_dtype = {"账单日": "str", "业务发生时间": "str", "二级订单号": "str"}

        # 订单货款
        d = load("订单货款", str_dtype)
        if not d.empty:
            d = self._base(d, "订单货款")
            d["收款"] = pd.to_numeric(d["结算金额"], errors="coerce").fillna(0.0).round(2)
            d["退款"] = 0.0
            d["销售订单号"] = d["二级订单号"]
            d["业务类别"] = BIZ_GOODS
            d["sku"] = d["条码"].astype("str")
            d["款号"] = d["款号"].astype("str")
            d["数量"] = pd.to_numeric(d["商品件数"], errors="coerce").fillna(0).astype("int64")
            parts.append(d.loc[:, RES_COL])

        # 售后扣款
        d = load("售后扣款", str_dtype)
        if not d.empty:
            d = self._base(d, "售后扣款")
            d["收款"] = 0.0
            d["退款"] = pd.to_numeric(d["扣款金额"], errors="coerce").fillna(0.0).round(2)
            d["销售订单号"] = d["二级订单号"]
            is_return = d["结算科目"].eq("售后退货款")
            d["业务类别"] = np.where(is_return, BIZ_GOODS, "售后扣款")
            d["sku"] = d["条码"].astype("str")
            d["款号"] = d["款号"].astype("str")
            d["数量"] = np.where(is_return, 1, 0)
            parts.append(d.loc[:, RES_COL])

        # 营销费用
        d = load("营销费用", str_dtype)
        if not d.empty:
            d = self._base(d, "营销费用")
            d = self._by_direction(d, "商家承担总金额")
            d["销售订单号"] = d["二级订单号"]
            d["业务类别"] = "营销费用"
            d["sku"] = d["条码"].astype("str")
            d["款号"] = d["款号"].astype("str")
            d["数量"] = pd.to_numeric(d["商品件数"], errors="coerce").fillna(0).astype("int64")
            parts.append(d.loc[:, RES_COL])

        # 退换运费无忧 / 订单赔付 / 违规明细 / 申诉补款 / 商家直客 / 品牌推广费 / 交易手续费
        simple = (
            ("退换运费无忧", "扣款金额", "二级订单号", "退换运费无忧", "业务发生时间", False),
            ("订单赔付", "扣款金额", "二级订单号", "订单赔付", "业务发生时间", True),
            ("违规明细", "扣款金额", "二级单号", "违规明细", "业务发生时间", False),
            ("申诉补款", "补款金额", "二级单号", "申诉补款", "业务发生时间", False),
            ("商家直客", "结算金额", "订单号", "商家直客", "进线时间", False),
            ("品牌推广费", "结算金额", "二级单号", "品牌推广费", "业务发生时间", False),
            ("交易手续费", "扣款金额", "二级单号", "交易手续费", "业务发生时间", False),
        )
        for sheet, amt_col, order_col, cat, time_col, has_sku in simple:
            d = load(sheet, {"账单日": "str", time_col: "str", order_col: "str"})
            if d.empty:
                continue
            d = self._base(d, sheet, time_col=time_col)
            d = self._by_direction(d, amt_col)
            d["销售订单号"] = d[order_col].astype("str")
            d["业务类别"] = cat
            if has_sku:
                d["sku"] = d["条码"].astype("str")
                d["款号"] = d["款号"].astype("str")
                d["数量"] = pd.to_numeric(d["数量"], errors="coerce").fillna(0).astype("int64")
            else:
                d["sku"] = ""
                d["款号"] = ""
                d["数量"] = 0
            if sheet in ("品牌推广费", "交易手续费"):
                d["店铺ID"] = ""
            parts.append(d.loc[:, RES_COL])

        # 流量雷达服务费 / 二程快递费 / 基础技术年费（无订单号、无店铺ID）
        for sheet, amt_col, cat in (("流量雷达服务费", "扣款金额", "流量雷达服务费"),
                                    ("二程快递费", "结算金额", "快递费"),
                                    ("基础技术年费", "结算金额", "基础技术年费")):
            d = load(sheet, {"账单日": "str", "业务发生时间": "str"})
            if d.empty:
                continue
            d = self._base(d, sheet)
            d = self._by_direction(d, amt_col)
            d["销售订单号"] = ""
            d["业务类别"] = cat
            d["sku"] = ""
            d["款号"] = ""
            d["数量"] = 0
            d["店铺ID"] = ""
            parts.append(d.loc[:, RES_COL])

        if not parts:
            return pd.DataFrame(columns=RES_COL)
        return pd.concat(parts, ignore_index=True)

    # ---------------- 明细（含店铺映射与款号推导）

    def detail_frame(self, ctx) -> pd.DataFrame:
        df = self._sheet_frames(ctx)
        if df.empty:
            return df
        if ctx.s_date and ctx.e_date:
            d = df["资金日期"]
            df = df[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))].copy()
        # 款号：基准实证 == sku[:-5]（原脚本查 ADB，插件去数据库依赖）
        sku = df["sku"].astype("str").replace("nan", "")
        df["款号"] = sku.map(lambda s: s[:-5] if len(s) > 5 else "")
        df["sku"] = sku
        # 店铺映射（原 shop_info；规则表 shop: 可覆盖，值=名称|编码|品牌）
        shop_map = self._shop_by_id()
        names, codes, brands = [], [], []
        for sid in df["店铺ID"].astype("str"):
            n, c, b = shop_map.get(sid, SHOP_FALLBACK)
            names.append(n); codes.append(c); brands.append(b)
        df["店铺名称"], df["店铺编码"], df["品牌"] = names, codes, brands
        df["品牌"] = np.where(df["sku"].eq(""), "", df["品牌"])
        df["资金日期"] = df["资金日期"].dt.strftime("%Y-%m-%d").fillna("")
        df["业务发生时间"] = df["业务发生时间"].dt.strftime("%Y-%m-%d").fillna("")
        for c in ("销售订单号", "店铺ID"):
            df[c] = df[c].astype("str").replace("nan", "")
        return df.loc[:, RES_COL].reset_index(drop=True)

    def _pivot(self, ctx, detail: pd.DataFrame) -> pd.DataFrame:
        cols = ["店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流"]
        if detail.empty:
            return pd.DataFrame(columns=cols)
        g = detail.groupby(["店铺名称", "店铺编码", "业务类别"], as_index=False, sort=True)[["收款", "退款"]].sum()
        g["现金流"] = g["收款"] - g["退款"]
        g["账单周期"] = ctx.period_cn
        g["账单类型"] = "爱库存账单"
        return g.loc[:, cols]

    # ---------------- 出口

    def parse(self, ctx) -> list:
        detail = self.detail_frame(ctx)
        if detail.empty:
            return []
        std = pd.DataFrame()
        std[STD_ORDER_NO] = detail["销售订单号"]
        std[STD_FUND_DATE] = detail["资金日期"]
        std[STD_BIZ_CATEGORY] = detail["业务类别"]
        std[STD_RECEIPT] = detail["收款"]
        std[STD_REFUND] = detail["退款"]
        return self.bill_rows(ctx, std)

    def export_books(self, ctx) -> list[dict]:
        detail = self.detail_frame(ctx)
        pivot = self._pivot(ctx, detail)
        safe = lambda s: "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|')
        return [{
            # 文件名特例：不含店铺名/编码（原 akc_arr 输出 爱库存_{bill_time}_账单整理.xlsx）
            "file_name": f"爱库存_{safe(ctx.period_cn)}_账单整理.xlsx",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "爱库存透视", "df": pivot, "kind": "yuan"},
                {"name": "爱库存账单整理", "df": detail, "kind": "yuan"},
            ],
        }]
