"""平台适配器基类。

三步流水线（对应原脚本每个 *_arr 函数的共同骨架）：
  read()      读原始账单文件（csv/xlsx），返回原始 DataFrame 列表
  clean()     平台特有清洗（去表头尾行、去 ="..." 包裹、字段裁剪）
  normalize() 归一为标准五元结构：订单编号 / 资金日期 / 业务类别 / 收款 / 退款

归一后的结果统一由 bill_rows() 转成 BillRow 记录写入 ext_recon_workbench_bill_rows，
金额一律转「分」的 INTEGER（core.money.to_fen）。

原脚本的差异点（每个平台各自的列名、编码字典、正负号）全部外置为规则：
  rule_kind='column_map'  标准字段 → 平台原始列名
  rule_kind='biz_code'    业务编码 → 业务类别（如 0010001 → 货款）
  rule_kind='amount_sign' 金额方向约定（receipt_positive 等）
  rule_kind='read_option' 读取参数（encoding / skiprows / drop_tail）
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import pandas as pd

from ..config import BIZ_GOODS
from ..errors import BillParseError
from ..money import to_fen

# 标准字段名（归一后的列）
STD_ORDER_NO = "订单编号"
STD_FUND_DATE = "资金日期"
STD_BIZ_CATEGORY = "业务类别"
STD_RECEIPT = "收款"
STD_REFUND = "退款"

STANDARD_COLUMNS: tuple[str, ...] = (
    STD_ORDER_NO, STD_FUND_DATE, STD_BIZ_CATEGORY, STD_RECEIPT, STD_REFUND,
)

# 无订单号时的占位（原脚本 '无订单号'）
NO_ORDER = "无订单号"

#: read_option 里存但**不是 pandas 读取参数**的清洗键（2026-11-16 规则剥离后
#: 会进 read_options()，直接 **opts 传 read_csv 会 TypeError；taobao 实证）
_NON_PANDAS_READ_KEYS = ("drop_tail",)


@dataclass
class ParseContext:
    """一次账单整理的上下文（对应原脚本 bill_type/bill_code/bill_time/s_date/e_date/shop_name）。"""

    platform: str
    shop_code: str = ""
    shop_name: str = ""
    period: str = ""            # 'YYYY-MM'
    s_date: str = ""            # 'YYYY-MM-DD'
    e_date: str = ""
    batch_id: str = ""
    file_paths: list[str] = field(default_factory=list)   # 主账单目录/文件
    extra_files: dict[str, list[str]] = field(default_factory=dict)  # 京东钱包/E卡等附加源

    def label(self) -> str:
        return f"{self.platform}/{self.shop_code}/{self.period}"

    @property
    def period_cn(self) -> str:
        """业务账期中文标签（'2026年08月-2026年08月'）。

        period 字段保持 'YYYY-MM'，只作分区键 / 目录名 / 筛选键；**导出与写库的
        业务字段必须用本属性**，与生产库 mid_jedz_datas.bill_period 同格式。
        2026-11-13 回归实测缺陷：账单「汇总」sheet 与匹配明细的账单周期写成
        'YYYY-MM'，基准是中文标签，且 _compare_periods 解析不出 'YYYY-MM'
        会静默返回 0（跨期单据识别失效）。
        """
        from ..config import period_label

        # 京东特例（2026-11-16 十月实测）：s/e 是「上月末~月末」数据窗口（一天到账延迟），
        # 文件名与账单周期标签必须用**账期**（基准 2026年10月-2026年10月），
        # 否则跨月窗口把标签写成 09月-10月。数据窗口只用于过滤，不进业务标签。
        if self.platform == "jd":
            return period_label(self.period) or self.period
        return period_label(self.period, s_date=self.s_date, e_date=self.e_date) or self.period


@dataclass
class BillRow:
    """归一后的一行账单（金额已转为分）。"""

    order_no: str
    fund_date: str
    biz_category: str
    receipt_fen: int
    refund_fen: int
    source_file: str = ""
    source_row: int | None = None

    @property
    def net_fen(self) -> int:
        return int(self.receipt_fen) - int(self.refund_fen)

    def to_dict(self, ctx: ParseContext) -> dict[str, Any]:
        return {
            "platform": ctx.platform,
            "shop_code": ctx.shop_code,
            "shop_name": ctx.shop_name,
            "period": ctx.period,
            "s_date": ctx.s_date,
            "e_date": ctx.e_date,
            "order_no": self.order_no or NO_ORDER,
            "fund_date": self.fund_date,
            "biz_category": self.biz_category or "",
            "receipt_fen": int(self.receipt_fen or 0),
            "refund_fen": int(self.refund_fen or 0),
            "net_fen": self.net_fen,
            "source_file": self.source_file,
            "source_row": self.source_row,
            "batch_id": ctx.batch_id,
        }


class BillAdapter:
    """平台适配器基类。子类只需覆盖差异点。"""

    #: 平台标识（与规则表 platform 列一致）
    platform: str = ""
    #: 中文显示名
    display_name: str = ""
    #: 接受的原始文件扩展名
    file_patterns: tuple[str, ...] = (".csv",)
    #: 默认读取参数（可被规则表 read_option 覆盖）
    default_read_options: dict[str, Any] = {"encoding": "utf-8"}
    #: 是否需要附加数据源（京东钱包/E卡）
    needs_extra_files: tuple[str, ...] = ()
    #: 是否 B2B 通道（不走 OMS 匹配）
    is_b2b: bool = False

    def __init__(self, rules: dict[str, dict[str, str]] | None = None) -> None:
        self.rules: dict[str, dict[str, str]] = rules or {}

    # ---------------- 规则读取

    def rule(self, kind: str, key: str, default: str = "") -> str:
        return str(self.rules.get(kind, {}).get(key, default) or default)

    def column_map(self) -> dict[str, str]:
        """标准字段 → 平台原始列名。"""
        raw = self.rules.get("column_map", {}) or {}
        return {str(k): str(v) for k, v in raw.items() if v}

    def biz_code_map(self) -> dict[str, str]:
        """业务编码 → 业务类别（如 0010001 → 货款）。

        带命名空间前缀的键（wx_id:/wx_type:/brand:/shop:，供淘系微信/唯品会品牌/
        爱库存店铺等副字典共用 biz_code 通道）不进主映射，用 prefixed_map() 取。
        """
        raw = self.rules.get("biz_code", {}) or {}
        return {str(k): str(v) for k, v in raw.items() if v and ":" not in str(k)}

    def prefixed_map(self, prefix: str) -> dict[str, str]:
        """取 biz_code 里某命名空间前缀的副字典（键去前缀）。"""
        raw = self.rules.get("biz_code", {}) or {}
        p = str(prefix)
        return {str(k)[len(p):]: str(v) for k, v in raw.items()
                if str(k).startswith(p) and v}

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        """内置默认规则（2026-11-16 剥离到 rules/ 的种子源）。

        子类覆盖：返回 {rule_kind: {key: value}}。种子脚本据此生成
        rules/{platform}.json（note=builtin-default）；代码字典保留为兜底，
        规则文件被删/停用时回落，口径不变。
        """
        out: dict[str, dict[str, str]] = {}
        ro = getattr(cls, "default_read_options", None) or {}
        if ro:
            import json as _json

            ser: dict[str, str] = {}
            for k, v in ro.items():
                if isinstance(v, (dict, list)):
                    ser[k] = _json.dumps(v, ensure_ascii=False)
                elif isinstance(v, bool):
                    ser[k] = "true" if v else "false"
                else:
                    ser[k] = str(v)
            out["read_option"] = ser
        return out

    def read_options(self) -> dict[str, Any]:
        """读取参数：默认值 + 规则表覆盖。规则表里值一律是字符串，这里做类型还原。

        值以 { 或 [ 开头时按 JSON 还原（dtype 字典等复杂参数，2026-11-16
        内置规则剥离到 rules/ 后必须能原样表达）；其余 true/false→bool、整数→int。
        """
        import json as _json

        opts = dict(self.default_read_options)
        for k, v in (self.rules.get("read_option", {}) or {}).items():
            if k in _NON_PANDAS_READ_KEYS:
                continue  # 清洗参数（如 drop_tail）不是 pandas 读取参数，走 self.rule() 消费
            sv = str(v).strip()
            if sv[:1] in ("{", "["):
                try:
                    opts[k] = _json.loads(sv)
                    continue
                except ValueError:
                    pass
            if sv.lower() in ("true", "false"):
                opts[k] = sv.lower() == "true"
            else:
                try:
                    opts[k] = int(sv)
                except ValueError:
                    opts[k] = sv
        return opts

    # ---------------- 文件读取

    def collect_files(self, ctx: ParseContext, key: str = "") -> list[str]:
        """收集原始账单文件。ctx.file_paths 可以是目录、具体文件或**原始下载压缩包**。

        zip 支持（2026-11-16 用户需求）：用户可直接给平台原始下载的 zip；
        目录里只有 zip 时也会自动解压。解压产物落 runtime_dir()/extracted/
        （按 zip 路径+mtime 缓存，重复跑不重复解压），再递归收集。
        """
        sources: Iterable[str]
        if key:
            sources = ctx.extra_files.get(key, []) or []
        else:
            sources = ctx.file_paths or []

        out: list[str] = []
        for p in sources:
            p = str(p).strip().strip('"')
            if not p:
                continue
            if os.path.isdir(p):
                direct = self._collect_dir(p)
                out.extend(direct)
                # 目录内已有账单文件时不再解压 zip（防 zip+已解压副本双份重复计账）；
                # 只有 zip（用户未解压）才自动解压收集
                if not direct:
                    for z in sorted(glob.glob(os.path.join(p, "*.zip"))):
                        out.extend(self._collect_dir(self._extract_zip(z)))
            elif os.path.isfile(p):
                if p.lower().endswith(".zip"):
                    out.extend(self._collect_dir(self._extract_zip(p)))
                else:
                    out.append(p)
        # 排除 Excel 临时文件
        out = [f for f in sorted(set(out)) if not os.path.basename(f).startswith("~$")]
        return out

    @classmethod
    def expand_source_paths(cls, paths: Iterable[str]) -> list[str]:
        """附加源路径展开：zip 文件/含 zip 的目录 → 解压目录；其余原样保留。

        供快手订单、淘系微信、京东钱包/结算单/E卡等自管文件发现的适配器复用，
        保证「用户直接给原始下载压缩包」在所有数据源上一致可用。
        """
        out: list[str] = []
        for p in paths or []:
            p = str(p).strip().strip('"')
            if not p:
                continue
            if os.path.isfile(p) and p.lower().endswith(".zip"):
                out.append(cls._extract_zip(p))
            elif os.path.isdir(p):
                entries = os.listdir(p)
                non_zip = [e for e in entries if not e.lower().endswith(".zip")]
                zips = sorted(glob.glob(os.path.join(p, "*.zip")))
                # 只有「纯 zip 目录」（无任何已解压文件/子目录）才自动解压；
                # 已有子目录或数据文件时保持原目录，防 zip+解压副本双份重复计账
                if zips and not non_zip:
                    out.extend(cls._extract_zip(z) for z in zips)
                else:
                    out.append(p)
            else:
                out.append(p)
        return out

    def _collect_dir(self, p: str) -> list[str]:
        """递归收集目录下符合 file_patterns 的文件（解压后常带日期子目录）。"""
        out: list[str] = []
        for pat in self.file_patterns:
            out.extend(glob.glob(os.path.join(p, f"*{pat}")))
            out.extend(glob.glob(os.path.join(p, "**", f"*{pat}"), recursive=True))
        return out

    @staticmethod
    def _extract_zip(zpath: str) -> str:
        """解压原始下载 zip 到缓存目录，返回解压根目录。

        缓存键 = zip 绝对路径 + mtime + size：同一 zip 重复跑直接复用；
        用户重新下载（mtime 变）则新建目录，不会读到旧内容。
        zip 内文件名可能是 gb18030（Windows 压缩工具），做编码兜底。
        """
        import hashlib
        import zipfile

        from ..config import runtime_dir

        st = os.stat(zpath)
        key = hashlib.md5(f"{os.path.abspath(zpath)}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:12]
        stem = os.path.splitext(os.path.basename(zpath))[0]
        safe_stem = "".join(ch for ch in stem if ch not in '\\/:*?"<>|')[:60]
        dest = runtime_dir() / "extracted" / f"{safe_stem}_{key}"
        if dest.is_dir() and any(dest.iterdir()):
            return str(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                name = info.filename
                if not (info.flag_bits & 0x800):  # 非 UTF-8 标志 → 尝试 gb18030
                    try:
                        name = name.encode("cp437").decode("gb18030")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                # Windows 非法字符净化：有赞 zip 内层目录名带冒号（00:00:00），
                # 直接落盘 WinError 123（2026-11-16 实证）。逐段替换，保留目录结构。
                parts = [re.sub(r'[\\/:*?"<>|]', "_", seg)
                         for seg in name.replace("\\", "/").split("/") if seg not in ("", ".", "..")]
                if not parts:
                    continue
                target = os.path.join(str(dest), *parts)
                # 防 zip-slip：只允许落在 dest 内
                if not os.path.abspath(target).startswith(os.path.abspath(str(dest))):
                    continue
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
        return str(dest)

    def read(self, ctx: ParseContext) -> list[pd.DataFrame]:
        """读原始文件为 DataFrame 列表。子类可覆盖以处理特殊表头。"""
        files = self.collect_files(ctx)
        if not files:
            raise BillParseError(
                f"未找到 {self.display_name} 账单文件",
                detail={
                    "platform": self.platform,
                    "searched": list(ctx.file_paths or []),
                    "patterns": list(self.file_patterns),
                    "hint": ("请确认给的是该平台账单目录/文件/原始下载 zip。"
                             "若已给 zip 仍找不到：可能下载错了平台或账期，"
                             "请核对压缩包内容后与用户确认"),
                },
            )
        opts = self.read_options()
        # index_col 允许 read_option 覆盖（有赞 csv 行尾多空字段，须 False 防错位）
        csv_opts = dict(opts)
        idx_col = csv_opts.pop("index_col", None)
        frames: list[pd.DataFrame] = []
        for f in files:
            try:
                if f.lower().endswith((".xlsx", ".xls")):
                    df = pd.read_excel(f, index_col=None, **self._excel_opts(opts))
                else:
                    df = pd.read_csv(f, index_col=idx_col, **csv_opts)
            except UnicodeDecodeError:
                # 编码兜底：utf-8 失败换 gb18030（平台账单常见）
                fallback = dict(csv_opts)
                fallback["encoding"] = "gb18030"
                df = pd.read_csv(f, index_col=idx_col, **fallback)
            except Exception as exc:
                raise BillParseError(
                    f"读取账单文件失败：{os.path.basename(f)}",
                    detail={"error": f"{type(exc).__name__}: {exc}"[:300], "file": f},
                ) from exc
            df = df.copy()
            df["__source_file__"] = os.path.basename(f)
            df["__source_row__"] = range(len(df))
            frames.append(df)
        return frames

    @staticmethod
    def _excel_opts(opts: dict[str, Any]) -> dict[str, Any]:
        """read_excel 不接受 encoding/skiprows 之外的 csv 参数，做过滤。"""
        allowed = {"sheet_name", "skiprows", "dtype", "usecols", "header", "skipfooter"}
        return {k: v for k, v in opts.items() if k in allowed}

    # ---------------- 清洗与归一

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        """平台特有清洗。默认去掉汇总尾行（含「合计」字样的行）。"""
        out = df.copy()
        drop_tail = int(self.rule("read_option", "drop_tail", "0") or 0)
        if drop_tail > 0 and len(out) > drop_tail:
            out = out.iloc[:-drop_tail]
        return out

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """归一为标准五元结构。子类通常覆盖此方法。

        基类实现：按 column_map 规则重命名 + 类型转换 + 金额正负处理，
        适配「列名不同但结构一致」的大多数平台。
        """
        out = df.copy()
        cmap = self.column_map()

        def src(std: str) -> str | None:
            col = cmap.get(std)
            if col and col in out.columns:
                return col
            if std in out.columns:
                return std
            return None

        order_col = src(STD_ORDER_NO)
        date_col = src(STD_FUND_DATE)
        cat_col = src(STD_BIZ_CATEGORY)
        receipt_col = src(STD_RECEIPT)
        refund_col = src(STD_REFUND)

        missing = [
            name
            for name, col in (
                (STD_ORDER_NO, order_col),
                (STD_FUND_DATE, date_col),
            )
            if not col
        ]
        if missing:
            raise BillParseError(
                f"{self.display_name} 账单缺少必要字段：{', '.join(missing)}",
                detail={
                    "platform": self.platform,
                    "missing": missing,
                    "available_columns": [str(c) for c in out.columns][:40],
                    "hint": "请在「账单整理 · 平台规则」页补全列名映射",
                },
            )

        res = pd.DataFrame()
        res[STD_ORDER_NO] = self._as_text(out[order_col])
        res[STD_FUND_DATE] = self._as_date(out[date_col])

        if cat_col:
            res[STD_BIZ_CATEGORY] = self._as_text(out[cat_col]).fillna("")
        else:
            res[STD_BIZ_CATEGORY] = BIZ_GOODS

        if receipt_col:
            res[STD_RECEIPT] = self._as_amount(out[receipt_col])
        else:
            res[STD_RECEIPT] = 0.0
        if refund_col:
            # 平台账单的支出列通常是负数或正数两种口径，统一取绝对值
            res[STD_REFUND] = self._as_amount(out[refund_col]).abs()
        else:
            res[STD_REFUND] = 0.0

        # 溯源列透传
        for c in ("__source_file__", "__source_row__"):
            if c in out.columns:
                res[c] = out[c].values

        return res

    # ---------------- 业务编码映射
    # 注：通用 apply_biz_codes 已于 2026-11-18 总体 review 移除（死代码）：
    # 各平台适配器直接消费 biz_code_map()（整表接管语义 + 各自 fallback 字典），
    # 通用版「未命中置 None」会丢各类平台特有的兜底口径，勿复活。

    # ---------------- 日期过滤

    def filter_by_period(self, df: pd.DataFrame, ctx: ParseContext) -> pd.DataFrame:
        """按账期起止过滤资金日期（原脚本每个函数末尾都有这一段）。"""
        if not ctx.s_date or not ctx.e_date or df.empty:
            return df
        if STD_FUND_DATE not in df.columns:
            return df
        d = pd.to_datetime(df[STD_FUND_DATE], errors="coerce")
        mask = (d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))
        return df[mask].copy()

    # ---------------- 出口

    def bill_rows(self, ctx: ParseContext, df: pd.DataFrame) -> list[BillRow]:
        """标准五元表 → BillRow 列表（金额转分）。"""
        if df is None or df.empty:
            return []
        rows: list[BillRow] = []
        records = df.to_dict(orient="records")
        for rec in records:
            rows.append(
                BillRow(
                    order_no=str(rec.get(STD_ORDER_NO) or "").strip() or NO_ORDER,
                    fund_date=str(rec.get(STD_FUND_DATE) or "")[:10],
                    biz_category=str(rec.get(STD_BIZ_CATEGORY) or "").strip(),
                    receipt_fen=to_fen(rec.get(STD_RECEIPT)),
                    refund_fen=to_fen(rec.get(STD_REFUND)),
                    source_file=str(rec.get("__source_file__") or ""),
                    source_row=(
                        int(rec["__source_row__"])
                        if rec.get("__source_row__") is not None
                        and str(rec.get("__source_row__")) != ""
                        else None
                    ),
                )
            )
        return rows

    def parse(self, ctx: ParseContext) -> list[BillRow]:
        """完整流水线：read → clean → normalize → 账期过滤 → BillRow。"""
        frames = self.read(ctx)
        cleaned = [self.clean(f) for f in frames]
        merged = pd.concat(cleaned, ignore_index=True) if cleaned else pd.DataFrame()
        if merged.empty:
            return []
        norm = self.normalize(merged)
        norm = self.filter_by_period(norm, ctx)
        return self.bill_rows(ctx, norm)

    def goods_rows(self, ctx: ParseContext) -> list[BillRow]:
        """只取业务类别为「货款」的行（SKU 匹配层的输入口径）。"""
        return [r for r in self.parse(ctx) if r.biz_category == BIZ_GOODS]

    #: 匹配层输入是否等价于「账单层筛货款」。
    #:
    #: 单源平台（淘系/抖音/拼多多）为 True —— 账单行里 biz_category=='货款' 就是
    #: SKU 对账的全部输入。
    #:
    #: 多源平台（京东）为 **False**：账单层是资金流水全景（钱包 24 类 + 结算单，
    #: 且商家结算款已被结算单替换），而匹配层输入是另外三源的子集
    #: （结算单货款 + 钱包「货款退款」改判货款 + E卡货款，E卡根本不进账单层）。
    #: 2026-11-14 JD001 实测：按账单层筛货款会与影刀基准
    #: 276,668.76 / 185,311.32 元对不上（错取钱包货款、漏 E卡与货款退款）。
    goods_input_matches_bill: bool = True

    def goods_input_rows(self, ctx: ParseContext, bill_rows: list[BillRow]) -> list[BillRow]:
        """匹配层输入行。

        bill_rows 是本次 parse() 的结果，单源平台直接从中筛，避免二次解析
        （拼多多单店可达 25 万行，重解析一次代价很高）。
        多源平台忽略 bill_rows，走子类 goods_rows() 的专属口径。
        """
        if self.goods_input_matches_bill:
            return [r for r in bill_rows if r.biz_category == BIZ_GOODS]
        return self.goods_rows(ctx)

    def match_window(self, ctx: ParseContext) -> tuple[str, str]:
        """SKU 对账「补录段」筛存货异动时间用的账期窗口。

        默认与账单账期相同。多源平台可覆盖 —— 京东返回钱包备注资金日期的
        min~max（原 main.py 把 jd_wallet_dict['开始日期'/'结束日期'] 传给
        look_sku.result，而不是用户填的日期）。

        2026-11-14 JD001 实测：用户账期 08-01~08-31、钱包反推 07-31~08-30，
        两端各差一天就让补录行在收款/退款侧翻转，结算金额差 -874473 / +87074 分。
        """
        return ctx.s_date, ctx.e_date

    # ---------------- 基准格式导出（对齐影刀 bill_arr 各 *_arr 的 to_excel 段）

    def export_books(self, ctx: ParseContext) -> list[dict[str, Any]]:
        """按影刀基准格式产出 Excel 工作簿清单。

        返回 [{infix, tail, prefix?, sheets:[{name, df, kind}]}]，文件名由 pipeline 拼：
          {prefix or bill_type}{infix}_{店铺名称}_{店铺编码}_{账单周期中文}{tail}.xlsx
        （与影刀 get_save_dir + 各 *_arr 的文件名完全同构，如
         抖音_抖音_秋水伊人官方旗舰店_DY007_2026年08月-2026年08月_账单整理.xlsx）
          · kind 'yuan' = df 已是元、原样写出（基准明细/透视）；
                 'fen'  = 金额分列，导出时转元（插件统一口径附加 sheet）。
        基类默认返回空列表 → pipeline 回落统一三 sheet 导出（GenericAdapter 等）。
        """
        return []

    #: 基准文件名里的 bill_type（多数平台=display_name；淘系基准用「淘系店铺」）
    bill_type: str = ""

    @staticmethod
    def chuanyun_from_pivot(pivot: pd.DataFrame) -> pd.DataFrame:
        """氚云导入模板（原各 *_arr 的 income_pivot/expense_pivot 段，逐平台同款）。

        输入是 8 列透视（店铺名称/店铺编码/账单周期/账单类型/业务类别/收款/退款/现金流），
        收款>0 → 资金方向'收款'，退款>0 → '付款'；金额单位元。
        """
        cols = ["店铺名称", "业务类别", "资金方向", "金额"]
        if pivot is None or pivot.empty:
            return pd.DataFrame(columns=cols)
        parts = []
        for col, label in (("收款", "收款"), ("退款", "付款")):
            if col not in pivot.columns:
                continue
            sub = pivot[pivot[col] > 0]
            if sub.empty:
                continue
            g = sub.groupby(["店铺名称", "业务类别"], as_index=False, sort=True)[col].sum()
            g = g.rename(columns={col: "金额"})
            g["资金方向"] = label
            parts.append(g)
        if not parts:
            return pd.DataFrame(columns=cols)
        out = pd.concat(parts, ignore_index=True)
        return out.loc[:, cols]

    @staticmethod
    def pivot_from_detail(
        detail: pd.DataFrame,
        ctx: ParseContext,
        bill_type: str,
        *,
        category_col: str = STD_BIZ_CATEGORY,
        receipt_col: str = STD_RECEIPT,
        refund_col: str = STD_REFUND,
        with_cashflow: bool = False,
    ) -> pd.DataFrame:
        """8 列基准透视（原 res_pivot：groupby 业务类别 sum 收款/退款 + 店铺信息）。

        with_cashflow=True 时现金流按行先算再汇总（京东口径：收款-退款逐行）。
        金额单位元；行序按业务类别排序（与 pd.pivot_table 的 index 排序一致）。
        """
        cols = ["店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流"]
        if detail is None or detail.empty:
            return pd.DataFrame(columns=cols)
        df = detail.copy()
        if with_cashflow and "现金流" in df.columns:
            g = df.groupby(category_col, as_index=False, sort=True).agg(
                **{receipt_col: (receipt_col, "sum"), refund_col: (refund_col, "sum"),
                   "现金流": ("现金流", "sum")})
        else:
            g = df.groupby(category_col, as_index=False, sort=True).agg(
                **{receipt_col: (receipt_col, "sum"), refund_col: (refund_col, "sum")})
            g["现金流"] = g[receipt_col] - g[refund_col]
        g = g.rename(columns={category_col: "业务类别", receipt_col: "收款", refund_col: "退款"})
        g["店铺名称"] = ctx.shop_name or ctx.shop_code
        g["店铺编码"] = ctx.shop_code
        g["账单周期"] = ctx.period_cn
        g["账单类型"] = bill_type
        return g.loc[:, cols]

    # ---------------- 工具

    @staticmethod
    def _as_text(series: pd.Series) -> pd.Series:
        s = series.astype("string").fillna("")
        return s.str.replace(r'="', "", regex=False).str.replace('"', "", regex=False).str.strip()

    @staticmethod
    def _as_date(series: pd.Series) -> pd.Series:
        s = series.astype("string").fillna("")
        s = s.str.replace(r'="', "", regex=False).str.replace('"', "", regex=False).str.strip()
        d = pd.to_datetime(s, errors="coerce")
        return d.dt.strftime("%Y-%m-%d").fillna("")

    @staticmethod
    def _as_amount(series: pd.Series) -> pd.Series:
        s = series.astype("string").fillna("0")
        s = (
            s.str.replace(",", "", regex=False)
            .str.replace("￥", "", regex=False)
            .str.replace("¥", "", regex=False)
            .str.strip()
        )
        return pd.to_numeric(s, errors="coerce").fillna(0.0).astype("float64")

    @staticmethod
    def require_columns(df: pd.DataFrame, cols: Sequence[str], platform: str) -> None:
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise BillParseError(
                f"{platform} 账单缺少字段：{', '.join(missing)}",
                detail={
                    "missing": missing,
                    "available_columns": [str(c) for c in df.columns][:40],
                    "hint": ("列头与平台预期不符：先确认是否下载错了平台/账期/报表类型"
                             "（与用户核对原始下载来源），再考虑平台改版补列名映射"),
                },
            )
