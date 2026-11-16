"""成本匹配（承接 cost_match.py 696 行：cost_base / cost_match / final_pivot / chuanyun）。

成本口径：
  · 两种取价模式：按 sku 计算 / 按款号计算（原 cost_base 参数）
  · 标准成本 = 数量 × 标准成本单价
  · 发货成本 = 数量 × 采购单价；唯品会等 B2B 平台发货成本改用标准成本（原脚本特判）
  · 含税/不含税由 is_include_tax 控制
  · 「款号缺失」与「款号有但成本表无此款」分开标记，原脚本两者都被压成 0 无法区分
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import pandas as pd

from .config import ADVICE_OFFSET_SHIP, MATCHED, UNMATCHED
from .errors import BillParseError
from .money import CENT, from_fen, safe_int, to_fen

MODE_SKU = "sku"
MODE_STYLE = "style"

# 成本表按月一个 sheet（'1月末' … '12月末'）；原脚本由用户在弹窗里手填 sheet 名。
# 这里按账期推导默认值，避免静默读到 1 月的单价（2026-11-13 实测踩坑：
# 不传 sheet_name 时 pandas 默认读第一个 sheet='1月末'，采购单价全线偏低）。
COST_SHEET_TEMPLATE = "{month}月末"


def cost_sheet_for_period(period: str, fallback: str = "") -> str | int:
    """账期 'YYYY-MM' → 成本表 sheet 名（'8月末'）。解析不出来时回落 fallback 或 0。"""
    s = str(period or "").strip()
    try:
        month = int(s.replace("/", "-").split("-")[1])
        if 1 <= month <= 12:
            return COST_SHEET_TEMPLATE.format(month=month)
    except (IndexError, ValueError):
        pass
    return fallback if fallback else 0

# 成本缺失原因（原脚本统一填 0，无法区分）
MISSING_NO_STYLE = "款号缺失"
MISSING_NO_COST = "成本表无此款"
MISSING_NONE = ""


@dataclass
class CostResult:
    frame: pd.DataFrame
    matched_rows: int = 0
    missing_no_style: int = 0
    missing_no_cost: int = 0
    total_std_cost_fen: int = 0
    total_ship_cost_fen: int = 0
    detail: dict = field(default_factory=dict)


def load_cost_base(
    file_path: str,
    *,
    mode: str = MODE_SKU,
    sheet_name: str | int = 0,
    include_tax: bool = False,
) -> dict[str, dict[str, int]]:
    """读成本单价表 → {key: {'unit_cost_fen': int, 'spec': str}}。

    原 cost_match.cost_base：物料编码 / 规格型号 强制 str，单价 fillna(0) 转 float。
    """
    try:
        df = pd.read_excel(
            file_path,
            index_col=None,
            dtype={"物料编码": "str", "规格型号": "str"},
            sheet_name=sheet_name,
        )
    except Exception as exc:
        raise BillParseError(
            f"读取成本单价表失败：{type(exc).__name__}",
            detail={"file": str(file_path), "hint": str(exc)[:200]},
        ) from exc

    if df.empty:
        return {}

    key_col = "物料编码" if mode == MODE_SKU else "规格型号"
    if key_col not in df.columns:
        # 按款号模式常用「款号」列名
        alt = "款号" if mode == MODE_STYLE else "sku"
        key_col = alt if alt in df.columns else key_col
    if key_col not in df.columns:
        raise BillParseError(
            "成本单价表缺少主键列",
            detail={
                "expected": ["物料编码", "规格型号", "款号", "sku"],
                "available_columns": [str(c) for c in df.columns][:30],
                "hint": "请确认成本表列名，或在「数据源」页重新上传",
            },
        )

    price_col = "含税单价" if include_tax and "含税单价" in df.columns else "单价"
    if price_col not in df.columns:
        raise BillParseError(
            "成本单价表缺少单价列",
            detail={"available_columns": [str(c) for c in df.columns][:30]},
        )

    out = df.loc[:, [key_col, price_col]].copy()
    out[key_col] = out[key_col].astype("string").fillna("").str.strip()
    out = out[out[key_col] != ""]

    # 同一主键多行时取首行（原脚本 drop_duplicates 语义）
    out = out.drop_duplicates(subset=[key_col], keep="first")

    result: dict[str, dict[str, int]] = {}
    for key, price in zip(out[key_col].tolist(), out[price_col].tolist()):
        # unit_cost_raw 保留成本表的**原始精度**（如 238.40708），发货成本必须用它算。
        # 2026-08 成本表实测：13378 行里 11703 行单价小数超过 2 位，只用量化到分的
        # unit_cost_fen 先乘数量，会与基准（元域乘完再 round）差 1 分。
        result[str(key)] = {
            "unit_cost_fen": to_fen(price),
            "unit_cost_raw": str(price).strip(),
            "spec": "",
        }
    return result


def apply_cost(
    df: pd.DataFrame,
    cost_map: dict[str, dict[str, int]],
    *,
    mode: str = MODE_STYLE,
    force_std_as_ship: bool = False,
) -> CostResult:
    """给匹配明细补标准成本与发货成本（金额单位：分）。

    两条成本线来源**不同**，2026-08 真实基准逐行验证（DY014 12/12、DY023 300/300）：
      · 标准成本单价 ← 数据库 AI_product_info.standard_cost（enrich_from_db 已写入）
        标准成本 = 数量 × 标准成本单价
      · 采购单价 ← 本地成本表，按**款号**查（原 cost_match.cost_base 的
        「成本价按款号计算」分支：dict(zip(规格型号, 单价))）
        发货成本 = 数量 × 采购单价，逐行 round 到分（原脚本 round(2)）
    成本表**不得**覆盖标准成本单价：两者是不同口径（标准成本=吊牌体系，
    采购单价=当月实际加权成本），基准里 300/300 行的标准成本单价都等于 DB 值、
    0 行等于成本表值。

    mode 默认改为 MODE_STYLE：成本表「规格型号」列才是 13 位款号，
    「物料编码」列实测只有 CO/QS/C3 三个品牌值，按 sku 查必然全不命中。
    force_std_as_ship：唯品会 FR 等 B2B 平台发货成本改用标准成本（原脚本特判）。
    """
    out = df.copy()
    if out.empty:
        return CostResult(frame=out)

    for c in ("sku", "款号", "数量", "标准成本单价", "采购单价", "标准成本", "发货成本"):
        if c not in out.columns:
            out[c] = ""

    qty = [safe_int(v) for v in out["数量"].tolist()]
    out["数量"] = qty

    # 标准成本单价：沿用 enrich_from_db 写入的 DB 值，只规整为分
    std_unit = [safe_int(v) for v in out["标准成本单价"].tolist()]
    out["标准成本单价"] = std_unit
    out["标准成本"] = [u * q for u, q in zip(std_unit, qty)]

    # 采购单价：成本表按款号（或 sku）查
    key_series = out["款号"] if mode == MODE_STYLE else out["sku"]
    keys = key_series.astype("string").fillna("").str.strip()

    purchase_fen: list[int] = []
    purchase_raw: list[Any] = []      # 原始精度单价，仅供发货成本计算
    miss_reason: list[str] = []
    for k in keys.tolist():
        if not k or k.lower() in ("nan", "none") or k == PLACEHOLDER_STYLE:
            purchase_fen.append(0)
            purchase_raw.append(None)
            miss_reason.append(MISSING_NO_STYLE)
            continue
        hit = cost_map.get(k)
        if hit is None:
            purchase_fen.append(0)
            purchase_raw.append(None)
            miss_reason.append(MISSING_NO_COST)
        else:
            purchase_fen.append(int(hit.get("unit_cost_fen") or 0))
            raw = hit.get("unit_cost_raw")
            purchase_raw.append(raw if raw not in (None, "") else None)
            miss_reason.append(MISSING_NONE)

    out["采购单价"] = purchase_fen
    out["成本缺失原因"] = miss_reason

    if force_std_as_ship:
        out["发货成本"] = [int(v) for v in out["标准成本"].tolist()]
    else:
        out["发货成本"] = [_round_ship(p, q) for p, q in zip(purchase_raw, qty)]

    return CostResult(
        frame=out,
        matched_rows=int(sum(1 for r in miss_reason if r == MISSING_NONE)),
        missing_no_style=int(sum(1 for r in miss_reason if r == MISSING_NO_STYLE)),
        missing_no_cost=int(sum(1 for r in miss_reason if r == MISSING_NO_COST)),
        total_std_cost_fen=int(sum(std_unit[i] * qty[i] for i in range(len(qty)))),
        total_ship_cost_fen=int(sum(out["发货成本"].tolist())),
        detail={
            "mode": mode,
            "cost_keys": len(cost_map),
            "force_std_as_ship": force_std_as_ship,
        },
    )


def _round_ship(unit_raw: Any, qty: int) -> int:
    """发货成本（分）= round(数量 × 采购单价_元, 2) → 分。

    严格复刻原脚本 cost_match 第 81 行：
        df['发货成本'] = (df['数量'] * df['采购单价']).round(2)
    即**先在元域乘完再 round**，而不是「单价先量化到分再乘数量」。

    两者不等价，且不是极端值才触发：2026-08 成本表 8月末 sheet 共 13378 行，
    其中 **11703 行单价小数超过 2 位**（如 824409B151261 = 238.40708）。
    实测 PDD003 收款匹配：
        元域乘完再 round → 3 × 238.40708 = 715.22124 → 715.22 → 71522 分（= 基准）
        先量化单价再乘   → 23841 × 3 = 71523 分（差 1 分）
    单价行数多时这类 ±1 分会累积，违反 ALLOWED_TOLERANCE_FEN = 0 的门禁。

    注意「采购单价」这一**展示列**仍存量化到分的值（与基准 Excel 逐行相等，
    基准里该列就是 238.40708，读出后 round 到分同为 23841），只有发货成本的
    计算路径必须用原始精度。
    """
    if unit_raw is None or unit_raw == "":
        return 0
    try:
        price = Decimal(str(unit_raw).strip())
    except Exception:
        return 0
    if price.is_nan() or price.is_infinite():
        return 0
    return int((price * Decimal(int(qty))).quantize(CENT, rounding=ROUND_HALF_UP) * 100)


# 品牌字典（原 look_sku.brand_dict：库内中文名 → 报表用简码）
BRAND_DICT: dict[str, str] = {
    "FV": "FV", "LC": "LC", "Fairy": "FR", "秋水伊人": "QS", "FirstView": "FV",
    "娜依": "娜依", "FR": "FR", "QSYR": "QS", "可可尼": "CO", "LIKEME": "LIKEME",
    "myall": "MyAll", "外购样衣": "外购样衣", "COCOON": "CO", "IVE": "IVE",
    "待定": "待定", "C3": "C3", "8": "8", "MYALL": "MyAll",
}

# 缺失占位（原脚本 fillna 的字面量；透视分组键必须非空，否则 pandas 会丢组）
PLACEHOLDER_STYLE = "无款号"
PLACEHOLDER_NAME = "无商品名称"
PLACEHOLDER_YEAR = "无年份"
PLACEHOLDER_SEASON = "无季节"
PLACEHOLDER_BRAND = "无品牌"
PLACEHOLDER_PRICE = "无吊牌价"

# 富化列 → (占位符, 是否去后缀)
_ENRICH_TEXT_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("款号", PLACEHOLDER_STYLE, False),
    ("商品名称", PLACEHOLDER_NAME, False),
    ("年份", PLACEHOLDER_YEAR, True),      # 原脚本 str.replace('年','')
    ("季节", PLACEHOLDER_SEASON, True),    # 原脚本 str.replace('季','')
    ("品牌", PLACEHOLDER_BRAND, False),
    ("吊牌价", PLACEHOLDER_PRICE, False),
)


def _brand_code(value: Any) -> str:
    """品牌中英文名 → 报表简码；字典没有的回落空串（原脚本 .get(x) 会得 None，此处不写 'None'）。"""
    key = str(value or "").strip()
    if not key or key.lower() in ("nan", "none"):
        return PLACEHOLDER_BRAND
    return BRAND_DICT.get(key, "")


def enrich_from_db(
    df: pd.DataFrame,
    product_df: pd.DataFrame,
    order_date_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """用 ADB 商品主数据补款号/品类/年份/季节/品牌/吊牌价/标准成本单价，并回填订单日期。

    对应原 look_sku.result 中的「匹配产品信息」段 + trade_no_fill 段。
    与原脚本逐条对齐（2026-08 基准实测）：
      · 按 sku 映射 6 列 + 标准成本单价；标准成本单价缺失 fillna(0)
      · 缺失值填中文占位（无款号 / 无商品名称 / 无年份 / 无季节 / 无品牌 / 无吊牌价），
        **不是空串**：这些列是透视分组键，空串会让 pandas groupby 丢组，
        且基准文件里确实是「无款号」这类字面量
      · 品牌再过一次 brand_dict（秋水伊人→QS、可可尼→CO …）；字典外 → 空串，
        不写入字符串「None」（原脚本 .get(x) 无默认值的缺陷）
      · 吊牌价按「元 → 分」存储（本模块金额一律分）
      · 订单日期缺失且非秒退 → 按订单号回填
    """
    out = df.copy()
    if out.empty:
        return out

    # 1) 商品主数据映射
    maps: dict[str, dict[str, Any]] = {}
    unit_price: dict[str, Any] = {}
    if product_df is not None and not product_df.empty and "sku" in product_df.columns:
        pdf = product_df.copy()
        pdf["sku"] = pdf["sku"].astype("string").fillna("").str.strip()
        cols = [c for c in ("款号", "商品名称", "品类", "年份", "季节", "品牌",
                            "吊牌价", "标准成本单价") if c in pdf.columns]
        idx = pdf.set_index("sku")[cols]
        for col in cols:
            maps[col] = idx[col].to_dict()
        unit_price = maps.get("标准成本单价", {})

    sku_keys = out["sku"].astype("string").fillna("").str.strip() if "sku" in out.columns else None

    for col, placeholder, strip_suffix in _ENRICH_TEXT_COLUMNS:
        if col not in out.columns:
            out[col] = None
        if sku_keys is None or col not in maps:
            series = out[col].astype("string").fillna("")
        else:
            series = sku_keys.map(maps[col]).astype("string").fillna("")
        if strip_suffix:
            suffix = "年" if col == "年份" else "季"
            series = series.str.strip().str.replace(suffix, "", regex=False)
        else:
            series = series.str.strip()
        # 品牌单独走字典
        if col == "品牌":
            series = series.map(_brand_code)
        elif col == "吊牌价":
            out[col] = [to_fen(v) for v in series.tolist()]
            continue
        out[col] = series.mask(series == "", placeholder).tolist()

    # 品类不进占位体系（原脚本未 fillna），仅去空白
    if "品类" in maps:
        out["品类"] = (
            sku_keys.map(maps["品类"]).astype("string").fillna("").str.strip().tolist()
            if sku_keys is not None else out.get("品类", "")
        )

    # 标准成本单价：元 → 分；缺失填 0（原脚本 fillna(0.00)）
    if sku_keys is not None and unit_price:
        out["标准成本单价"] = [to_fen(unit_price.get(k, 0)) for k in sku_keys.tolist()]
    elif "标准成本单价" not in out.columns:
        out["标准成本单价"] = 0

    # 2) 订单日期回填
    if order_date_map and "订单编号" in out.columns:
        if "订单日期" not in out.columns:
            out["订单日期"] = ""
        need = out["订单日期"].astype("string").fillna("").str.strip().isin(["", "nan", "None", "NaT"])
        # 秒退行不回填（原脚本 mask 条件含「是否秒退判断 != 是」）
        if "是否秒退判断" in out.columns:
            need = need & (out["是否秒退判断"].astype("string").fillna("") != "是")
        if need.any():
            filled = out.loc[need, "订单编号"].astype("string").fillna("").map(order_date_map)
            out.loc[need, "订单日期"] = filled.fillna("").astype("string").fillna("").tolist()

    return out


def cost_summary_by_advice(df: pd.DataFrame) -> list[dict]:
    """按处理意见汇总成本（管理页成本卡片的载荷）。"""
    if df is None or df.empty:
        return []
    g = df.groupby("处理意见", as_index=False).agg(
        rows=("订单编号", "size"),
        qty=("数量", "sum"),
        std_cost_fen=("标准成本", "sum"),
        ship_cost_fen=("发货成本", "sum"),
        allocated_fen=("货款分摊金额", "sum"),
        settle_fen=("结算金额", "sum"),
    )
    rows = []
    for rec in g.to_dict(orient="records"):
        rec["std_cost_yuan"] = float(from_fen(rec.get("std_cost_fen")))
        rec["ship_cost_yuan"] = float(from_fen(rec.get("ship_cost_fen")))
        rec["allocated_yuan"] = float(from_fen(rec.get("allocated_fen")))
        rec["settle_yuan"] = float(from_fen(rec.get("settle_fen")))
        rows.append(rec)
    return rows
