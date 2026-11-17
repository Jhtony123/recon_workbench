"""【休眠模块 · 2026-11-14 方案改版后未接线】SKU对账 / 成本匹配 / 回传 / 全链路编排。

⚠️ 本文件**不在任何运行路径上**：run.py / actions.py / core/pipeline.py 都不 import 它。
保留它的理由：这里的匹配、分摊、成本、透视、回传逻辑是三层回归全绿的成果
（账单 30/30、匹配 8/8、成本 5/5、分摊差异恒 0 分），重新启用时不必重写。

⚠️ 直接运行会失败，因为它依赖已退役的库表（repo.T_BILL / T_GOODS / T_OMS /
T_MATCH / T_PIVOT_* / T_WRITEBACK）—— 这些表名已从 storage.ALLOWED_TABLES 移除，
写入会被白名单闸门拦下（这是有意的：防止绕过「数据库只存店铺映射」的方案约束）。

重新启用的四步：
  1) 先决定结果落盘形态（库表 or artifacts 文件）——方案约束要求优先文件；
  2) 若走库表，须在 storage.ALLOWED_TABLES 与 schema.sql 显式加回表名（= 一次有意的方案评审）；
     若走文件，把 repo.insert_rows(T_MATCH, …) 等改成 artifacts.write_parquet(…)；
  3) 把本文件的 run_match / run_writeback / run_full 搬回 core/pipeline.py；
  4) config.json 恢复对应 mcp_tools，UI 把占位卡换回真实表单。

以下是改版前的原始实现（原 pipeline.py 全文）。

----------------------------------------------------------------------
对账编排器：把三层串成一条可分阶段执行的流水线。

对应原影刀 main.py 的 791 行弹窗分流逻辑，这里改为显式 stage 参数：

  stage=bill       平台账单整理 → bill_rows（入库 + 导出账单整理 Excel）
  stage=match      bill + OMS 取数 → 匹配 / 分摊 / 成本 / 透视 / 回传表
  stage=writeback  已匹配单据回传 ADB 标记
  stage=full       bill → match → writeback（可选 export）

设计要点：
  · 每个 stage 都能独立重跑（按 batch_id / period+shop 清旧数据）
  · 失败一律抛业务异常（core.errors），由 run.py 转成 Job 终态，不再 exit()
  · OMS 取数失败或为空 → status=skipped（正常业务场景，新店首月/本期无发货）
  · 金额全程「分」的 int；只在导出 Excel 时转元
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import allocation, cost, db, exporters, matching, pivots, repo, writeback
from .config import BIZ_GOODS, SRC_RECEIPT, SRC_REFUND, UNMATCHED, period_label
from .errors import DataSourceEmpty, ReconError
from .money import fen_to_float, from_fen, sum_fen, to_fen
from .platforms import ParseContext, get_adapter
from .platforms.registry import parse_platform

ProgressFn = Callable[[int, str], None]


def _noop_progress(pct: int, msg: str) -> None:
    return None


@dataclass
class StageResult:
    stage: str
    status: str                    # success / partial / skipped / failed
    message: str = ""
    batch_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _bill_df_from_rows(rows, ctx: ParseContext) -> pd.DataFrame:
    """BillRow 列表 → 标准五元 DataFrame（金额已是分）。"""
    if not rows:
        return pd.DataFrame(columns=["订单编号", "资金日期", "业务类别", "收款", "退款"])
    return pd.DataFrame(
        [
            {
                "订单编号": r.order_no,
                "资金日期": r.fund_date,
                "业务类别": r.biz_category,
                "收款": int(r.receipt_fen or 0),
                "退款": int(r.refund_fen or 0),
                "__source_file__": r.source_file,
                "__source_row__": r.source_row,
            }
            for r in rows
        ]
    )


# ------------------------------------------------------------------ stage: bill


def run_bill(
    *,
    platform: str,
    shop_code: str,
    shop_name: str = "",
    period: str,
    s_date: str,
    e_date: str,
    file_paths: list[str],
    extra_files: dict[str, list[str]] | None = None,
    batch_id: str = "",
    export: bool = True,
    progress: ProgressFn = _noop_progress,
) -> StageResult:
    """阶段 1：平台账单整理（承接 bill_arr）。"""
    t0 = time.time()
    plat = parse_platform(platform)
    batch_id = batch_id or repo.new_batch_id("bill")
    progress(5, f"整理 {plat} 账单：{shop_code} / {period}")

    rules = repo.load_rules_map(plat)
    adapter = get_adapter(plat, rules)

    ctx = ParseContext(
        platform=plat,
        shop_code=shop_code,
        shop_name=shop_name or shop_code,
        period=period,
        s_date=s_date,
        e_date=e_date,
        batch_id=batch_id,
        file_paths=list(file_paths or []),
        extra_files=dict(extra_files or {}),
    )

    progress(15, "读取并清洗原始账单")
    rows = adapter.parse(ctx)
    if not rows:
        repo.save_job_log(
            batch_id, stage="bill", status="skipped", platform=plat,
            shop_code=shop_code, period=period,
            message="账期内没有账单数据", duration_sec=time.time() - t0,
        )
        return StageResult(
            stage="bill", status="skipped", batch_id=batch_id,
            message=f"{ctx.platform} 在 {period} 账期内没有账单数据",
            data={"bill_rows": 0},
        )

    progress(45, f"账单行入库（{len(rows)} 行）")
    bill_df = _bill_df_from_rows(rows, ctx)
    db_rows = [
        {
            "platform": plat,
            "shop_code": ctx.shop_code,
            "shop_name": ctx.shop_name,
            "period": period,
            "s_date": s_date,
            "e_date": e_date,
            "order_no": r.order_no,
            "fund_date": r.fund_date,
            "biz_category": r.biz_category,
            "receipt_fen": int(r.receipt_fen or 0),
            "refund_fen": int(r.refund_fen or 0),
            "net_fen": int(r.net_fen or 0),
            "source_file": r.source_file,
            "source_row": r.source_row,
            "batch_id": batch_id,
        }
        for r in rows
    ]
    written = repo.insert_rows(repo.T_BILL, db_rows)

    # 匹配层输入（货款）单独落库到 bill_goods。
    #
    # 为什么不复用 bill_rows 筛「业务类别=货款」：多源平台两者不等价。
    # 京东 JD001 实测（2026-11-14）——账单层是钱包流水(1097行/24类)+结算单(16604行)
    # 合并、且商家结算款已被结算单替换；而 SKU 对账的输入是另外三源子集：
    # 结算单货款 2295 行 + 钱包「货款退款」315 行（改判货款、订单号从备注正则提取）
    # + E卡 82 行（E卡根本不进账单层，只与钱包 E卡货款做交叉校验）。
    # 按账单层筛货款会错取「钱包货款」、漏掉 E卡与货款退款，货款金额与影刀基准
    # 276,668.76 / 185,311.32 元对不上。详见 platforms/base.goods_input_matches_bill。
    goods_rows = adapter.goods_input_rows(ctx, rows)
    # SKU 对账补录段的账期窗口：多源平台与账单账期不等价（京东用钱包备注资金日期
    # min~max）。必须在 parse/goods_input_rows 之后取 —— 京东靠解析时写入的
    # last_diag['inferred_period'] 才能给出推断值。
    match_s, match_e = adapter.match_window(ctx)
    repo.insert_rows(
        repo.T_GOODS,
        [
            {
                "platform": plat,
                "shop_code": ctx.shop_code,
                "shop_name": ctx.shop_name,
                "period": period,
                "s_date": s_date,
                "e_date": e_date,
                "order_no": r.order_no,
                "fund_date": r.fund_date,
                "receipt_fen": int(r.receipt_fen or 0),
                "refund_fen": int(r.refund_fen or 0),
                "net_fen": int(r.net_fen or 0),
                "source_file": r.source_file,
                "source_row": r.source_row,
                "match_s_date": match_s,
                "match_e_date": match_e,
                "batch_id": batch_id,
            }
            for r in goods_rows
        ],
    )
    goods_receipt_fen = sum_fen([r.receipt_fen for r in goods_rows])
    goods_refund_fen = sum_fen([r.refund_fen for r in goods_rows])

    exported = ""
    if export:
        progress(80, "导出账单整理结果 Excel")
        try:
            pivot = _bill_pivot(bill_df, ctx)
            out_dir = exporters.build_result_dir(period, shop_code, f"{plat}_账单整理")
            chuanyun = _bill_chuanyun(bill_df, ctx)
            path = exporters.export_bill_summary(
                out_dir,
                file_name=f"{plat}_{ctx.shop_name}_{shop_code}_{period}_账单整理.xlsx",
                bill_pivot=pivot,
                bill_detail=bill_df.drop(columns=["__source_file__", "__source_row__"], errors="ignore"),
                chuanyun=chuanyun,
            )
            exported = str(path)
        except Exception as exc:
            exported = f"导出失败：{type(exc).__name__}: {str(exc)[:120]}"

    progress(100, f"账单整理完成：{written} 行")
    duration = time.time() - t0
    repo.save_job_log(
        batch_id, stage="bill", status="success", platform=plat,
        shop_code=shop_code, period=period, bill_rows=written,
        message=f"{written} 行账单入库（货款 {len(goods_rows)} 行）",
        duration_sec=duration,
    )
    return StageResult(
        stage="bill", status="success", batch_id=batch_id,
        message=f"账单整理完成：{written} 行（其中货款 {len(goods_rows)} 行）",
        data={
            "bill_rows": written,
            "goods_rows": len(goods_rows),
            "goods_receipt_yuan": fen_to_float(goods_receipt_fen),
            "goods_refund_yuan": fen_to_float(goods_refund_fen),
            "goods_net_yuan": fen_to_float(goods_receipt_fen - goods_refund_fen),
            "exported": exported,
            "duration_sec": round(duration, 2),
        },
    )


def _bill_pivot(bill_df: pd.DataFrame, ctx: ParseContext) -> pd.DataFrame:
    """账单透视：按业务类别汇总收款/退款/现金流（原 *_账单透视 sheet）。"""
    if bill_df.empty:
        return pd.DataFrame()
    df = bill_df.copy()
    if "业务类别" not in df.columns:
        df["业务类别"] = BIZ_GOODS
    g = df.groupby("业务类别", as_index=False).agg(收款=("收款", "sum"), 退款=("退款", "sum"))
    g["现金流"] = g["收款"] - g["退款"]
    g["店铺名称"] = ctx.shop_name
    g["账单周期"] = ctx.period_cn
    g["店铺编码"] = ctx.shop_code
    for c in ("收款", "退款", "现金流"):
        g[c] = g[c].map(lambda v: float(from_fen(v)))
    return g.loc[:, ["店铺名称", "店铺编码", "账单周期", "业务类别", "收款", "退款", "现金流"]]


def _bill_chuanyun(bill_df: pd.DataFrame, ctx: ParseContext) -> pd.DataFrame:
    """氚云导入模板（账单层）：店铺名称/业务类别/资金方向/金额。"""
    if bill_df.empty:
        return pd.DataFrame(columns=list(pivots.CHUANYUN_COLUMNS))
    rows = []
    for col, label in (("收款", "收款"), ("退款", "付款")):
        if col not in bill_df.columns:
            continue
        g = bill_df.groupby("业务类别", as_index=False)[col].sum()
        g = g[g[col] > 0]
        if g.empty:
            continue
        g = g.rename(columns={col: "金额"})
        g["资金方向"] = label
        g["店铺名称"] = ctx.shop_name
        rows.append(g)
    if not rows:
        return pd.DataFrame(columns=list(pivots.CHUANYUN_COLUMNS))
    out = pd.concat(rows, ignore_index=True)
    out["金额"] = out["金额"].map(lambda v: float(from_fen(v)))
    for c in pivots.CHUANYUN_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    return out.loc[:, list(pivots.CHUANYUN_COLUMNS)]


# ------------------------------------------------------------------ stage: match


def run_match(
    *,
    platform: str,
    shop_code: str,
    shop_name: str = "",
    period: str,
    s_date: str,
    e_date: str,
    batch_id: str = "",
    bill_batch_id: str = "",
    lookback_months: int | None = None,
    cost_file: str = "",
    cost_mode: str = cost.MODE_SKU,
    cost_sheet: str | int = "",
    include_tax: bool = False,
    force_std_as_ship: bool = False,
    export: bool = True,
    use_db: bool = True,
    save_oms_snapshot: str = "0",
    oms_snapshot_limit: int = 5000,
    progress: ProgressFn = _noop_progress,
) -> StageResult:
    """阶段 2：SKU 出入库匹配 + 货款分摊 + 成本计算 + 透视。"""
    t0 = time.time()
    plat = parse_platform(platform)
    batch_id = batch_id or repo.new_batch_id("match")
    progress(5, f"匹配 {plat} / {shop_code} / {period}")

    # 1) 账单侧：优先用本次传入的 bill_batch_id，否则按 platform+shop+period 取最近一次
    bill_df = _load_bill_frame(plat, shop_code, period, bill_batch_id)
    if bill_df.empty:
        raise DataSourceEmpty(
            "没有可用的账单数据，请先执行「账单整理」",
            detail={"platform": plat, "shop_code": shop_code, "period": period},
        )

    # 1b) 匹配窗口：补录段筛「存货异动时间」用的账期，**可能不等于用户填的账期**。
    #
    # 原 main.py 京东分支就是这么分工的：oms_sql_select 收 dialog_result_jd.s_date/e_date
    # （用户账期 08-01~08-31），而 look_sku.result 收 jd_wallet_dict['开始日期'/'结束日期']
    # （钱包「备注资金日期」min~max，08 月实测为 07-31~08-30）。
    # 2026-11-14 JD001 实测：两处都用用户账期时，收款侧少 13 行、结算金额差 -874473 分，
    # 退款侧多 6 行、差 +87074 分 —— 差异行全是补录行（账单周期为空），
    # 基准独有行 id 2018~2021 开头、插件独有行 id 2029~2031 开头，正是边界日期翻转。
    # 单源平台该窗口 == 用户账期，行为不变。
    src_batch = _resolve_bill_batch(plat, shop_code, period, bill_batch_id)
    win_s, win_e = repo.query_goods_window(
        batch_id=src_batch, period=period, platform=plat, shop_code=shop_code
    )
    match_s_date = win_s or s_date
    match_e_date = win_e or e_date

    # 明细里的「账单周期」是业务字段，必须与生产库 bill_period / 基准导出同一中文格式
    # （'2026年08月-2026年08月'）；'YYYY-MM' 只作 SQLite 分区键与目录名。
    # 2026-08 实测缺陷：明细写 'YYYY-MM'，导致 _compare_periods 解析不出账期、
    # 导出与基准逐列不一致。
    period_cn = period_label(period, s_date=s_date, e_date=e_date) or period

    bill_split = matching.split_bill(bill_df, period=period_cn)
    receipt = bill_split.get("收款")
    refund = bill_split.get("退款")
    progress(20, f"账单货款：收款 {0 if receipt.empty else len(receipt)} 单 / 退款 {0 if refund.empty else len(refund)} 单")

    # 2) OMS 侧取数
    oms: dict[str, pd.DataFrame] = {"出库": pd.DataFrame(), "入库": pd.DataFrame(), "退款": pd.DataFrame()}
    oms_counts: dict[str, int] = {"出库": 0, "入库": 0, "退款": 0}
    if use_db:
        progress(30, "查询巨益出入库单")
        kwargs = {} if lookback_months is None else {"lookback_months": int(lookback_months)}
        oms = db.fetch_oms_rows(shop_code, s_date, e_date, **kwargs)
        oms_counts = {k: int(len(v)) for k, v in oms.items()}
        # OMS 是生产库镜像（单店半年可达 40 万行），默认不整表落本地：
        # 匹配在内存完成，只留行数摘要与「已匹配单据」明细（后者才是对账证据）。
        # 需要排查取数问题时，传 save_oms_snapshot=1 落一份有界样本。
        if str(save_oms_snapshot).lower() in ("1", "true", "yes"):
            oms_rows = _oms_to_db_rows(
                {k: v.head(int(oms_snapshot_limit)) for k, v in oms.items()},
                batch_id=batch_id, shop_code=shop_code, period=period,
            )
            if oms_rows:
                progress(35, f"落 OMS 快照样本 {len(oms_rows)} 行")
                repo.insert_rows(repo.T_OMS, oms_rows)

    total_oms = sum(len(v) for v in oms.values())
    if total_oms == 0:
        repo.save_job_log(
            batch_id, stage="match", status="skipped", platform=plat,
            shop_code=shop_code, period=period,
            message="账期内数据库无出入库单（新店首月或本期无发货属正常情况）",
            duration_sec=time.time() - t0,
        )
        return StageResult(
            stage="match", status="skipped", batch_id=batch_id,
            message="账期内数据库无出入库单，跳过匹配",
            data={"oms_rows": 0, "bill_rows": int(len(bill_df))},
        )

    # 3) 匹配（**不做终判**：终判读「货款分摊金额」，必须等第 4 步分摊完成后）
    #
    # s_date/e_date 传的是**匹配窗口**而非用户账期：原 main.py 里 oms_sql_select 收
    # 用户账期、look_sku.result 收 jd_wallet_dict 的开始/结束日期，本函数只承接后者
    # （第 2 步 OMS 取数已单独用 s_date/e_date 完成）。见 1b) 处注释。
    progress(50, "收款/退款与出入库单匹配")
    if (match_s_date, match_e_date) != (s_date, e_date):
        progress(50, f"匹配窗口按钱包账期 {match_s_date}~{match_e_date}")
    matched = matching.match_all(bill_split, oms, period=period_cn,
                                 s_date=match_s_date, e_date=match_e_date,
                                 shop_name=shop_name or shop_code, shop_code=shop_code,
                                 finalize=False)
    rec, ref = matched.receipt, matched.refund

    # 4) 分摊 + 自检（原脚本此处 exit()，现在抛异常）
    progress(62, "货款分摊与合计校验")
    detail = matched.detail
    if not detail.empty:
        alloc_in = detail.copy()
        # 货款金额：账单侧是分 int，补录行是 None（无账单金额）。不能 fillna(0)——
        # 原脚本靠 isna(货款金额) 区分「补录行」与「账单金额确为 0」，抹成 0 会让
        # 补录行走错分摊分支，并被终判误当成账单侧行处理。
        alloc_in["货款金额"] = alloc_in["货款金额"].map(
            lambda v: None if v is None or (isinstance(v, float) and v != v) else _to_int(v)
        )
        alloc_in["结算金额"] = pd.to_numeric(alloc_in.get("结算金额", 0), errors="coerce").fillna(0).astype("int64")
        alloc_res = allocation.allocate(alloc_in)
        alloc_res.verify_or_raise(f"{plat}/{shop_code}/{period}")
        detail = alloc_res.frame
        diff_fen = alloc_res.diff_fen
    else:
        diff_fen = 0

    # 4b) 处理意见终判（原脚本顺序：分摊 → 终判）
    #
    # 必须在分摊之后：finalize_advice 的 cond2（分摊>0 & 数量>0 → 结转成本）、
    # cond3（分摊≤0 → 成本不做处理）、fallback（分摊≤0 & 未对账 → 匹配状态回落）
    # 三条都读「货款分摊金额」。分摊前终判读到的是未分摊的货款金额，会把分摊为 0
    # 的行误判成「结转成本」。2026-08 DY023 实测：2 行错判，店铺透视结转成本
    # 多算 280.36 元 / 2 件，并连锁把退款侧 2 行「冲发出商品」翻成「冲成本」。
    if not detail.empty:
        detail = matching.finalize_detail(detail, period=period_cn)

    # 分摊 + 终判结果回灌到收/退款表，供成本、透视与回传使用
    if not detail.empty and "数据来源" in detail.columns:
        rec = detail[detail["数据来源"] == SRC_RECEIPT].copy()
        ref = detail[detail["数据来源"] == SRC_REFUND].copy()

    # 5) 成本
    progress(72, "匹配商品信息与成本")
    product_df = pd.DataFrame()
    order_date_map: dict[str, str] = {}
    if use_db and not detail.empty:
        skus = [s for s in detail.get("sku", pd.Series(dtype="object")).astype("string").fillna("").unique().tolist() if s]
        if skus:
            try:
                product_df = db.fetch_product_info(skus)
            except ReconError:
                product_df = pd.DataFrame()
        orders = [o for o in detail["订单编号"].astype("string").fillna("").unique().tolist() if o and o != "无订单号"]
        if orders:
            try:
                order_date_map = db.fetch_order_dates(orders)
            except ReconError:
                order_date_map = {}

    if not detail.empty:
        detail = cost.enrich_from_db(detail, product_df, order_date_map)
        cost_map: dict[str, dict[str, int]] = {}
        if cost_file:
            # 成本表按月一个 sheet（'8月末'），必须按账期选；不传会静默读到第一个
            # sheet（'1月末'），采购单价全线偏低（2026-11-13 实测踩坑）
            sheet = cost_sheet or cost.cost_sheet_for_period(period)
            cost_map = cost.load_cost_base(cost_file, mode=cost_mode,
                                           sheet_name=sheet, include_tax=include_tax)
        cost_res = cost.apply_cost(detail, cost_map, mode=cost_mode, force_std_as_ship=force_std_as_ship)
        detail = cost_res.frame
    else:
        cost_res = cost.CostResult(frame=detail)

    # 收/退款表同步成本列
    if not detail.empty:
        rec = detail[detail.get("数据来源", pd.Series(index=detail.index)).astype("string") == SRC_RECEIPT].copy()
        ref = detail[detail.get("数据来源", pd.Series(index=detail.index)).astype("string") == SRC_REFUND].copy()

    # 6) 透视
    progress(84, "生成店铺/款号透视")
    # dormant 是成本匹配阶段管线：显式 stage="cost"（pivot 默认已改 match 口径）
    p_shop = pivots.pivot_shop(detail, include_tax=include_tax, stage="cost")
    p_product = pivots.pivot_product(detail, include_tax=include_tax, stage="cost")
    p_match = pivots.pivot_match(detail)
    oms_matched = matching.build_oms_writeback(rec, ref)
    chuanyun = pivots.chuanyun_template(detail, period=period)

    # 7) 入库
    progress(90, "匹配结果入库")
    detail_rows = _detail_to_db_rows(detail, batch_id=batch_id, period=period, platform=plat,
                                     shop_code=shop_code, shop_name=shop_name or shop_code)
    repo.insert_rows(repo.T_MATCH, detail_rows)
    # shop_code 必传：透视行落库后按 (period, shop_code) 取最新批次，
    # 空 shop_code 会形成独立分组、把脏行永久带进管理页透视。
    repo.insert_rows(
        repo.T_PIVOT_SHOP,
        pivots.pivot_to_db_rows("shop", p_shop, batch_id=batch_id, period=period, platform=plat,
                               shop_code=shop_code),
    )
    repo.insert_rows(
        repo.T_PIVOT_PRODUCT,
        pivots.pivot_to_db_rows("product", p_product, batch_id=batch_id, period=period, platform=plat,
                               shop_code=shop_code),
    )

    # 8) 导出
    exported = ""
    if export:
        progress(95, "导出对账结果 Excel")
        try:
            out_dir = exporters.build_result_dir(period, shop_code, f"{plat}_匹配结果")
            path = exporters.export_match_result(
                out_dir,
                file_name=f"{plat}_{shop_name or shop_code}_{shop_code}_{period}_账单匹配结果.xlsx",
                pivot_shop=p_shop, pivot_product=p_product, pivot_match=p_match,
                receipt=rec, refund=ref, oms_matched=oms_matched, chuanyun=chuanyun,
            )
            exported = str(path)
        except Exception as exc:
            exported = f"导出失败：{type(exc).__name__}: {str(exc)[:120]}"

    # 终判会把部分行的匹配状态回落成「未匹配」（fallback 分支），
    # match_all(finalize=False) 里的 stats 已过时，必须按终判后的明细重算，
    # 否则 status 的 success/partial 判定与管理页的已匹配/未匹配数都是错的。
    stats = matching.recount(rec, ref, oms_matched)
    stats.update({
        k: v for k, v in matched.stats.items() if k.startswith("supplement_")
    })
    stats.update({
        "oms_rows": int(total_oms),
        "bill_rows": int(len(bill_df)),
        "diff_fen": int(diff_fen),
        "std_cost_yuan": fen_to_float(cost_res.total_std_cost_fen),
        "ship_cost_yuan": fen_to_float(cost_res.total_ship_cost_fen),
        "cost_missing_no_style": cost_res.missing_no_style,
        "cost_missing_no_cost": cost_res.missing_no_cost,
        "writeback_rows": int(len(oms_matched)),
        "exported": exported,
    })

    unmatched = int(stats.get("unmatched_rows") or 0)
    status = "success" if unmatched == 0 else "partial"
    duration = time.time() - t0
    repo.save_job_log(
        batch_id, stage="match", status=status, platform=plat,
        shop_code=shop_code, period=period,
        bill_rows=int(len(bill_df)),
        matched_rows=int(stats.get("matched_rows") or 0),
        unmatched_rows=unmatched,
        diff_fen=int(diff_fen),
        message=f"匹配 {stats.get('matched_rows', 0)} / 未匹配 {unmatched} / 分摊差异 {diff_fen} 分",
        duration_sec=duration,
    )
    progress(100, f"匹配完成：已匹配 {stats.get('matched_rows', 0)}，未匹配 {unmatched}")
    return StageResult(
        stage="match", status=status, batch_id=batch_id,
        message=(
            f"匹配完成：已匹配 {stats.get('matched_rows', 0)} 行，未匹配 {unmatched} 行"
            + (f"；秒退 {stats.get('instant_refund_rows', 0)} 行" if stats.get("instant_refund_rows") else "")
            + (f"；成本缺失 {cost_res.missing_no_style + cost_res.missing_no_cost} 行"
               if (cost_res.missing_no_style + cost_res.missing_no_cost) else "")
        ),
        data=stats,
    )


# OMS 中文列（db.fetch_oms_rows 产物）→ T_OMS 英文列（schema.sql）
_OMS_DB_COLUMNS: dict[str, str] = {
    "id": "oms_id",
    "单据类型": "doc_type",
    "存货异动时间": "stock_date",
    "单号": "doc_no",
    "订单编号": "order_no",
    "sku": "sku",
    "数量": "qty",
    "结算金额": "settle_fen",
    "订单日期": "order_date",
    "退款类型": "refund_type",
    "退款状态": "refund_status",
}
# 结算金额：ADB 回来是 Decimal 元 → 库存分
_OMS_MONEY_COLUMNS = ("结算金额",)
_OMS_INT_COLUMNS = ("数量",)


def _oms_to_db_rows(
    oms: dict[str, pd.DataFrame],
    *,
    batch_id: str,
    shop_code: str,
    period: str,
) -> list[dict]:
    """OMS 快照 → ext_recon_workbench_oms_rows 行。

    fetch_oms_rows 返回的是原脚本口径的中文列 DataFrame，
    而 T_OMS 表是英文列 + 金额以分存储，这里做显式映射，不靠位置。
    """
    out: list[dict] = []
    for kind, df in (oms or {}).items():
        if df is None or df.empty:
            continue
        for rec in df.to_dict(orient="records"):
            row: dict = {
                "batch_id": batch_id,
                "shop_code": str(rec.get("店铺编码") or shop_code or ""),
                "period": period,
            }
            for cn, en in _OMS_DB_COLUMNS.items():
                v = rec.get(cn)
                if cn in _OMS_MONEY_COLUMNS:
                    row[en] = to_fen(v)
                elif cn in _OMS_INT_COLUMNS:
                    try:
                        row[en] = int(float(v or 0))
                    except (TypeError, ValueError):
                        row[en] = 0
                else:
                    if v is None:
                        row[en] = ""
                    else:
                        s = str(v).strip()
                        row[en] = "" if s.lower() in ("nan", "none", "nat") else s
            # 单据类型为空时用中文分类兜底，便于排查
            if not row.get("doc_type"):
                row["doc_type"] = str(kind)
            out.append(row)
    return out


def _resolve_bill_batch(platform: str, shop_code: str, period: str, bill_batch_id: str) -> str:
    """本次匹配该用哪一批账单行。未显式指定时取该店铺该账期最新一次账单整理。"""
    batch = str(bill_batch_id or "").strip()
    if batch:
        return batch
    return repo.resolve_latest_batch(
        repo.T_BILL, period=period, platform=platform, shop_code=shop_code
    )


def _load_bill_frame(platform: str, shop_code: str, period: str, bill_batch_id: str) -> pd.DataFrame:
    """从 SQLite 取**匹配层输入行**（货款，金额分 int）。

    优先读 bill_goods（账单阶段由 adapter.goods_input_rows() 落库）；该表为空时
    回落到 bill_rows 筛「业务类别=货款」，兼容 2026-11-14 之前整理的旧批次。

    为什么要分表：多源平台的账单层与匹配层输入不等价。京东 JD001 实测——
    账单层是钱包流水(1097行/24类)+结算单(16604行)合并、商家结算款已被结算单替换；
    而 SKU 对账输入是 结算单货款2295行 + 钱包「货款退款」315行(改判货款、订单号从
    备注正则提取) + E卡82行(E卡不进账单层)。按 bill_rows 筛货款会错取钱包货款、
    漏掉 E卡与货款退款，货款金额与影刀基准 276,668.76 / 185,311.32 元对不上。

    未显式传 bill_batch_id 时，**只取该店铺该账期最新一次账单整理**的行。

    为什么必须收敛批次：bill_rows / bill_goods 的自然键都含 batch_id
    （见 repo.NATURAL_KEYS），同一店铺同一账期重跑「账单整理」会**新增**一批行，
    不会覆盖旧行。若按 period+platform+shop 全量取，split_bill 的
    groupby("订单编号").sum() 会把历次账单叠加，货款金额被放大 N 倍，
    分摊结果随之全错（而 OMS 侧每次从 ADB 现取、不累积，于是出现
    「数量与成本对、货款分摊翻倍」的诡异组合）。

    2026-11-13 浏览器实测：DY014 跑过 33 次回归后，管理页显示
    货款分摊 59,408.58 元（= 1,800.26 × 33），而数量 3、标准成本 707.72
    与基准一致。回归脚本因始终传显式 bill_batch_id 而未暴露此缺陷。
    """
    batch = _resolve_bill_batch(platform, shop_code, period, bill_batch_id)
    if not batch:
        return pd.DataFrame()

    rows = repo.query_goods_rows(batch_id=batch, limit=2_000_000)
    if shop_code:
        rows = [r for r in rows if r.get("shop_code") == shop_code]

    if rows:
        return pd.DataFrame(
            [
                {
                    "订单编号": r.get("order_no") or "",
                    "资金日期": r.get("fund_date") or "",
                    # bill_goods 恒为货款；split_bill 仍按此列筛，保持契约不变
                    "业务类别": BIZ_GOODS,
                    "收款": int(r.get("receipt_fen") or 0),
                    "退款": int(r.get("refund_fen") or 0),
                }
                for r in rows
            ]
        )

    # 回落：旧批次没有 bill_goods（该表 2026-11-14 才引入）
    rows = repo.query_bill_rows(batch_id=batch, limit=2_000_000)
    if shop_code:
        rows = [r for r in rows if r.get("shop_code") == shop_code]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "订单编号": r.get("order_no") or "",
                "资金日期": r.get("fund_date") or "",
                "业务类别": r.get("biz_category") or "",
                "收款": int(r.get("receipt_fen") or 0),
                "退款": int(r.get("refund_fen") or 0),
            }
            for r in rows
        ]
    )



_DETAIL_FIELD_MAP: dict[str, tuple[str, bool]] = {
    # 中文列 → (db 列, 是否金额分)
    "账单周期": ("period", False),
    "订单编号": ("order_no", False),
    "订单日期": ("order_date", False),
    "货款金额": ("gross_fen", True),
    "货款分摊金额": ("allocated_fen", True),
    "id": ("oms_id", False),
    "单据类型": ("doc_type", False),
    "存货异动时间": ("stock_date", False),
    "单号": ("doc_no", False),
    "sku": ("sku", False),
    "款号": ("style_no", False),
    "商品名称": ("product_name", False),
    "品类": ("category", False),
    "年份": ("year_tag", False),
    "季节": ("season", False),
    "品牌": ("brand", False),
    "吊牌价": ("tag_price_fen", True),
    "数量": ("qty", False),
    "店铺名称": ("shop_name", False),
    "结算金额": ("settle_fen", True),
    "退款类型": ("refund_type", False),
    "退款状态": ("refund_status", False),
    "标准成本单价": ("std_unit_cost_fen", True),
    "标准成本": ("std_cost_fen", True),
    "采购单价": ("purchase_unit_cost_fen", True),
    "发货成本": ("ship_cost_fen", True),
    "巨益单据匹配状态": ("match_state", False),
    "历史对账记录": ("history_recon", False),
    "对账周期": ("recon_period", False),
    "是否秒退判断": ("instant_refund", False),
    "处理意见": ("advice", False),
    "数据来源": ("data_source", False),
}


def _to_int(value: Any, default: int = 0) -> int:
    """安全转 int：None / NaN / NaT / 非数值串都回落到 default。

    注意不能用 `int(v or 0)`：float('nan') 是 truthy，`or` 不会兜底，
    int(nan) 直接抛 ValueError。真实库里 bill_amount / mall_type 就是 NULL。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        if value != value:  # NaN
            return default
        return int(value)
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_text(value: Any) -> str:
    """安全转 str：None / NaN / NaT → 空串（不产生 'nan' 字面量）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _detail_to_db_rows(detail: pd.DataFrame, *, batch_id: str, period: str,
                       platform: str, shop_code: str, shop_name: str) -> list[dict]:
    if detail is None or detail.empty:
        return []
    out: list[dict] = []
    for rec in detail.to_dict(orient="records"):
        row: dict[str, Any] = {
            "batch_id": batch_id,
            # SQLite 的 period 是**分区键**，必须保持 'YYYY-MM'；明细里的「账单周期」
            # 是业务字段（中文标签 '2026年08月-2026年08月'，与生产库 bill_period 同格式），
            # 只用于导出 Excel，不能拿来当分区键——否则 query_match_detail(period=…)
            # 与管理页按账期筛选全部查不到数据。
            "period": period or _to_text(rec.get("账单周期")) or "",
            # 业务账期单独存一列：补录行的「账单周期」是空串（原脚本口径，导出后为空白），
            # 这个语义必须在 DB 往返中保留，否则管理页/问AI 会把补录行显示成本期账单行。
            "bill_period_label": _to_text(rec.get("账单周期")),
            "platform": platform,
            "shop_code": shop_code,
        }
        for cn, (col, is_money) in _DETAIL_FIELD_MAP.items():
            if cn == "账单周期":
                continue
            v = rec.get(cn)
            if is_money:
                row[col] = _to_int(v)
            elif col == "qty":
                row[col] = _to_int(v)
            else:
                row[col] = _to_text(v)
        row.setdefault("shop_name", _to_text(rec.get("店铺名称")) or shop_name or "")
        row.setdefault("direction", SRC_RECEIPT if row.get("data_source") == SRC_RECEIPT else SRC_REFUND)
        row.setdefault("written_back", 0)
        out.append(row)
    return out


# ------------------------------------------------------------------ stage: writeback


def run_writeback(
    *,
    period: str,
    match_batch_id: str = "",
    platform: str = "",
    shop_code: str = "",
    s_date: str = "",
    e_date: str = "",
    oms_matched: pd.DataFrame | None = None,
    dry_run: bool = False,
    progress: ProgressFn = _noop_progress,
) -> StageResult:
    """阶段 3：结果回传数据库标记。"""
    t0 = time.time()
    batch_id = match_batch_id or repo.new_batch_id("wb")

    if oms_matched is None:
        oms_matched = _load_oms_matched(period=period, platform=platform,
                                       shop_code=shop_code, batch_id=match_batch_id)

    rows = writeback.prepare_rows(oms_matched, period=period)
    progress(20, f"待回传 {len(rows)} 条")

    try:
        res = writeback.writeback(rows, period=period, s_date=s_date, e_date=e_date,
                                  dry_run=dry_run, progress=progress)
    except ReconError as exc:
        repo.save_writeback_log(
            batch_id=batch_id, period=period, total_rows=len(rows), ok_rows=0,
            failed_rows=len(rows), status="failed", message=exc.message,
            duration_sec=time.time() - t0,
        )
        raise

    repo.save_writeback_log(
        batch_id=batch_id, period=period, total_rows=res.total, ok_rows=res.ok,
        failed_rows=res.failed, failed_detail=res.failed_detail,
        status=res.status, message=res.message, duration_sec=res.duration_sec,
    )
    if res.ok:
        repo._query(  # noqa: SLF001 - 同包内部使用
            f"UPDATE {repo.T_MATCH} SET written_back = 1 WHERE batch_id = ? AND match_state = '已匹配'",
            [batch_id],
        ) if match_batch_id else None

    progress(100, res.message)
    return StageResult(
        stage="writeback", status=res.status, batch_id=batch_id,
        message=res.message, data=res.to_dict(),
    )


def _load_oms_matched(*, period: str, platform: str = "", shop_code: str = "",
                      batch_id: str = "") -> pd.DataFrame:
    """从 SQLite 匹配明细里重建「已匹配的巨益单号」表。"""
    rows = repo.query_match_detail(
        period=period, shop_code=shop_code, batch_id=batch_id,
        match_state="已匹配", limit=1_000_000,
    )
    if not rows:
        return pd.DataFrame(columns=["id", "账单周期", "数量"])
    df = pd.DataFrame(
        [
            {
                "id": str(r.get("oms_id") or ""),
                "账单周期": str(r.get("period") or period or ""),
                "数量": int(r.get("qty") or 0),
            }
            for r in rows
        ]
    )
    df = df[df["id"] != ""].drop_duplicates(subset=["id"])
    return df


# ------------------------------------------------------------------ stage: full


def run_full(params: dict[str, Any], progress: ProgressFn = _noop_progress) -> dict[str, Any]:
    """完整流水线：bill → match →（可选）writeback。"""
    common = dict(
        platform=str(params.get("platform") or ""),
        shop_code=str(params.get("shop_code") or ""),
        shop_name=str(params.get("shop_name") or ""),
        period=str(params.get("period") or ""),
        s_date=str(params.get("s_date") or ""),
        e_date=str(params.get("e_date") or ""),
    )
    do_writeback = str(params.get("writeback", "0")) in ("1", "true", "True", "yes")
    dry_run = str(params.get("dry_run", "0")) in ("1", "true", "True", "yes")

    stages: list[dict[str, Any]] = []
    t0 = time.time()

    def sub(a: int, b: int) -> ProgressFn:
        def fn(pct: int, msg: str) -> None:
            progress(a + int(pct * (b - a) / 100), msg)
        return fn

    bill_res = run_bill(
        file_paths=_as_list(params.get("file_paths")),
        extra_files=_as_file_map(params.get("extra_files")),
        export=str(params.get("export", "1")) not in ("0", "false", "False"),
        progress=sub(0, 30),
        **common,
    )
    stages.append({"stage": bill_res.stage, "status": bill_res.status,
                   "message": bill_res.message, "batch_id": bill_res.batch_id,
                   "data": bill_res.data})

    if bill_res.status == "skipped":
        return {"ok": True, "status": "skipped", "message": bill_res.message,
                "stages": stages, "duration_sec": round(time.time() - t0, 2)}

    match_res = run_match(
        bill_batch_id=bill_res.batch_id,
        lookback_months=params.get("lookback_months"),
        cost_file=str(params.get("cost_file") or ""),
        cost_mode=str(params.get("cost_mode") or cost.MODE_SKU),
        cost_sheet=str(params.get("cost_sheet") or ""),
        include_tax=str(params.get("include_tax", "0")) in ("1", "true", "True"),
        force_std_as_ship=str(params.get("force_std_as_ship", "0")) in ("1", "true", "True"),
        export=str(params.get("export", "1")) not in ("0", "false", "False"),
        use_db=str(params.get("use_db", "1")) not in ("0", "false", "False"),
        save_oms_snapshot=str(params.get("save_oms_snapshot", "0")),
        oms_snapshot_limit=int(params.get("oms_snapshot_limit") or 5000),
        progress=sub(30, 85),
        **common,
    )
    stages.append({"stage": match_res.stage, "status": match_res.status,
                   "message": match_res.message, "batch_id": match_res.batch_id,
                   "data": match_res.data})

    if do_writeback and match_res.status in ("success", "partial"):
        wb_res = run_writeback(
            period=common["period"], match_batch_id=match_res.batch_id,
            s_date=common["s_date"], e_date=common["e_date"],
            dry_run=dry_run, progress=sub(85, 100),
        )
        stages.append({"stage": wb_res.stage, "status": wb_res.status,
                       "message": wb_res.message, "batch_id": wb_res.batch_id,
                       "data": wb_res.data})

    statuses = [s["status"] for s in stages]
    overall = "failed" if "failed" in statuses else ("partial" if "partial" in statuses else
                                                     ("skipped" if set(statuses) == {"skipped"} else "success"))
    total_bill = sum(int(s["data"].get("bill_rows") or 0) for s in stages if s["stage"] == "bill")
    return {
        "ok": overall != "failed",
        "status": overall,
        "message": " → ".join(f"{s['stage']}:{s['status']}" for s in stages),
        "stages": stages,
        "batch_id": match_res.batch_id or bill_res.batch_id,
        "row_count": total_bill,
        "duration_sec": round(time.time() - t0, 2),
    }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    s = str(value).strip()
    if not s:
        return []
    if s.startswith("["):
        import json
        try:
            return [str(v) for v in json.loads(s) if str(v).strip()]
        except Exception:
            pass
    return [p.strip().strip('"') for p in s.split(";") if p.strip()]


def _as_file_map(value: Any) -> dict[str, list[str]]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): _as_list(v) for k, v in value.items()}
    return {}
