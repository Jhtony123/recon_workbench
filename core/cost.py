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
from .money import CENT, safe_int, to_fen

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

# cost 阶段「特殊情况说明」列取值（spec Q6，用户批准的基准附加列，明细第 32 列）。
# 与内部诊断列「成本缺失原因」分开：前者进交付 Excel（措辞照用户确认），后者仅底稿。
SPECIAL_NO_STYLE = "无款号"
SPECIAL_NO_COST = "成本表无此款"
SPECIAL_NONE = ""
SPECIAL_NOTE_COLUMN = "特殊情况说明"

# cost 阶段采购单价**展示列**的原始精度载体（内部列，出口层原样写出，不经分→元量化）。
# spec A2：基准该列存全精度小数（52.212389380531），不是量化到分的值。
PURCHASE_RAW_COLUMN = "采购单价_raw"


@dataclass
class CostResult:
    frame: pd.DataFrame
    matched_rows: int = 0
    missing_no_style: int = 0
    missing_no_cost: int = 0
    total_std_cost_fen: int = 0
    total_ship_cost_fen: int = 0
    detail: dict = field(default_factory=dict)
    #: cost 阶段：成本表查不到的款号清单（进运行汇报与底稿 manifest，spec Q6）
    missing_styles: list = field(default_factory=list)


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
            f"读取成本单价表失败：{type(exc).__name__}（sheet=「{sheet_name}」不存在或不可读）",
            detail={"file": str(file_path), "sheet": str(sheet_name),
                    "hint": str(exc)[:200]},
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

    # 空值语义（2026-11-17 影刀10月基准逐行实测，HYK 收235/退178 全对齐）：
    #   · 账单侧行（货款金额非空）→ NaN：原脚本 L659/L695 初始化 ""，本阶段不回填，
    #     Excel 空白（收款已匹配 177 行、退款账单行 115+47 行全是 NaN）。
    #   · 补录行（货款金额 NaN，_blank_oms_rows 置 None）→ 0：原脚本 L917/L943
    #     df_out/df_in 显式赋 0.00（收款未匹配补录 58 行、退款补录 16 行全是 0）。
    #   · 秒退重匹配行 → 0：原脚本 L824 merge_receipt_2['采购单价']=0.00。
    #     插件 rematch 里赋 0，但货款金额非空会被上一条误判 NaN——用「数据来源=收款
    #     且 单据类型=退款单」识别（重匹配命中才有退款单单据类型）。
    gross_null = out["货款金额"].map(
        lambda v: v is None or (isinstance(v, float) and v != v)
        or (isinstance(v, str) and not v.strip())
    ) if "货款金额" in out.columns else pd.Series(False, index=out.index)
    # 秒退行 → 0 仅限**收款侧**：原脚本 L824 merge_receipt_2['采购单价']=0.00
    # 只作用于收款秒退重匹配段（含未命中的 47 行 sku/单号空）。
    # 退款侧秒退交叉标记行（L885-887）不改采购单价，保持 ""→NaN
    # （2026-11-17 HYK 基准实测：收款秒退 47 行=0，退款秒退 47 行=NaN）。
    instant_rec = pd.Series(False, index=out.index)
    if {"是否秒退判断", "数据来源"}.issubset(out.columns):
        from .config import SRC_RECEIPT as _SR
        instant_rec = (
            (out["是否秒退判断"].astype("string").fillna("") == "是")
            & (out["数据来源"].astype("string").fillna("") == _SR)
        )
    keep_zero = gross_null | instant_rec
    out["采购单价"] = [
        purchase_fen[i] if keep_zero.iloc[i] else None
        for i in range(len(purchase_fen))
    ]
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


def _is_na(v: Any) -> bool:
    """None / NaN / pd.NA / 字面 nan/none 串 → True（底稿 csv 读回的空值形态）。"""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "nat")


