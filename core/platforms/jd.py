"""京东适配器（承接 bill_arr.jd_arr + jd_Ecard + jd_wallet_arr + jd_wallet_ecard
+ jd_wallet_arr_final + look_sku.jdwallet_time / bill_arrange_jd）。

京东是**三源**平台，且账单层与匹配层的行集**不同**（原脚本就是两套）：

账单层（对应基准「京东钱包透视」19 类）
  · 钱包流水是**主源**：按「资金动账备注」多级分类
  · 账期不取用户填的 s_date/e_date，而是用 look_sku.jdwallet_time 的口径：
    取钱包「备注资金日期」（从备注里正则提取 YYYY年M月D日）的 min~max，
    再用它过滤结算单 —— 2026-08 实测该账期为 2026-07-31~2026-08-30
  · E卡 CSV **不进账单**：jd_wallet_ecard 只用它与钱包「E卡货款」现金流做交叉校验，
    相等则原样返回，不等则原脚本 exit()
  · 「商家结算款」替换：仅当 钱包商家结算款现金流 == 结算单现金流合计 时，
    把钱包的商家结算款整类换成结算单透视；不等则只导出钱包

匹配层（goods_rows，对应 look_sku.bill_arrange_jd）
  · 结算单「货款」 + 钱包「货款退款」（改判货款、订单号从备注提取） + E卡「货款」
  · 注意：钱包自身的「货款」类**不进**匹配层

2026-11-14 全量回归实证（scripts/probe_jd_period.py）：
  备注资金日期 min~max 账期 → 商家结算款替换 → 19 类逐分对平（差异 0 类）；
  固定 8/1~8/31 账期 → nc_wallet 11986216 ≠ nc_js 12053300（差 67084 分）→ 不替换
  → 只有 10 类、11 类不一致。账期口径是能否对平的决定性因素。

目录结构（原始账单根目录下自动识别子目录，无需用户逐个选文件）：
  <root>/京东钱包/*.csv          钱包流水（skiprows=6, gbk）
  <root>/*结算单*/**/*.csv       结算单（gb18030，可含年份子目录）
  <root>/E卡/*.csv               E卡（gb18030）
也可用 extra_files['wallet' | 'settle' | 'ecard'] 显式指定。
"""
from __future__ import annotations

import glob
import os
import re
from typing import Iterable

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
    BillRow,
    ParseContext,
)

# 结算单必需列
SETTLE_COLUMNS = ("账单日期", "订单编号", "费用项", "金额")
# E卡必需列
ECARD_COLUMNS = ("订单编号", "金额", "完成时间")
# 钱包流水必需列
WALLET_COLUMNS = ("日期", "商户订单号", "收入金额(元)", "支出金额(元)")

UNMATCHED = "未匹配"
MERCHANT_SETTLE = "商家结算款"
ECARD_GOODS = "E卡货款"
GOODS_REFUND = "货款退款"
FEE_BUCKET = "账扣费用"

_DATE_IN_NOTE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_ORDER_IN_NOTE = re.compile(r"(对应)?订单号(\d+)")
_MONTH_IN_NAME = re.compile(r"_(\d{6})_")


def _strip_excel_guard(series: pd.Series) -> pd.Series:
    """剥离京东 csv 里的 ="..." 包裹（防科学计数法写法）。"""
    return (
        series.astype("string").fillna("")
        .str.replace('="', "", regex=False)
        .str.replace('"', "", regex=False)
        .str.strip()
    )


def _money(series: pd.Series) -> pd.Series:
    """原脚本：'--' → '0.00' 再转 float。"""
    return pd.to_numeric(
        series.astype("string").fillna("0.00").str.replace("--", "0.00", regex=False),
        errors="coerce",
    ).fillna(0.0)


