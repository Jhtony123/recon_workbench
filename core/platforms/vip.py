"""唯品会适配器（承接 bill_arr.wph_arr，约 987 行）。

关键口径（与其余平台差异最大）：
  · **按旬出账**：一个账期 = 一旬（如 08.01-08.10），目录里 VIP-/TGCY- 等多族文件；
    原脚本**不做日期过滤**（文件本身已按旬切好），插件保持同口径
  · 文件族按文件名前缀识别：VIP- / TGCY- / ZS- / MD-（排除 PRSHOP）/ PRSHOP-MD /
    -H- / VIPSHOP-，各族读各自 sheet 集合，缺 sheet 跳过
  · 全部行 业务类别=货款；品牌先按 品牌名称 brand_dict 映射，再按**款号前缀规则**
    覆盖（8+字母→FR / 42+数字→MyALL / 7→MyAll(菁禾) / 3→YUUSAAY / 5+数字→C3）
  · 店铺由品牌反推（shop_info）：QS→VIP00101 / CO→VIP00201 / FR / MyALL /
    MyAll(菁禾) / YUUSAAY / C3，未知→唯品会_未知店铺；透视按店铺分组（多店一簿）
  · 文件名特例：`唯品会_{账期标签}{cus_select}_账单整理.xlsx`，
    账期标签带日（2026年0801-2026年0810），cus_select 默认「唯品中国」
  · 数量先 abs，退款>0 且数量>0 时取负
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

RES_COL = ["店铺名称", "店铺编码", "数据来源", "业务发生时间", "销售订单号", "原采购单号/PO",
           "业务类别", "品牌", "款号", "sku", "数量", "含税结算金额", "不含税结算金额",
           "税率", "收款", "退款"]

BRAND_DICT = {
    "秋水伊人": "QS", "COCOON": "CO", "C3": "C3", "MyAll": "MyAll",
    "MyAll(菁禾)": "MyAll(菁禾)", "Myall-唯品-唯品会（菁禾）": "MyAll(菁禾)",
    "唯品会_MyAll（菁禾）": "MyAll(菁禾)", "YUUSAAY": "YUUSAAY",
}

SHOP_BY_BRAND = {
    "QS": ("唯品会_秋水伊人官方旗舰店", "VIP00101"),
    "CO": ("唯品会_COCOON官方旗舰店", "VIP00201"),
    "FR": ("唯品会_FR", "FR"),
    "MyALL": ("唯品会_MyAll", "MyALL"),
    "MyAll(菁禾)": ("唯品会_MyAll(菁禾)", "MyAll(菁禾)"),
    "YUUSAAY": ("唯品会_YUUSAAY", "YUUSAAY"),
    "C3": ("唯品会_C3", "C3"),
}
SHOP_UNKNOWN = ("唯品会_未知店铺", "未知编码")

# 标准结算列（销售/客退/其他明细共用）
_SETTLE_TAX = "结算含税金额:销售数量*结算含税单价"
_SETTLE_NOTAX = "结算不含税金额:销售数量*结算不含税单价"


def _qty_str(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype("str").str.replace(".0", "", regex=False),
                         errors="coerce").fillna(0).astype("int64")


class VipAdapter(BillAdapter):
    platform = "vip"
    display_name = "唯品会"
    bill_type = "唯品会"
    file_patterns = (".xlsx",)
    default_read_options = {}
    is_b2b = True

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        # 品牌名称→品牌简称走 biz_code 命名空间 brand:（款号前缀覆盖规则顺序敏感，留代码）
        out["biz_code"] = {f"brand:{k}": v for k, v in BRAND_DICT.items()}
        return out

    def _brand_dict(self) -> dict[str, str]:
        return self.prefixed_map("brand:") or BRAND_DICT

    # ---------------- 读取工具

    def _files(self, ctx) -> list[str]:
        files = self.collect_files(ctx)
        if not files:
            raise BillParseError("未找到唯品会账单文件",
                                 detail={"searched": list(ctx.file_paths or []),
                                         "hint": "唯品会按旬出账：给该旬目录（含 VIP-*.xlsx 与 TGCY-*.xlsx 等）"})
        return files

    @staticmethod
    def _load(files: list[str], sheet: str, dtype: dict) -> pd.DataFrame:
        frames = []
        for f in files:
            try:
                frames.append(pd.read_excel(f, index_col=None, sheet_name=sheet, dtype=dtype))
            except ValueError:
                continue
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _finish(self, d: pd.DataFrame, source: str) -> pd.DataFrame:
        """统一金额方向/品牌/店铺占位（原各 sheet 段的公共尾部）。"""
        d["数据来源"] = source
        d["业务类别"] = BIZ_GOODS
        amt = pd.to_numeric(d["含税结算金额"], errors="coerce").fillna(0.0).round(2)
        d["含税结算金额"] = amt
        d["不含税结算金额"] = pd.to_numeric(d["不含税结算金额"], errors="coerce").fillna(0.0).round(2)
        d["收款"] = np.where(amt > 0, amt, 0.0)
        d["退款"] = np.where(amt < 0, -amt, 0.0)
        if "税率" not in d.columns:
            d["税率"] = ""
        d["店铺名称"] = ""
        d["店铺编码"] = ""
        for c in RES_COL:
            if c not in d.columns:
                d[c] = ""
        return d.loc[:, RES_COL]

    def _settle_sheet(self, files, sheet, source, *, qty_col="结算数量", qty_str_mode=False):
        """销售明细/客退明细/其他明细 同构段。"""
        dtype = {"销售订单号": "str", "货号": "str", "条码": "str", "业务发生日期": "str",
                 "原采购单号": "str", qty_col: "str" if qty_str_mode else "int"}
        d = self._load(files, sheet, dtype)
        if d.empty:
            return d
        d["业务发生时间"] = pd.to_datetime(d["业务发生日期"].astype("str"), errors="coerce").dt.floor("d")
        d["数量"] = _qty_str(d[qty_col]) if qty_str_mode else pd.to_numeric(d[qty_col], errors="coerce").fillna(0).astype("int64")
        d["含税结算金额"] = d[_SETTLE_TAX]
        d["不含税结算金额"] = d[_SETTLE_NOTAX]
        d["款号"] = d["货号"].astype("str")
        d["sku"] = d["条码"].astype("str")
        d["原采购单号/PO"] = d["原采购单号"].astype("str")
        d["品牌"] = d["品牌名称"].map(lambda x: self._bd.get(x))
        return self._finish(d, source)

    def _discount_sheet(self, files, sheet, source):
        d = self._load(files, sheet, {"销售订单号": "str", "条码": "str", "业务发生日期": "str",
                                      "原采购单号": "str"})
        if d.empty:
            return d
        d["业务发生时间"] = pd.to_datetime(d["业务发生日期"].astype("str"), errors="coerce").dt.floor("d")
        d["数量"] = 0
        # 原脚本先把含税金额 round(2) 再除税（4101-4102 行）；用未舍入值除税
        # 会产生分位漂移（vip2 实证差 0.14 元）
        amt = pd.to_numeric(d["应扣供应商活动费用金额"], errors="coerce").fillna(0.0).round(2)
        d["含税结算金额"] = amt
        tax = pd.to_numeric(d["税率"], errors="coerce").fillna(0.0).round(2)
        d["不含税结算金额"] = (amt / (1 + tax)).round(2)
        d["sku"] = d["条码"].astype("str")
        d["款号"] = d["sku"].str[:-5]
        d["原采购单号/PO"] = d["原采购单号"].astype("str")
        d["品牌"] = d["品牌名称"].map(lambda x: self._bd.get(x))
        return self._finish(d, source)

    def _member_sheet(self, files):
        d = self._load(files, "会员价保", {"SO": "str", "SKU": "str", "价保申请时间": "str", "PO": "str"})
        if d.empty:
            return d
        d["销售订单号"] = d["SO"].astype("str")
        d["业务发生时间"] = pd.to_datetime(d["价保申请时间"].astype("str"), errors="coerce").dt.floor("d")
        d["数量"] = 0
        amt = pd.to_numeric(d["供应商实际承担金额"], errors="coerce").fillna(0.0).round(2)
        d["含税结算金额"] = amt
        tax = pd.to_numeric(d["税率"], errors="coerce").fillna(0.0).round(2)
        d["不含税结算金额"] = (amt / (1 + tax)).round(2)
        d["sku"] = d["SKU"].astype("str")
        d["款号"] = d["sku"].str[:-5]
        d["原采购单号/PO"] = d["PO"].astype("str")
        d["品牌"] = d["品牌"].map(lambda x: self._bd.get(x))
        return self._finish(d, "VIP表-会员价保")

    def _diff_sheet(self, files, source, *, with_order=True):
        d = self._load(files, "差异详情", {"SO": "str", "SKU": "str", "新建时间": "str", "PO": "str"})
        if d.empty:
            return d
        if with_order and "SO" in d.columns:
            d["销售订单号"] = d["SO"].astype("str")
        d["业务发生时间"] = pd.to_datetime(d["新建时间"].astype("str"), errors="coerce").dt.floor("d")
        d["数量"] = 0
        amt = pd.to_numeric(d["确认金额"], errors="coerce").fillna(0.0).round(2)
        d["含税结算金额"] = amt
        tax = pd.to_numeric(d["税率"], errors="coerce").fillna(0.0).round(2)
        d["不含税结算金额"] = (amt / (1 + tax)).round(2)
        d["sku"] = d["SKU"].astype("str")
        d["款号"] = d["sku"].str[:-5]
        d["原采购单号/PO"] = d["PO"].astype("str")
        d["品牌"] = d["品牌"].map(lambda x: self._bd.get(x))
        return self._finish(d, source)

    def _md_sheets(self, files, prefix):
        """MD / PRSHOP-MD 采购入库+采购退供（退供列名带前导空格，原脚本如此）。"""
        parts = []
        d = self._load(files, "采购入库明细",
                       {"货号": "str", "条码": "str", "采购订单创建日期": "str", "采购单号": "str"})
        if not d.empty:
            d["业务发生时间"] = pd.to_datetime(d["采购订单创建日期"].astype("str"), errors="coerce").dt.floor("d")
            d["数量"] = _qty_str(d["收货数量"])
            d["含税结算金额"] = d["采购含税金额"]
            d["不含税结算金额"] = d["采购不含税金额"]
            d["sku"] = d["条码"].astype("str")
            d["款号"] = d["货号"].astype("str")
            d["原采购单号/PO"] = d["采购单号"].astype("str")
            d["品牌"] = d["品牌名称"].map(lambda x: self._bd.get(x))
            parts.append(self._finish(d, f"{prefix}表-采购入库明细"))
        d = self._load(files, "采购退供明细", {"采购单号": "str"})
        if not d.empty:
            d["业务发生时间"] = ""
            d["数量"] = -_qty_str(d[" 退货数量"])
            d["含税结算金额"] = -pd.to_numeric(d[" 退货含税金额"], errors="coerce").fillna(0.0).round(2)
            d["不含税结算金额"] = -pd.to_numeric(d[" 退货不含税金额"], errors="coerce").fillna(0.0).round(2)
            d["sku"] = d[" 条码"].astype("str")
            d["款号"] = d[" 货号"].astype("str")
            d["原采购单号/PO"] = d["采购单号"].astype("str")
            d["品牌"] = d[" 品牌名称"].map(lambda x: self._bd.get(x))
            parts.append(self._finish(d, f"{prefix}表-采购退供明细"))
        return parts

    # ---------------- 明细

    def detail_frame(self, ctx) -> pd.DataFrame:
        files = self._files(ctx)
        # 品牌字典每次解析取一次（规则表 brand: 可覆盖），供各 sheet 段复用
        self._bd = self._brand_dict()
        by = lambda pre: [f for f in files if pre in os.path.basename(f)]
        vip = by("VIP-")
        tgcy = by("TGCY-")
        zs = by("ZS-")
        md = [f for f in files if "MD-" in os.path.basename(f) and "PRSHOP" not in os.path.basename(f)]
        prshop = by("PRSHOP-MD")
        h = by("-H-")
        vipshop = by("VIPSHOP-")

        parts: list[pd.DataFrame] = []
        if vip:
            parts += [self._settle_sheet(vip, "销售明细", "VIP表-销售明细"),
                      self._settle_sheet(vip, "客退明细", "VIP表-客退明细"),
                      self._discount_sheet(vip, "活动折扣明细", "VIP表-活动折扣明细"),
                      self._discount_sheet(vip, "活动折扣明细_2", "VIP表-活动折扣明细_2"),
                      self._member_sheet(vip),
                      self._settle_sheet(vip, "其他明细", "VIP表-其他明细", qty_str_mode=True),
                      self._diff_sheet(vip, "VIP表-差异详情", with_order=False)]
        if tgcy:
            parts += [self._settle_sheet(tgcy, "其他明细", "TGCY表-其他明细", qty_str_mode=True),
                      self._diff_sheet(tgcy, "TGCY表-差异详情")]
        if zs:
            parts += [self._settle_sheet(zs, "销售明细", "ZS表-销售明细", qty_str_mode=True),
                      self._settle_sheet(zs, "客退明细", "ZS表-客退明细", qty_str_mode=True),
                      self._discount_sheet(zs, "活动折扣明细", "ZS表-活动折扣明细"),
                      self._diff_sheet(zs, "ZS表-差异详情")]
        if md:
            parts += self._md_sheets(md, "MD")
        if prshop:
            parts += self._md_sheets(prshop, "PRSHOP")
        if h:
            d = self._load(h, "差异详情", {"SKU": "str", "新建时间": "str", "PO": "str"})
            if not d.empty:
                d["业务发生时间"] = pd.to_datetime(d["新建时间"].astype("str"), errors="coerce").dt.floor("d")
                d["数量"] = pd.to_numeric(d["数量"], errors="coerce").fillna(0)
                amt = pd.to_numeric(d["明细金额"], errors="coerce").fillna(0.0).round(2)
                d["含税结算金额"] = amt
                # H 表原脚本税率不 round（4714 行），与其余 sheet 不同，保持
                tax = pd.to_numeric(d["税率"], errors="coerce").fillna(0.0)
                d["不含税结算金额"] = (amt / (1 + tax)).round(2)
                d["sku"] = d["SKU"].astype("str")
                d["款号"] = d["sku"].str[:-5]
                d["原采购单号/PO"] = d["PO"].astype("str")
                d["品牌"] = d["品牌"].map(lambda x: self._bd.get(x))
                parts.append(self._finish(d, "H-表-差异详情"))
        if vipshop:
            parts += [self._settle_sheet(vipshop, "销售明细", "VIPSHOP表-销售明细", qty_str_mode=True),
                      self._settle_sheet(vipshop, "客退明细", "VIPSHOP表-客退明细", qty_str_mode=True)]

        parts = [p for p in parts if p is not None and not p.empty]
        if not parts:
            return pd.DataFrame(columns=RES_COL)
        df = pd.concat(parts, ignore_index=True)

        # 款号前缀规则覆盖品牌（原 check_sku_rule，顺序敏感）
        brands = df["品牌"].tolist()
        for i, kh in enumerate(df["款号"].astype("str")):
            if len(kh) >= 2:
                if kh.startswith("8") and kh[1].isalpha():
                    brands[i] = "FR"
                elif kh.startswith("42") and kh[1].isdigit():
                    brands[i] = "MyALL"
                elif kh.startswith("7"):
                    brands[i] = "MyAll(菁禾)"
                elif kh.startswith("3"):
                    brands[i] = "YUUSAAY"
                elif kh.startswith("5") and kh[1].isdigit():
                    brands[i] = "C3"
        df["品牌"] = brands
        # 店铺反推（原 shop_info）
        shops = [SHOP_BY_BRAND.get(b, SHOP_UNKNOWN) for b in df["品牌"]]
        df["店铺名称"] = [s[0] for s in shops]
        df["店铺编码"] = [s[1] for s in shops]
        df["收款"] = pd.to_numeric(df["收款"], errors="coerce").fillna(0.0)
        df["退款"] = pd.to_numeric(df["退款"], errors="coerce").fillna(0.0)
        df["数量"] = pd.to_numeric(df["数量"], errors="coerce").fillna(0).abs()
        df["数量"] = np.where((df["退款"] > 0) & (df["数量"] > 0), -df["数量"], df["数量"])
        df["业务发生时间"] = df["业务发生时间"].map(
            lambda v: v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else "")
        for c in ("销售订单号", "原采购单号/PO", "款号", "sku", "品牌"):
            df[c] = df[c].astype("str").replace("nan", "").replace("None", "")
        # 原脚本不做日期过滤（文件已按旬切好）；保持同口径
        return df.loc[:, RES_COL].reset_index(drop=True)

    # ---------------- 账期标签与透视

    @staticmethod
    def period_label_vip(ctx) -> str:
        s, e = str(ctx.s_date or ""), str(ctx.e_date or "")
        if len(s) >= 10 and len(e) >= 10:
            return f"{s[:4]}年{s[5:7]}{s[8:10]}-{e[:4]}年{e[5:7]}{e[8:10]}"
        return ctx.period_cn

    def _pivot(self, ctx, detail: pd.DataFrame) -> pd.DataFrame:
        cols = ["店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流"]
        if detail.empty:
            return pd.DataFrame(columns=cols)
        g = detail.groupby(["店铺名称", "店铺编码", "业务类别"], as_index=False, sort=True)[["收款", "退款"]].sum()
        g["现金流"] = g["收款"] - g["退款"]
        g["账单周期"] = self.period_label_vip(ctx)
        g["账单类型"] = str(ctx.extra_files.get("_cus_select", ["唯品中国"])[0]
                          if isinstance(ctx.extra_files.get("_cus_select"), list)
                          else ctx.extra_files.get("_cus_select") or "唯品中国")
        return g.loc[:, cols]

    # ---------------- 出口

    def parse(self, ctx) -> list:
        detail = self.detail_frame(ctx)
        if detail.empty:
            return []
        std = pd.DataFrame()
        std[STD_ORDER_NO] = detail["销售订单号"]
        std[STD_FUND_DATE] = detail["业务发生时间"]
        std[STD_BIZ_CATEGORY] = detail["业务类别"]
        std[STD_RECEIPT] = detail["收款"]
        std[STD_REFUND] = detail["退款"]
        return self.bill_rows(ctx, std)

    def export_books(self, ctx) -> list[dict]:
        detail = self.detail_frame(ctx)
        pivot = self._pivot(ctx, detail)
        safe = lambda s: "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|')
        cus = "唯品中国"
        return [{
            # 文件名特例：唯品会_{带日账期}{cus_select}_账单整理.xlsx（不含店铺）
            "file_name": f"唯品会_{safe(self.period_label_vip(ctx))}{safe(cus)}_账单整理.xlsx",
            "sheets": [
                {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                {"name": "唯品会透视", "df": pivot, "kind": "yuan"},
                {"name": "唯品会账单整理", "df": detail, "kind": "yuan"},
            ],
        }]