def apply_cost_stage(
    df: pd.DataFrame,
    cost_map: dict[str, dict[str, int]],
    *,
    platform: str = "",
    mode: str = MODE_STYLE,
) -> CostResult:
    """cost 阶段回填（spec C2，独立分支——**不复用** apply_cost 的 match 语义）。

    输入 = SKU 对账底稿 match_detail（分域全列）。与 match 阶段的本质区别：
      · 标准成本/标准成本单价 **一律透传禁重算**（A1：保 VIP FR 结算金额特例）；
      · 采购单价全 fillna(0)，无 NaN 语义；展示列带原始精度（A2，PURCHASE_RAW_COLUMN）；
      · 占位符重写（C2.4）：款号 None/空→无款号、吊牌价→无吊牌价、
        商品名称/年份/季节→占位字面量；品牌/品类按平台（B2C→未知；
        VIP 品类→字面 'None'、季节空→无季节；AKC 季节空保持空白）——
        2026-11-18 单元格级基准实测（probe_cost_cells3）；
      · id 空值→**空白**（基准实测 NoneType；spec A5 原记录「字面串 None」
        是 pandas 读表假象，已按实测修正）；
      · FR 行级特例（A6）：店铺名称含「唯品会」& 品牌==FR → 发货成本=标准成本，
        采购单价=0（基准实测 FR 行采购单价全 0）；
      · 特殊情况说明列（Q6）：无款号 / 成本表无此款 / 空；FR 特例不标注。
    明细保持正数（A4）：数量 abs，货款/分摊/结算不动；取负只发生在透视。
    """
    out = df.copy()
    if out.empty:
        return CostResult(frame=out)

    for c in ("sku", "款号", "数量", "标准成本单价", "采购单价", "标准成本", "发货成本"):
        if c not in out.columns:
            out[c] = ""

    # 数量 abs（C2.1；退款侧底稿数量本已为正，abs 幂等）
    qty = [abs(safe_int(v)) for v in out["数量"].tolist()]
    out["数量"] = qty

    # 标准成本/标准成本单价：透传（A1）。safe_int 只规整类型，不改值。
    out["标准成本单价"] = [safe_int(v) for v in out["标准成本单价"].tolist()]
    out["标准成本"] = [safe_int(v) for v in out["标准成本"].tolist()]

    # ---- 占位符重写（先于查表：款号 None/空 → 无款号）----
    is_vip = platform == "vip"
    is_akc = platform == "akc"

    def _rewrite(col: str, placeholder: str) -> None:
        if col not in out.columns:
            out[col] = placeholder
            return
        out[col] = [placeholder if _is_na(v) else str(v).strip() or placeholder
                    for v in out[col].tolist()]

    _rewrite("款号", PLACEHOLDER_STYLE)
    _rewrite("商品名称", PLACEHOLDER_NAME)
    _rewrite("年份", PLACEHOLDER_YEAR)
    # 季节：B2C/VIP 空→无季节；AKC 空保持空白（基准实测 AKC 无空季节行，
    # 但 B2B 季节 DB 空串在 SKU 阶段导出为空白，cost 阶段 astype(str) 不产生占位）
    if is_akc:
        if "季节" in out.columns:
            out["季节"] = ["" if _is_na(v) else str(v).strip() for v in out["季节"].tolist()]
    else:
        _rewrite("季节", PLACEHOLDER_SEASON)
    # 品牌：B2C 空→未知；B2B 账单自带品牌，空→未知兜底
    if "品牌" in out.columns:
        out["品牌"] = ["未知" if _is_na(v) else str(v).strip() or "未知"
                    for v in out["品牌"].tolist()]
    else:
        out["品牌"] = "未知"
    # 品类：B2C 空→未知（match 阶段透视已 fillna）；B2B（VIP/AKC）空→字面 'None'
    # （影刀读 Excel 后 astype(str) 副作用；VIP 基准 70498 行、AKC 匹配透视
    # 'None' 组均实测，probe_cost_cells3/4）
    if "品类" in out.columns:
        if is_vip or is_akc:
            out["品类"] = ["None" if _is_na(v) else str(v).strip() or "None"
                        for v in out["品类"].tolist()]
        else:
            out["品类"] = ["未知" if _is_na(v) else str(v).strip() or "未知"
                        for v in out["品类"].tolist()]
    # 吊牌价：空/None→无吊牌价；有值保持**分域 int**（透视 _assemble_pivot 按分
    # 量化、出口层再转元文本）。基准是 '688.0000'/'1118.0' 文本列，数值等价即可。
    if "吊牌价" in out.columns:
        out["吊牌价"] = [
            PLACEHOLDER_PRICE if _is_na(v) else safe_int(v)
            for v in out["吊牌价"].tolist()
        ]
    # id：空值→空白（基准实测 NoneType；A5 修正）
    if "id" in out.columns:
        out["id"] = [None if _is_na(v) else str(v).strip() for v in out["id"].tolist()]

    # ---- 采购单价查表（款号颗粒度；无款号/表无此款 → 0）----
    key_series = out["款号"] if mode == MODE_STYLE else out["sku"]
    keys = key_series.astype("string").fillna("").str.strip()
    shop_names = (out["店铺名称"].astype("string").fillna("")
                  if "店铺名称" in out.columns else pd.Series("", index=out.index))
    brands = out["品牌"].astype("string").fillna("")

    purchase_fen: list[int] = []
    purchase_raw: list[Any] = []
    miss_reason: list[str] = []
    special: list[str] = []
    missing_styles: set[str] = set()
    for i, k in enumerate(keys.tolist()):
        is_fr = ("唯品会" in str(shop_names.iloc[i])) and (str(brands.iloc[i]) == "FR")
        if not k or k == PLACEHOLDER_STYLE:
            purchase_fen.append(0)
            purchase_raw.append(None)
            miss_reason.append(MISSING_NO_STYLE)
            special.append(SPECIAL_NO_STYLE)
            continue
        hit = cost_map.get(k)
        if hit is None:
            purchase_fen.append(0)
            purchase_raw.append(None)
            miss_reason.append(MISSING_NO_COST)
            special.append(SPECIAL_NO_COST)
            missing_styles.add(k)
        elif is_fr:
            # FR 行级特例：采购单价=0（基准实测），发货成本下面用标准成本
            purchase_fen.append(0)
            purchase_raw.append(None)
            miss_reason.append(MISSING_NONE)
            special.append(SPECIAL_NONE)
        else:
            purchase_fen.append(int(hit.get("unit_cost_fen") or 0))
            raw = hit.get("unit_cost_raw")
            purchase_raw.append(raw if raw not in (None, "") else None)
            miss_reason.append(MISSING_NONE)
            special.append(SPECIAL_NONE)

    out["采购单价"] = purchase_fen                      # 内部计算列（分）
    out[PURCHASE_RAW_COLUMN] = [                        # 展示列原始精度（A2）
        0.0 if r is None else float(r) for r in purchase_raw
    ]
    out["成本缺失原因"] = miss_reason                    # 内部诊断（仅底稿）
    out[SPECIAL_NOTE_COLUMN] = special                  # 交付第 32 列（Q6）

    # ---- 发货成本（C2.3：元域 Decimal 原始精度乘完再 round，HALF_UP）----
    std_list = out["标准成本"].tolist()
    ship: list[int] = []
    for i in range(len(qty)):
        is_fr = ("唯品会" in str(shop_names.iloc[i])) and (str(brands.iloc[i]) == "FR")
        if is_fr:
            ship.append(int(std_list[i]))               # FR：发货成本=标准成本（A6 行级）
        else:
            ship.append(_round_ship(purchase_raw[i], qty[i]))
    out["发货成本"] = ship

    return CostResult(
        frame=out,
        matched_rows=int(sum(1 for r in miss_reason if r == MISSING_NONE)),
        missing_no_style=int(sum(1 for r in miss_reason if r == MISSING_NO_STYLE)),
        missing_no_cost=int(sum(1 for r in miss_reason if r == MISSING_NO_COST)),
        total_std_cost_fen=int(sum(out["标准成本"].tolist())),
        total_ship_cost_fen=int(sum(ship)),
        detail={"mode": mode, "cost_keys": len(cost_map), "stage": "cost",
                "platform": platform},
        missing_styles=sorted(missing_styles),
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


# 注：_brand_code 已于 2026-11-18 总体 review 移除（死代码）：品牌映射现行口径在
# enrich_from_db 内直接 BRAND_DICT.get（缺失保 NaN，透视层 fillna("未知")），
# 与本函数「回落占位符」语义不同，勿复活。


def enrich_from_db(
    df: pd.DataFrame,
    product_df: pd.DataFrame,
    order_date_map: dict[str, str] | None = None,
    *,
    is_b2b: bool = False,
) -> pd.DataFrame:
    """用 ADB 商品主数据补款号/品类/年份/季节/品牌/吊牌价/标准成本单价，并回填订单日期。

    对应原 look_sku.result「匹配产品信息」段（B2C L1272-1341 / B2B L2179-2211）。
    缺失语义逐列不同（2026-11-17 影刀10月基准实测，sku 未命中字典时）：
      · 款号 / 吊牌价：原 astype(str).fillna(占位) 对 NaN 无效（已变 'nan' 串）→ **保持 NaN**
        （明细空白；透视层再 fillna("未知")）。
      · 商品名称 / 年份 / 季节：原 .fillna / .str 链保留 NaN → fillna 生效 →
        「无商品名称 / 无年份 / 无季节」字面量。
      · 品牌：原 .str.strip().fillna('无品牌') 后再 brand_dict.get('无品牌')→None → **NaN**
        （透视层 fillna("未知")）；字典外的真实品牌名同样 → NaN。
      · 品类：B2C 原脚本**不赋值**（保留 OMS join 值，未匹配为 NaN）；
        B2B 从字典取，未命中 → NaN。两者透视层都 fillna("未知")。
      · 标准成本单价：缺失 fillna(0)。
    透视层 fillna("未知") 由 pivots.build_match_pivot 就地执行（会同步改写明细，
    与原脚本一致——原脚本无 .copy()，明细 sheet 的品类/品牌因此也显示「未知」）。
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

    # 缺失语义（2026-11-17 影刀10月基准实测，对齐 look_sku L1272-1341）：
    #   · sku 在字典里查不到（dict.get → None）→ **保留 NaN**：明细 Excel 空白，
    #     透视分组键由 pivots 统一 fillna("未知")。旧实现 astype(str) 把 NaN 变 'nan'
    #     再 mask(=="") 填「无款号」，导致明细/透视占位全错（HYK 47 行、VIP 成批）。
    #   · sku 命中但字段本身是空串 → 才是「无款号/无季节…」字面量（原 fillna 只填 NaN，
    #     空串经 .str 链保持 NaN 的分支不存在；此处显式区分）。
    #   · 品牌：先 strip，再过 brand_dict；字典外 → NaN（原 .get(x) 无默认值），
    #     不写「无品牌」——「无品牌」只在 DB 字段为空串时出现。
    def _is_missing(v: Any) -> bool:
        if v is None:
            return True
        try:
            return bool(pd.isna(v))
        except (TypeError, ValueError):
            return False

    # 逐列缺失语义不同（见 docstring）。sku 未命中字典时：
    #   款号/吊牌价 → NaN；商品名称/年份/季节 → 占位字面量；品牌 → NaN（透视层填「未知」）。
    _KEEP_NA = {"款号", "吊牌价", "品牌"}
    for col, placeholder, strip_suffix in _ENRICH_TEXT_COLUMNS:
        if col not in out.columns:
            out[col] = None
        if sku_keys is None or col not in maps:
            raw = out[col].tolist()
        else:
            m = maps[col]
            raw = [m.get(k) for k in sku_keys.tolist()]
        vals: list[Any] = []
        for v in raw:
            if _is_missing(v):
                # 字典未命中：款号/吊牌价/品牌保持 NaN；其余填占位字面量
                vals.append(None if col in _KEEP_NA else placeholder)
                continue
            s = str(v).strip()
            if strip_suffix:
                s = s.replace("年" if col == "年份" else "季", "")
            if col == "品牌":
                # 原 brand_dict.get(x) 无默认值：字典外（含空串）→ None → NaN
                vals.append(BRAND_DICT.get(s))
            elif col == "吊牌价":
                vals.append(to_fen(s) if s else None)
            elif col == "款号":
                vals.append(s if s else None)   # 命中但空串 → NaN（astype(str) 语义）
            elif is_b2b and col in ("年份", "季节"):
                # B2B（L2190/L2207）：.str.strip().str.replace().fillna(占位) 对空串
                # 经 .str 链变 NaN 后 fillna 因 object dtype 失效 → 保持空白。
                # 2026-11-17 VIP 基准实测：86/99 行 DB 季节='' 导出为空白，非「无季节」。
                vals.append(s if s else None)
            else:
                vals.append(s if s else placeholder)
        out[col] = vals

    # 品类：B2C 原脚本不赋值（保留 OMS join 值，未匹配为 NaN，透视层填「未知」）；
    # B2B 从字典取（L2184/L2199），未命中 → NaN（透视层填「未知」）。
    if is_b2b and "品类" in maps and sku_keys is not None:
        m = maps["品类"]
        out["品类"] = [
            None if _is_missing(m.get(k)) else str(m.get(k)).strip()
            for k in sku_keys.tolist()
        ]

    # 标准成本单价：元 → 分。两阶段缺失语义不同（2026-11-17 影刀10月基准实测）：
    #   B2C（L1278）：.get(x).fillna(0.00) → 未命中填 0；
    #   B2B（L2187/L2204）：.get(x) **无 fillna** → 未命中保持 NaN（VIP 6万行空白）。
    #   下游标准成本计算各自兜底：B2C apply_cost safe_int(NaN)=0；
    #   B2B match_b2b_frame pd.to_numeric(...).fillna(0) → 金额合计不受影响。
    if sku_keys is not None and unit_price:
        if is_b2b:
            out["标准成本单价"] = [
                None if _is_missing(unit_price.get(k)) else to_fen(unit_price.get(k))
                for k in sku_keys.tolist()
            ]
        else:
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


# 注：cost_summary_by_advice（旧管理页成本卡片载荷）已于 2026-11-18 总体 review
# 移除（死代码）：能力页已整体下线，成本汇总走交付 Excel 透视与 sku_pivot_summary。