def bustype_split(act_bus_type: str, bus_note: str) -> str:
    """账扣费用按备注关键字再细分（原 bill_arr.bustype_split，**顺序敏感**）。

    2026-11-14 踩坑：曾把「处理消费者售后退款问题」两条挪到循环末尾，
    导致「商家结算款」现金流算成 11663279 而非 11986216 分，
    替换判定从「相等」翻成「不等」，19 类基准只对平 10 类。
    """
    if str(act_bus_type) != FEE_BUCKET:
        return act_bus_type
    n = str(bus_note or "")
    if "京东开放物流费用" in n:
        return "京东开放物流费用"
    if "京东联盟扣款" in n:
        return "京东联盟扣款"
    if "直赔退款代扣" in n:
        return "小额打款"
    if "处理消费者售后退款问题" in n and "价保" not in n:
        return GOODS_REFUND
    if "处理消费者售后退款问题" in n and "价保" in n:
        return "价保退款"
    if "理赔单号" in n:
        return "理赔"
    if "京东联盟" in n:
        return "京东联盟"
    if "先行赔付" in n:
        return "订单赔付"
    if "结算单号" in n:
        return ECARD_GOODS
    if "FCS-备款" in n:
        return ECARD_GOODS
    if "日商家结算款" in n:
        return MERCHANT_SETTLE
    return act_bus_type


class JdAdapter(BillAdapter):
    platform = "jd"
    display_name = "京东"
    file_patterns = (".csv",)
    default_read_options = {"encoding": "gb18030"}
    needs_extra_files = ("wallet", "settle", "ecard")
    # 账单层（钱包+结算单 24 类）≠ 匹配层输入（结算单货款+货款退款改判+E卡）
    goods_input_matches_bill = False

    # ---------------- 文件发现

    def _roots(self, ctx: ParseContext) -> list[str]:
        # 主目录支持原始下载 zip（2026-11-16 用户需求）：解压后按子目录名识别三源
        return [p for p in self.expand_source_paths(ctx.file_paths or []) if p]

    def _discover(self, ctx: ParseContext, key: str) -> list[str]:
        """优先用 extra_files 显式指定，否则按子目录名自动识别。"""
        explicit = ctx.extra_files.get(key) if ctx.extra_files else None
        if explicit:
            files: list[str] = []
            # 附加源支持原始下载 zip（2026-11-16 用户需求）
            for p in self.expand_source_paths(explicit):
                p = str(p).strip().strip('"')
                if os.path.isdir(p):
                    files.extend(glob.glob(os.path.join(p, "**", "*.csv"), recursive=True))
                elif os.path.isfile(p):
                    files.append(p)
            return self._tidy(files)

        markers = {
            "wallet": ("钱包",),
            "ecard": ("e卡", "E卡"),
            "settle": ("结算单",),
        }[key]

        found: list[str] = []
        for root in self._roots(ctx):
            if os.path.isfile(root):
                if key == "settle":
                    found.append(root)
                continue
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                base = os.path.basename(dirpath)
                if any(m.lower() in base.lower() for m in markers):
                    found.extend(os.path.join(dirpath, f) for f in filenames
                                 if f.lower().endswith(".csv"))
            if key == "settle" and not found:
                # 兜底：没有「结算单」子目录时，取根目录下的 csv
                found.extend(glob.glob(os.path.join(root, "*.csv")))
        return self._tidy(found)

    @staticmethod
    def _tidy(files: Iterable[str]) -> list[str]:
        return [f for f in sorted(set(str(x) for x in files))
                if not os.path.basename(f).startswith("~$")]

    # ---------------- 钱包流水（主源）

    def _withdraw_keywords(self, ctx: ParseContext) -> list[str]:
        """提现判定关键字。原脚本硬编码 '京东_秋水伊人旗舰店' / '京东秋水伊人旗舰店'，
        这里按店铺名派生（规则表 biz_code['jd_withdraw_keyword'] 可追加）。"""
        kws = ["提现"]
        name = str(ctx.shop_name or "").strip()
        if name:
            # 店铺映射给的是氚云名称（已含「京东_」前缀，如 京东_秋水伊人旗舰店），
            # 原脚本硬编码关键字正是 京东_秋水伊人旗舰店 / 京东秋水伊人旗舰店。
            # 不能再盲目加前缀，否则变成 京东_京东_… 永不命中 → 提现整类丢失
            # （2026-11-16 JD001 回归实证：透视 18 类 vs 基准 19 类）。
            kws.append(name)
            kws.append(name.replace("_", ""))
            if not name.startswith("京东"):
                kws.append(f"京东_{name}")
                kws.append(f"京东{name}")
        extra = self.rule("biz_code", "jd_withdraw_keyword")
        if extra:
            kws.append(extra)
        return kws

    def _read_wallet(self, ctx: ParseContext) -> pd.DataFrame:
        files = self._discover(ctx, "wallet")
        if not files:
            return pd.DataFrame()
        frames = []
        for f in files:
            df = pd.read_csv(f, index_col=None, skiprows=6, encoding="gbk", low_memory=False)
            missing = [c for c in WALLET_COLUMNS if c not in df.columns]
            if missing:
                raise BillParseError(
                    f"京东钱包流水缺少字段：{', '.join(missing)}",
                    detail={"file": os.path.basename(f),
                            "available_columns": [str(c) for c in df.columns][:40],
                            "hint": "钱包流水需 skiprows=6 表头；请确认文件未被改动"},
                )
            df = df.copy()
            df["__source_file__"] = os.path.basename(f)
            df["__source_row__"] = range(len(df))
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _clean_wallet(self, df: pd.DataFrame, ctx: ParseContext) -> pd.DataFrame:
        out = df.copy()
        out["日期"] = _strip_excel_guard(out["日期"])
        out["商户订单号"] = _strip_excel_guard(out["商户订单号"]).astype(str)
        if "账户名称" in out.columns:
            out["账户名称"] = _strip_excel_guard(out["账户名称"])
        # 原脚本 add_column_if_not_exists(df,'资金动账备注','交易备注')
        if "资金动账备注" not in out.columns:
            if "交易备注" not in out.columns:
                raise BillParseError(
                    "京东钱包流水缺少「资金动账备注」与「交易备注」",
                    detail={"available_columns": [str(c) for c in out.columns][:40]},
                )
            out["资金动账备注"] = out["交易备注"]
        out["资金动账备注"] = _strip_excel_guard(out["资金动账备注"]).astype(str)
        out["收款"] = _money(out["收入金额(元)"])
        out["退款"] = _money(out["支出金额(元)"])
        out["资金发生日期"] = pd.to_datetime(out["日期"], errors="coerce").dt.floor("D")

        note = out["资金动账备注"]
        # 款项性质四级整理（原 jd_wallet_arr）
        c1 = np.where(note.str.contains("货款", na=False) | note.str.contains("退货", na=False),
                      BIZ_GOODS, "其它")
        # 原脚本：(业务类别-1 == '其它') AND (备注含 提现/店铺名 之一)
        # 2026-11-14 踩坑：曾写成 is_withdraw = (c1=='其它')，再逐个 OR 关键字，
        # 最后 AND (c1=='其它') —— 关键字条件被完全抵消，1097 行钱包流水
        # 只剩 2 类、提现虚增 12706588 分。必须先 OR 关键字、再与 '其它' 相 AND。
        kw_hit = pd.Series(False, index=out.index)
        for kw in self._withdraw_keywords(ctx):
            kw_hit = kw_hit | note.str.contains(kw, na=False, regex=False)
        c2 = np.where((pd.Series(c1, index=out.index) == "其它") & kw_hit, "提现", c1)
        c3 = np.where(pd.Series(c2, index=out.index) == "其它", "交易费用", c2)
        # 商户订单号长度 == 32 才视为有订单号
        is32 = out["商户订单号"].apply(lambda x: 1 if len(str(x)) == 32 else 0)
        cat = np.where((pd.Series(c3, index=out.index) == "交易费用") & (is32 == 0),
                       FEE_BUCKET, c3).astype(str)
        out[STD_BIZ_CATEGORY] = [bustype_split(a, b) for a, b in zip(cat, note.tolist())]
        out[STD_BIZ_CATEGORY] = out[STD_BIZ_CATEGORY].fillna(UNMATCHED)

        out["现金流"] = out["收款"] - out["退款"]
        out["备注资金日期"] = pd.to_datetime(
            note.map(lambda s: (_DATE_IN_NOTE.search(str(s)).groups()
                                if _DATE_IN_NOTE.search(str(s)) else None))
            .map(lambda g: f"{g[0]}-{int(g[1]):02d}-{int(g[2]):02d}" if g else None),
            errors="coerce",
        )

        out[STD_ORDER_NO] = out["商户订单号"]
        out[STD_FUND_DATE] = out["资金发生日期"].dt.strftime("%Y-%m-%d").fillna("")
        out[STD_RECEIPT] = out["收款"]
        out[STD_REFUND] = out["退款"]
        return out

    def _wallet_in_period(self, ctx: ParseContext) -> pd.DataFrame:
        wal = self._read_wallet(ctx)
        if wal.empty:
            return pd.DataFrame()
        wal = self._clean_wallet(wal, ctx)
        if ctx.s_date and ctx.e_date:
            d = wal["资金发生日期"]
            wal = wal[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        return wal

    def _infer_period(self, wal: pd.DataFrame, ctx: ParseContext) -> tuple[str, str]:
        """look_sku.jdwallet_time 口径：钱包「备注资金日期」min~max 决定结算单账期。"""
        if wal.empty or "备注资金日期" not in wal.columns:
            return ctx.s_date, ctx.e_date
        s = wal["备注资金日期"].min()
        e = wal["备注资金日期"].max()
        if pd.isna(s) or pd.isna(e):
            return ctx.s_date, ctx.e_date
        return str(pd.Timestamp(s).date()), str(pd.Timestamp(e).date())

    # ---------------- 结算单

    def _settle_files(self, ctx: ParseContext, period: tuple[str, str]) -> list[str]:
        """按推断账期挑结算单文件（文件名含 _YYYYMM_），避免读全量数百 MB。"""
        files = self._discover(ctx, "settle")
        s, e = period
        if not s or not e:
            return files
        lo, hi = s[:7].replace("-", ""), e[:7].replace("-", "")
        picked = []
        unparsable = []
        for f in files:
            m = _MONTH_IN_NAME.search(os.path.basename(f))
            if not m:
                unparsable.append(f)
                continue
            if lo <= m.group(1) <= hi:
                picked.append(f)
        # 文件名不含月份的，保守纳入（数量通常很少）
        return picked + unparsable

    def _read_settle(self, ctx: ParseContext, period: tuple[str, str]) -> pd.DataFrame:
        files = self._settle_files(ctx, period)
        if not files:
            return pd.DataFrame()
        frames = []
        for f in files:
            df = pd.read_csv(f, index_col=None, encoding="gb18030", low_memory=False)
            missing = [c for c in SETTLE_COLUMNS if c not in df.columns]
            if missing:
                raise BillParseError(
                    f"京东结算单缺少字段：{', '.join(missing)}",
                    detail={"file": os.path.basename(f),
                            "available_columns": [str(c) for c in df.columns][:40],
                            "hint": "请确认选中的是「结算单」目录而非钱包/E卡"},
                )
            df = df.copy()
            df["__source_file__"] = os.path.basename(f)
            df["__source_row__"] = range(len(df))
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _clean_settle(self, df: pd.DataFrame, period: tuple[str, str]) -> pd.DataFrame:
        out = df.copy()
        out["订单编号"] = _strip_excel_guard(out["订单编号"])
        for col in ("单据编号", "商品编号", "商户订单号"):
            if col in out.columns:
                out[col] = _strip_excel_guard(out[col])

        fee = out["费用项"].astype("string").fillna("")
        amount = pd.to_numeric(out["金额"], errors="coerce").fillna(0.0)

        # 货款类费用项（原 jd_arr）
        is_goods = (fee.str.contains("货款", na=False)
                    | fee.str.contains("预售首付款", na=False)
                    | fee.str.contains("价保扣款", na=False))
        cat = fee.where(~is_goods, BIZ_GOODS)
        # 售后卖家赔付 / 返还运费 且金额为负 → 货款
        cat = cat.mask(fee.str.contains("售后卖家赔付", na=False) & (amount < 0), BIZ_GOODS)
        cat = cat.mask(fee.str.contains("返还运费", na=False) & (amount < 0), BIZ_GOODS)
        out[STD_BIZ_CATEGORY] = cat.fillna(UNMATCHED).replace("", UNMATCHED)

        out["收款"] = amount.where(amount > 0, 0.0)
        out["退款"] = (-amount).where(amount < 0, 0.0)
        out["现金流"] = out["收款"] - out["退款"]

        raw_date = _strip_excel_guard(out["账单日期"])
        parsed = pd.to_datetime(raw_date, format="%Y%m%d", errors="coerce")
        fallback = pd.to_datetime(raw_date, errors="coerce")
        d = parsed.fillna(fallback)

        s, e = period
        if s and e:
            out = out.assign(__d__=d)
            out = out[(out["__d__"] >= pd.Timestamp(s)) & (out["__d__"] <= pd.Timestamp(e))]
            d = out["__d__"]
            out = out.drop(columns=["__d__"])
        else:
            out = out.copy()

        out[STD_ORDER_NO] = out["订单编号"]
        out[STD_FUND_DATE] = d.dt.strftime("%Y-%m-%d").fillna("")
        out[STD_RECEIPT] = out["收款"]
        out[STD_REFUND] = out["退款"]
        return out

    # ---------------- E卡（只交叉校验，不进账单）

    def _read_ecard(self, ctx: ParseContext) -> pd.DataFrame:
        files = self._discover(ctx, "ecard")
        if not files:
            return pd.DataFrame()
        frames = []
        for f in files:
            df = pd.read_csv(f, index_col=None, encoding="gb18030", low_memory=False)
            if not all(c in df.columns for c in ECARD_COLUMNS):
                continue
            df = df.copy()
            df["订单编号"] = _strip_excel_guard(df["订单编号"]).astype(str)
            df["金额"] = pd.to_numeric(df["金额"], errors="coerce").fillna(0.0)
            df["资金发生日期"] = pd.to_datetime(
                _strip_excel_guard(df["完成时间"]), errors="coerce").dt.floor("D")
            df[STD_BIZ_CATEGORY] = BIZ_GOODS
            df["收款"] = np.where(df["金额"] > 0, df["金额"], 0.0)
            df["退款"] = np.where(df["金额"] > 0, 0.0, -df["金额"])
            df["现金流"] = df["收款"] - df["退款"]
            df[STD_ORDER_NO] = df["订单编号"]
            df[STD_FUND_DATE] = df["资金发生日期"].dt.strftime("%Y-%m-%d").fillna("")
            df[STD_RECEIPT] = df["收款"]
            df[STD_REFUND] = df["退款"]
            df["__source_file__"] = os.path.basename(f)
            df["__source_row__"] = range(len(df))
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _cross_check_ecard(self, wal: pd.DataFrame, ecard: pd.DataFrame) -> None:
        """原 jd_wallet_ecard：钱包 E卡货款 现金流必须等于 E卡整理现金流，否则终止。"""
        if wal.empty or STD_BIZ_CATEGORY not in wal.columns:
            return
        sub = wal[wal[STD_BIZ_CATEGORY] == ECARD_GOODS]
        if sub.empty:
            return
        wallet_fen = int(round(float(sub["现金流"].sum()) * 100))
        ecard_fen = int(round(float(ecard["现金流"].sum()) * 100)) if not ecard.empty else 0
        if wallet_fen != ecard_fen:
            raise BillParseError(
                "京东 E卡 金额校验不平",
                detail={
                    "钱包E卡货款_分": wallet_fen,
                    "E卡表合计_分": ecard_fen,
                    "差额_分": wallet_fen - ecard_fen,
                    "hint": "原脚本此处直接终止。请确认 E卡 目录下的 csv 与钱包流水属同一账期",
                },
            )

    # ---------------- 账单层出口

    def _std_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        cols = [STD_ORDER_NO, STD_FUND_DATE, STD_BIZ_CATEGORY, STD_RECEIPT, STD_REFUND,
                "__source_file__", "__source_row__"]
        out = df.copy()
        for c in ("__source_file__", "__source_row__"):
            if c not in out.columns:
                out[c] = "" if c == "__source_file__" else None
        return out[cols]

    #: _build 结果缓存：parse() 与 goods_rows() 会各调一次，
    #: 而京东结算单单月 csv 可达 5-12MB（202608 为 5.0MB、202607 为 7.2MB），
    #: 重复解析代价高。缓存键含全部影响取数的上下文字段。
    _build_cache: dict[tuple, tuple] | None = None

    def _build(self, ctx: ParseContext):
        key = (
            tuple(ctx.file_paths or ()),
            ctx.shop_code, ctx.period, ctx.s_date, ctx.e_date,
            tuple(sorted((k, tuple(v or ())) for k, v in (ctx.extra_files or {}).items())),
        )
        cache = self._build_cache
        if cache is None:
            cache = self._build_cache = {}
        hit = cache.get(key)
        if hit is not None:
            return hit
        out = self._build_uncached(ctx)
        cache[key] = out
        return out

    def _build_uncached(self, ctx: ParseContext):
        """返回 (账单层行集, 结算单清洗后表, 钱包清洗后表, E卡表, 诊断信息)。"""
        wal = self._wallet_in_period(ctx)
        ecard = self._read_ecard(ctx)
        self._cross_check_ecard(wal, ecard)

        period = self._infer_period(wal, ctx)
        settle = pd.DataFrame()
        raw_settle = self._read_settle(ctx, period)
        if not raw_settle.empty:
            settle = self._clean_settle(raw_settle, period)

        diag: dict[str, object] = {
            "wallet_rows": int(len(wal)),
            "settle_rows": int(len(settle)),
            "ecard_rows": int(int(len(ecard)) if not ecard.empty else 0),
            "inferred_period": list(period),
            "replaced_merchant_settle": False,
        }

        if wal.empty and settle.empty:
            raise BillParseError(
                "未找到京东账单文件",
                detail={
                    "platform": self.platform,
                    "searched": list(ctx.file_paths or []),
                    "hint": ("京东需在账单根目录下放「京东钱包」「*结算单*」「E卡」三个子目录，"
                             "或在附加源里显式指定 wallet / settle / ecard"),
                },
            )

        # 商家结算款替换判定（原 jd_wallet_arr_final）
        parts = []
        if not wal.empty:
            merchant = wal[wal[STD_BIZ_CATEGORY] == MERCHANT_SETTLE]
            nc_wallet = int(round(float(merchant["现金流"].sum()) * 100)) if not merchant.empty else 0
            nc_js = int(round(float(settle["现金流"].sum()) * 100)) if not settle.empty else 0
            diag["nc_wallet_fen"] = nc_wallet
            diag["nc_js_fen"] = nc_js

            if not merchant.empty and not settle.empty and nc_wallet == nc_js:
                diag["replaced_merchant_settle"] = True
                parts.append(self._std_frame(wal[wal[STD_BIZ_CATEGORY] != MERCHANT_SETTLE]))
                parts.append(self._std_frame(settle))
            else:
                parts.append(self._std_frame(wal))
        elif not settle.empty:
            parts.append(self._std_frame(settle))

        bill = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return bill, settle, wal, ecard, diag

    def parse(self, ctx: ParseContext) -> list[BillRow]:
        bill, _settle, _wal, _ecard, diag = self._build(ctx)
        self.last_diag = diag
        if bill.empty:
            return []
        return self.bill_rows(ctx, bill)

    #: 最近一次 parse 的诊断（账期推断 / 是否替换 / 行数），供管理页与回归脚本核对
    last_diag: dict[str, object] = {}

    def match_window(self, ctx: ParseContext) -> tuple[str, str]:
        """原 main.py 京东分支：result() 收的是 jd_wallet_dict 的开始/结束日期
        （= 钱包「备注资金日期」min~max），不是用户填的账期。

        钱包缺失或备注里没有日期时回落到用户账期。
        """
        inferred = (self.last_diag or {}).get("inferred_period") or []
        if len(inferred) == 2 and inferred[0] and inferred[1]:
            return str(inferred[0]), str(inferred[1])
        return ctx.s_date, ctx.e_date

    # ---------------- 基准格式出口（原 jd_wallet_arr_final / jd_Ecard）

    #: 基准「京东钱包」sheet 的 9 列
    WALLET_DETAIL_COLUMNS = ("账户名称", "资金发生日期", "商户订单号", "资金动账备注",
                             "备注资金日期", "业务类别", "收款", "退款", "现金流")

    def _wallet_detail_frame(self, wal: pd.DataFrame) -> pd.DataFrame:
        if wal.empty:
            return pd.DataFrame(columns=list(self.WALLET_DETAIL_COLUMNS))
        out = wal.copy()
        if "账户名称" not in out.columns:
            out["账户名称"] = ""
        out["资金发生日期"] = pd.to_datetime(out["资金发生日期"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
        out["备注资金日期"] = pd.to_datetime(out["备注资金日期"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
        out = out.rename(columns={STD_BIZ_CATEGORY: "业务类别"})
        for c in self.WALLET_DETAIL_COLUMNS:
            if c not in out.columns:
                out[c] = ""
        return out.loc[:, list(self.WALLET_DETAIL_COLUMNS)].reset_index(drop=True)

    def _settle_detail_frame(self, settle: pd.DataFrame) -> pd.DataFrame:
        """基准「京东结算单明细」8 列（原 jd_arr res_df）。"""
        cols = ["业务基础订单号", "商户订单号", "资金发生日期", "费用项", "业务类别", "收款", "退款", "现金流"]
        if settle.empty:
            return pd.DataFrame(columns=cols)
        out = settle.copy()
        out["业务基础订单号"] = out["订单编号"]
        out["资金发生日期"] = out[STD_FUND_DATE]
        out = out.rename(columns={STD_BIZ_CATEGORY: "业务类别"})
        for c in cols:
            if c not in out.columns:
                out[c] = ""
        return out.loc[:, cols].reset_index(drop=True)

    def export_books(self, ctx) -> list[dict]:
        """京东基准簿：
        · _京东钱包整理.xlsx：氚云导入模板 + 京东钱包透视（替换后）+ 京东钱包（9 列）
          透视 = 钱包透视（剔除商家结算款）+ 结算单透视（账单类型=京东结算单），
          仅当 nc_wallet == nc_js 时替换；不替换则纯钱包透视（原脚本同口径）。
        · _京东账单整理.xlsx：京东结算单透视 + 京东结算单明细（原 jd_arr 产物）
        · 京东E卡_….xlsx：京东E卡 sheet（有 E卡源时，原 jd_Ecard 产物）
        """
        _bill, settle, wal, ecard, diag = self._build(ctx)
        books: list[dict] = []

        wallet_detail = self._wallet_detail_frame(wal)
        pivot_wallet = self.pivot_from_detail(
            wallet_detail, ctx, "京东钱包", with_cashflow=True)

        settle_detail = self._settle_detail_frame(settle)
        pivot_settle = self.pivot_from_detail(
            settle_detail, ctx, "京东结算单", with_cashflow=True)

        if bool(diag.get("replaced_merchant_settle")):
            pivot_sub = pd.concat(
                [pivot_wallet[pivot_wallet["业务类别"] != MERCHANT_SETTLE], pivot_settle],
                ignore_index=True)
        else:
            pivot_sub = pivot_wallet

        if not wallet_detail.empty or not pivot_sub.empty:
            books.append({
                "infix": "", "tail": "_京东钱包整理",
                "sheets": [
                    {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot_sub), "kind": "yuan"},
                    {"name": "京东钱包透视", "df": pivot_sub, "kind": "yuan"},
                    {"name": "京东钱包", "df": wallet_detail, "kind": "yuan"},
                ],
            })

        if not settle_detail.empty:
            books.append({
                "infix": "", "tail": "_京东账单整理",
                "sheets": [
                    {"name": "京东结算单透视", "df": pivot_settle, "kind": "yuan"},
                    {"name": "京东结算单明细", "df": settle_detail, "kind": "yuan"},
                ],
            })

        if not ecard.empty:
            cols = ["业务基础订单号", "商户订单号", "资金发生日期", "费用项", "业务类别", "收款", "退款", "现金流"]
            ec = ecard.copy()
            ec["业务基础订单号"] = ec["订单编号"]
            ec["商户订单号"] = ""
            ec["资金发生日期"] = ec[STD_FUND_DATE]
            ec["费用项"] = ec.get("费用项", "")
            ec = ec.rename(columns={STD_BIZ_CATEGORY: "业务类别"})
            for c in cols:
                if c not in ec.columns:
                    ec[c] = ""
            books.append({
                "prefix": "京东E卡",
                "infix": "", "tail": "",
                # E卡是交叉校验底稿，不是财务交付件 → 落 底稿/（2026-11-16 用户需求）
                "draft": True,
                "sheets": [{"name": "京东E卡", "df": ec.loc[:, cols], "kind": "yuan"}],
            })
        return books

    # ---------------- 匹配层出口（口径与账单层不同）

    def goods_rows(self, ctx: ParseContext) -> list[BillRow]:
        """look_sku.bill_arrange_jd 口径：结算单货款 + 钱包货款退款(改判货款) + E卡货款。

        ⚠️ 钱包自身的「货款」类**不进**匹配层 —— 这是原脚本的既有口径，
        与账单层透视（含钱包货款）刻意不同，不要「统一」掉。
        """
        _bill, settle, wal, ecard, _diag = self._build(ctx)
        parts = []

        if not settle.empty:
            parts.append(self._std_frame(settle[settle[STD_BIZ_CATEGORY] == BIZ_GOODS]))

        if not wal.empty:
            refund = wal[wal[STD_BIZ_CATEGORY] == GOODS_REFUND].copy()
            if not refund.empty:
                # 原 jdwallet_time：订单号从「资金动账备注」里正则提取
                refund[STD_ORDER_NO] = refund["资金动账备注"].map(
                    lambda t: (_ORDER_IN_NOTE.search(str(t)).group(2)
                               if _ORDER_IN_NOTE.search(str(t)) else "")
                )
                refund[STD_BIZ_CATEGORY] = BIZ_GOODS
                parts.append(self._std_frame(refund))

        if not ecard.empty:
            parts.append(self._std_frame(ecard))

        if not parts:
            return []
        merged = pd.concat(parts, ignore_index=True)
        merged = merged[merged[STD_BIZ_CATEGORY] == BIZ_GOODS]
        return self.bill_rows(ctx, merged)
