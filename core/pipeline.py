"""对账编排器（2026-11-14 方案改版后：只有「账单整理」在运行）。

【方案改版】用户决策：
  1) 数据库不存 SKU 对账明细结果，也不存账单整理结果，只存店铺映射；
  2) 插件只做影刀现有能力 —— 账单整理（可用）+ SKU对账 / 成本匹配 / 数据库回传 / 氚云对接（占位暂缓）。

因此本文件只保留 stage=bill 的真实实现，其余 stage 一律返回**可读的占位终态**
（status="deferred"），不静默成功、也不抛栈。四个占位功能的完整算法没有删除，
整体封存在 core/pipeline_dormant.py（含 matching/allocation/cost/pivots/writeback/db 接线），
日后重新启用时：
  · 从 pipeline_dormant 取回 run_match / run_writeback / run_full 三段；
  · 在 storage.ALLOWED_TABLES 显式加表名，或改为落 artifacts 文件；
  · config.json 里恢复对应 mcp_tools，UI 去掉占位卡。
那批代码是三层回归全绿（账单 30/30、匹配 8/8、成本 5/5、分摊差异恒 0 分）的成果，不必重写。

账单整理产物去向（core/artifacts.py，2026-11-16 底稿分离）：
  results/{账期}_{平台}_账单整理_{店铺}_{时间戳}/
    ├── *_账单整理.xlsx         财务交付件（只有这些留在批次根目录）
    └── 底稿/                   过程底稿（用户不需要直接看）
        ├── manifest.json       批次元信息（行数、货款合计、产物路径）
        ├── *_统一明细.xlsx      插件五元口径附加簿
        ├── 京东E卡_….xlsx       交叉校验底稿（京东）
        ├── bill_detail.parquet|.csv   账单标准行（金额=分 int64）
        └── bill_goods.parquet|.csv    匹配层输入货款行

金额全程「分」的 int；只在导出 Excel 时转元。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import allocation, artifacts, b2b, cost, db, exporters, joblog, matching, pivots, repo, rules_store
from .config import BIZ_GOODS, DEFAULT_LOOKBACK_MONTHS, SRC_RECEIPT, SRC_REFUND, period_label
from .errors import DataSourceEmpty, ReconError
from .money import fen_to_float, from_fen, sum_fen
from .platforms import ParseContext, get_adapter
from .platforms.registry import parse_platform

ProgressFn = Callable[[int, str], None]

#: 真正可运行的阶段（2026-11-18 cost-match-stage spec：cost 启用，SKU 对账下游）
ACTIVE_STAGES = ("bill", "match", "cost")

#: 占位暂缓的阶段（含 UI 导航里的能力）
DEFERRED_STAGES = {
    "writeback": "数据库回传",
    "chuanyun": "氚云对接",
    "full": "全链路（账单+匹配+回传）",
}

#: 占位说明：为什么暂缓、启用条件、代码在哪
DEFERRED_NOTE = (
    "该能力按 2026-11-14 方案改版暂缓开放。完整实现保留在 core/pipeline_dormant.py"
    "（匹配/分摊/成本/透视/回传），重新启用时需先决定结果落盘形态"
    "（数据库只存店铺映射，不再存对账明细）。"
)


def _noop_progress(pct: int, msg: str) -> None:
    return None


@dataclass
class StageResult:
    stage: str
    status: str                    # success / partial / skipped / deferred / failed
    message: str = ""
    batch_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def deferred_result(stage: str) -> StageResult:
    """占位阶段的统一终态：deferred + 可读原因（不是失败，也不假装成功）。"""
    name = DEFERRED_STAGES.get(stage, stage)
    return StageResult(
        stage=stage,
        status="deferred",
        message=f"「{name}」暂未开放：{DEFERRED_NOTE}",
        data={
            "deferred": True,
            "capability": name,
            "stage": stage,
            "note": DEFERRED_NOTE,
            "dormant_module": "core/pipeline_dormant.py",
        },
    )


def capability_status() -> list[dict[str, Any]]:
    """五个能力的开放状态（管理页占位卡与总览用）。"""
    out = [
        {
            "key": "bill",
            "name": "账单整理",
            "status": "active",
            "stage": "bill",
            "note": "各平台账单 → 统一口径（订单编号/资金日期/业务类别/收款/退款），"
                    "导出 Excel + parquet；结果不入数据库。",
        },
        {
            "key": "match",
            "name": "SKU对账",
            "status": "active",
            "stage": "match",
            "note": "账单货款与巨益出入库单逐单匹配、货款分摊（不带成本表）。"
                    "2026-11-17 启用；产物落 sku匹配/未匹配成本/，不写生产库。",
        },
        {
            "key": "cost",
            "name": "成本匹配",
            "status": "active",
            "stage": "cost",
            "note": "财务物料成本表（不含税 SPU 单价）回填采购单价/发货成本，"
                    "重算透视毛利（收入/1.13−发货成本）。2026-11-18 启用；"
                    "成功后批次迁移 未匹配成本→已匹配成本。",
        },
    ]
    notes = {
        "writeback": "已匹配单据回写 ADB 标记「已对账」与对账周期。算法已封存待启用。",
        "chuanyun": "对账结果推送氚云。氚云导入模板 sheet 已随账单整理导出，推送通道未接。",
    }
    for key in ("writeback", "chuanyun"):
        out.append({
            "key": key,
            "name": DEFERRED_STAGES.get(key, key),
            "status": "deferred",
            "stage": key,
            "note": notes[key],
        })
    return out


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
    """阶段 1：平台账单整理（承接 bill_arr）。

    唯品会特例（2026-11-16 用户需求）：一个账期平台给多份按旬账单
    （目录/zip 名如 2026.08.01-2026.08.10）。给父目录或整批 zip 时自动
    展开成多旬，逐旬跑 _run_bill_single，交付件累积在同一批次目录
    （YYYY-MM-唯品会-唯品中国），底稿按旬子目录隔离。
    """
    plat = parse_platform(platform)
    if plat == "vip":
        xuns = _vip_expand_xuns(file_paths)
        if len(xuns) >= 1 and not (len(xuns) == 1 and s_date and e_date):
            return _vip_run_xuns(xuns, platform=platform, shop_code=shop_code,
                                 shop_name=shop_name, period=period,
                                 extra_files=extra_files, export=export,
                                 progress=progress)
    return _run_bill_single(
        platform=platform, shop_code=shop_code, shop_name=shop_name,
        period=period, s_date=s_date, e_date=e_date, file_paths=file_paths,
        extra_files=extra_files, batch_id=batch_id, export=export, progress=progress,
    )


_XUN_DIR_RE = __import__("re").compile(
    r"^(\d{4})\.(\d{1,2})\.(\d{1,2})-(\d{4})\.(\d{1,2})\.(\d{1,2})$")


def _xun_dates(name: str) -> tuple[str, str] | None:
    m = _XUN_DIR_RE.match(str(name or "").strip())
    if not m:
        return None
    y1, m1, d1, y2, m2, d2 = m.groups()
    return (f"{y1}-{int(m1):02d}-{int(d1):02d}", f"{y2}-{int(m2):02d}-{int(d2):02d}")


def _vip_expand_xuns(file_paths: list[str]) -> list[tuple[str, str, list[str]]]:
    """把唯品会输入展开为 [(s_date, e_date, [paths])]。

    支持：旬目录本身 / 含多个旬目录的父目录 / 旬名 zip / 含旬目录或旬名 zip 的父目录。
    识别不出旬结构时返回 []（回落用户给的 s/e 单跑）。
    """
    import os
    import re as _re

    from .platforms.base import BillAdapter

    out: list[tuple[str, str, list[str]]] = []
    for p in file_paths or []:
        p = str(p).strip().strip('"')
        if not p:
            continue
        if os.path.isfile(p) and p.lower().endswith(".zip"):
            stem = os.path.splitext(os.path.basename(p))[0]
            dates = _xun_dates(stem)
            root = BillAdapter._extract_zip(p)
            if dates:
                out.append((dates[0], dates[1], [root]))
            else:
                out.extend(_vip_expand_xuns([root]))
            continue
        if os.path.isdir(p):
            dates = _xun_dates(os.path.basename(p.rstrip("\\/")))
            if dates:
                out.append((dates[0], dates[1], [p]))
                continue
            subs = sorted(os.listdir(p))
            hit = False
            for name in subs:
                full = os.path.join(p, name)
                d = _xun_dates(name)
                if d and os.path.isdir(full):
                    out.append((d[0], d[1], [full]))
                    hit = True
                elif d and name.lower().endswith(".zip"):
                    root = BillAdapter._extract_zip(full)
                    out.append((d[0], d[1], [root]))
                    hit = True
            if not hit:
                # 父目录里可能是已解压但改了名的旬文件夹：递归一层找旬名
                for name in subs:
                    full = os.path.join(p, name)
                    if os.path.isdir(full):
                        out.extend(_vip_expand_xuns([full]))
    # 去重（同旬多路径合并）
    merged: dict[tuple[str, str], list[str]] = {}
    for s, e, paths in out:
        merged.setdefault((s, e), []).extend(paths)
    return [(s, e, paths) for (s, e), paths in sorted(merged.items())]


def _vip_run_xuns(xuns, *, platform, shop_code, shop_name, period,
                  extra_files, export, progress) -> StageResult:
    """逐旬跑唯品会并聚合终态（交付件同目录累积，底稿按旬隔离）。"""
    results: list[StageResult] = []
    n = len(xuns)
    for i, (s, e, paths) in enumerate(xuns):
        def prog(pct, msg, _i=i):
            progress(int((_i * 100 + pct) / n), f"[旬 {_i + 1}/{n} {s}~{e}] {msg}")
        results.append(_run_bill_single(
            platform=platform, shop_code=shop_code, shop_name=shop_name,
            period=period, s_date=s, e_date=e, file_paths=paths,
            extra_files=extra_files, export=export, progress=prog,
        ))
    ok = [r for r in results if r.status == "success"]
    bad = [r for r in results if r.status not in ("success", "skipped")]
    status = "success" if ok and not bad else ("failed" if bad else "skipped")
    exported: list[str] = []
    downloads: list[dict] = []
    rows = 0
    dirs: list[str] = []
    for r in results:
        exported.extend(r.data.get("exported_files") or [])
        downloads.extend(r.data.get("downloads") or [])
        rows += int(r.data.get("bill_rows") or 0)
        if r.data.get("batch_dir") and r.data["batch_dir"] not in dirs:
            dirs.append(r.data["batch_dir"])
    msg = "；".join(f"{r.data.get('bill_period_label') or ''}: {r.message}" for r in results)
    return StageResult(
        stage="bill", status=status, batch_id=results[0].batch_id if results else "",
        message=f"唯品会 {n} 旬整理{'完成' if status == 'success' else '存在失败'}：合计 {rows} 行。{msg}",
        data={"bill_rows": rows, "exported_files": exported, "downloads": downloads,
              "batch_dir": dirs[0] if dirs else "", "xun_count": n,
              "xun_results": [{"s": s, "e": e, "status": r.status, "message": r.message}
                              for (s, e, _), r in zip(xuns, results)]},
    )


def _run_bill_single(
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
    """单账期（唯品会为单旬）账单整理。不写数据库，结果落文件。"""
    t0 = time.time()
    plat = parse_platform(platform)
    batch_id = batch_id or repo.new_batch_id("bill")
    progress(5, f"整理 {plat} 账单：{shop_code} / {period}")

    # 京东日期窗口特例（2026-11-16 用户口径）：钱包/结算单有一天到账延迟，
    # 8 月账期实际取 7月31日~8月31日。只给 period 未给 s/e 时自动推「上月末~月末」；
    # 显式给了 s_date/e_date 则尊重用户（回归基准按显式日期跑）。
    if plat == "jd" and period and not s_date and not e_date:
        import calendar
        from datetime import date

        try:
            y, m = int(period[:4]), int(period[5:7])
            last = date(y, m, calendar.monthrange(y, m)[1])
            prev = date(y, m, 1) - __import__("datetime").timedelta(days=1)
            s_date, e_date = str(prev), str(last)
        except (ValueError, IndexError):
            pass

    rules = rules_store.load_rules_map(plat)
    adapter = get_adapter(plat, rules)

    # 店铺名称解析：基准 Excel 的「店铺名称」与文件名用**氚云名称**
    # （oms_shop.oms_shop：shopCode → 氚云名称，如 DY007 → 抖音_秋水伊人官方旗舰店）。
    # 未显式传 shop_name 时查店铺映射；映射缺失回落 shop_code（并在 notes 提示）。
    resolved_name = str(shop_name or "").strip()
    name_note = ""
    if not resolved_name and shop_code:
        try:
            for s in repo.query_shops(keyword=shop_code, limit=50):
                if str(s.get("shop_code")) == shop_code:
                    resolved_name = str(s.get("chuanyun_name") or s.get("shop_name") or "").strip()
                    break
        except Exception:
            resolved_name = ""
        if not resolved_name:
            name_note = ("店铺映射未导入或未命中：Excel 店铺名称暂用店铺编码。"
                         "请在「数据源与规则」页导入 OMS店铺.xlsx（含氚云名称列）后重跑。")
    resolved_name = resolved_name or shop_code

    ctx = ParseContext(
        platform=plat,
        shop_code=shop_code,
        shop_name=resolved_name,
        period=period,
        s_date=s_date,
        e_date=e_date,
        batch_id=batch_id,
        file_paths=list(file_paths or []),
        extra_files=dict(extra_files or {}),
    )

    progress(15, "读取并清洗原始账单")
    rows = adapter.parse(ctx)

    out_dir = artifacts.build_batch_dir(period, plat, shop_code,
                                        shop_name=ctx.shop_name,
                                        display_name=adapter.display_name)
    # 唯品会多旬共用批次目录：底稿按旬子目录隔离（底稿/2026年0801-2026年0810/），
    # 交付 Excel 仍在目录根按旬文件名累积。
    drafts_subdir = ""
    if plat == "vip" and s_date and e_date:
        drafts_subdir = (f"{artifacts.DRAFTS_DIRNAME}/"
                         f"{s_date[:4]}年{s_date[5:7]}{s_date[8:10]}-"
                         f"{e_date[:4]}年{e_date[5:7]}{e_date[8:10]}")
    batch = artifacts.BillBatch(
        batch_id=batch_id,
        dir=out_dir,
        platform=plat,
        shop_code=shop_code,
        shop_name=ctx.shop_name,
        period=period,
        s_date=s_date,
        e_date=e_date,
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        drafts_subdir=drafts_subdir,
    )

    if not rows:
        # 部分平台（小红书）原脚本账期内无数据也导出全零透视簿（XHS002 8月基准实证）。
        # export_when_empty=True 的适配器走导出分支，其余保持 skipped。
        if getattr(adapter, "export_when_empty", False) and export:
            try:
                import json as _json
                import shutil

                safe = lambda s: "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|')
                bill_type = getattr(adapter, "bill_type", "") or adapter.display_name or plat
                exported_files: list[str] = []
                for book in adapter.export_books(ctx):
                    fname = book.get("file_name") or (
                        f"{safe(book.get('prefix') or bill_type)}{book.get('infix', '')}_{safe(ctx.shop_name)}_"
                        f"{safe(shop_code)}_{safe(ctx.period_cn)}{book.get('tail', '_账单整理')}.xlsx")
                    path = exporters.export_native_book(out_dir, file_name=fname,
                                                        sheets=book.get("sheets") or [])
                    exported_files.append(str(path))
                download_files: list[dict] = []
                from .config import runtime_dir
                dl_dir = runtime_dir() / "downloads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                for p in exported_files:
                    src = Path(p)
                    dl_name = f"{stamp}_{src.name}"
                    try:
                        shutil.copy2(src, dl_dir / dl_name)
                        download_files.append({
                            "name": src.name,
                            "url": f"/agent/extensions/recon_workbench/files/downloads/{dl_name}",
                        })
                    except OSError:
                        pass
                batch.status = "success"
                batch.files = {"excel": exported_files[0] if exported_files else "",
                               "excel_list": _json.dumps(exported_files, ensure_ascii=False),
                               "downloads": _json.dumps(download_files, ensure_ascii=False)}
                batch.notes.append("账期内没有账单数据：按原脚本口径导出全零透视簿")
                artifacts.write_manifest(batch)
                joblog.save_job_log(
                    batch_id, stage="bill", status="success", platform=plat,
                    shop_code=shop_code, period=period, bill_rows=0,
                    message="账期内无数据，已导出全零透视簿", duration_sec=time.time() - t0,
                )
                return StageResult(
                    stage="bill", status="success", batch_id=batch_id,
                    message=f"{plat} 在 {period} 账期内没有账单数据（已按基准导出全零透视簿）",
                    data={"bill_rows": 0, "goods_rows": 0, "batch_dir": str(out_dir),
                          "exported_files": exported_files, "downloads": download_files},
                )
            except Exception as exc:
                batch.notes.append(f"空账期导出失败：{type(exc).__name__}: {str(exc)[:120]}")
        batch.status = "skipped"
        batch.files = {}
        artifacts.write_manifest(batch)
        joblog.save_job_log(
            batch_id, stage="bill", status="skipped", platform=plat,
            shop_code=shop_code, period=period,
            message="账期内没有账单数据", duration_sec=time.time() - t0,
        )
        return StageResult(
            stage="bill", status="skipped", batch_id=batch_id,
            message=f"{ctx.platform} 在 {period} 账期内没有账单数据",
            data={"bill_rows": 0, "batch_dir": str(out_dir)},
        )

    progress(45, f"整理 {len(rows)} 行账单（结果落文件，不入库）")
    bill_df = _bill_df_from_rows(rows, ctx)

    detail_records = [
        {
            "batch_id": batch_id,
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
        }
        for r in rows
    ]
    detail_df = pd.DataFrame(detail_records)

    # 匹配层输入（货款）单独落一份 parquet。
    #
    # 为什么不复用账单明细筛「业务类别=货款」：多源平台两者不等价。
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

    goods_records = [
        {
            "batch_id": batch_id,
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
            "match_s_date": match_s,
            "match_e_date": match_e,
            "source_file": r.source_file,
            "source_row": r.source_row,
        }
        for r in goods_rows
    ]
    goods_df = pd.DataFrame(goods_records)

    progress(65, "写入账单明细 parquet")
    _, detail_n = artifacts.write_parquet(
        batch.detail_path, detail_df, artifacts.DETAIL_COLUMNS,
        int_cols=("receipt_fen", "refund_fen", "net_fen", "source_row"),
    )
    _, goods_n = artifacts.write_parquet(
        batch.goods_path, goods_df, artifacts.GOODS_COLUMNS,
        int_cols=("receipt_fen", "refund_fen", "net_fen", "source_row"),
    )

    goods_receipt_fen = sum_fen([r.receipt_fen for r in goods_rows])
    goods_refund_fen = sum_fen([r.refund_fen for r in goods_rows])
    bill_receipt_fen = sum_fen([r.receipt_fen for r in rows])
    bill_refund_fen = sum_fen([r.refund_fen for r in rows])

    batch.detail_rows = detail_n
    batch.goods_rows = goods_n
    batch.totals = {
        "bill_receipt_fen": bill_receipt_fen,
        "bill_refund_fen": bill_refund_fen,
        "bill_net_fen": bill_receipt_fen - bill_refund_fen,
        "goods_receipt_fen": goods_receipt_fen,
        "goods_refund_fen": goods_refund_fen,
        "goods_net_fen": goods_receipt_fen - goods_refund_fen,
        "biz_categories": int(bill_df["业务类别"].nunique()) if not bill_df.empty else 0,
        "match_s_date": match_s,
        "match_e_date": match_e,
        "bill_period_label": period_label(period, s_date=s_date, e_date=e_date),
    }

    exported = ""
    exported_files: list[str] = []
    download_files: list[dict] = []
    if name_note:
        batch.notes.append(name_note)
    if export:
        progress(80, "导出账单整理结果 Excel（影刀基准格式）")
        try:
            import json as _json
            import shutil

            bill_type = getattr(adapter, "bill_type", "") or adapter.display_name or plat
            safe = lambda s: "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|')
            # 批次根目录只放财务交付 Excel；过程底稿（统一明细/E卡/manifest/表格）
            # 一律进 底稿/ 子目录（唯品会多旬时按旬再分一层，见 batch.drafts_subdir）
            drafts_dir = batch.drafts_dir
            draft_files: list[str] = []
            books = adapter.export_books(ctx)
            if books:
                for book in books:
                    prefix = book.get("prefix") or bill_type
                    # file_name 特例：爱库存/唯品会等多店一簿平台，文件名不含店铺（原脚本口径）
                    fname = book.get("file_name") or (
                        f"{safe(prefix)}{book.get('infix', '')}_{safe(ctx.shop_name)}_"
                        f"{safe(shop_code)}_{safe(ctx.period_cn)}{book.get('tail', '_账单整理')}.xlsx")
                    target = drafts_dir if book.get("draft") else out_dir
                    path = exporters.export_native_book(
                        target, file_name=fname, sheets=book.get("sheets") or [])
                    (draft_files if book.get("draft") else exported_files).append(str(path))
            else:
                # 未实现基准格式的平台（GenericAdapter 等）→ 回落统一三 sheet
                pivot = _bill_pivot(bill_df, ctx)
                chuanyun = _bill_chuanyun(bill_df, ctx)
                excel_name = f"{plat}_{ctx.shop_name}_{shop_code}_{period}_账单整理.xlsx"
                path = exporters.export_bill_summary(
                    out_dir, file_name=excel_name, bill_pivot=pivot,
                    bill_detail=bill_df.drop(
                        columns=["__source_file__", "__source_row__"], errors="ignore"),
                    chuanyun=chuanyun)
                exported_files.append(str(path))

            # 统一五元明细附加簿：插件自身口径（分→元），属过程底稿 → 底稿/
            uni_name = (f"{safe(bill_type)}_{safe(ctx.shop_name)}_{safe(shop_code)}_"
                        f"{safe(ctx.period_cn)}_统一明细.xlsx")
            uni_path = exporters.export_native_book(
                drafts_dir, file_name=uni_name,
                sheets=[{"name": "统一账单明细", "kind": "fen",
                         "df": bill_df.drop(columns=["__source_file__", "__source_row__"],
                                            errors="ignore")}])
            draft_files.append(str(uni_path))

            # 复制到 downloads/（宿主 /files/downloads/ 路由只认该目录），供 UI 一键下载。
            # 文件名带批次时间戳，避免不同批次同名互相覆盖。
            from .config import runtime_dir
            dl_dir = runtime_dir() / "downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            for p in exported_files:
                src = Path(p)
                dl_name = f"{stamp}_{src.name}"
                try:
                    shutil.copy2(src, dl_dir / dl_name)
                    download_files.append({
                        "name": src.name,
                        "url": f"/agent/extensions/recon_workbench/files/downloads/{dl_name}",
                    })
                except OSError:
                    pass  # 下载副本失败不影响批次主产物

            exported = exported_files[0] if exported_files else (draft_files[0] if draft_files else "")
            batch.files["excel"] = exported
            batch.files["excel_list"] = _json.dumps(exported_files, ensure_ascii=False)
            batch.files["draft_files"] = _json.dumps(draft_files, ensure_ascii=False)
            batch.files["downloads"] = _json.dumps(download_files, ensure_ascii=False)
        except Exception as exc:
            exported = f"导出失败：{type(exc).__name__}: {str(exc)[:120]}"
            batch.notes.append(exported)

    batch.files["detail_parquet"] = str(batch.detail_path)
    batch.files["goods_parquet"] = str(batch.goods_path)
    batch.status = "success"
    artifacts.write_manifest(batch)

    progress(100, f"账单整理完成：{detail_n} 行")
    duration = time.time() - t0
    joblog.save_job_log(
        batch_id, stage="bill", status="success", platform=plat,
        shop_code=shop_code, period=period, bill_rows=detail_n,
        message=f"{detail_n} 行账单整理完成（货款 {goods_n} 行），结果已落文件未入库",
        duration_sec=duration,
    )
    return StageResult(
        stage="bill", status="success", batch_id=batch_id,
        message=f"账单整理完成：{detail_n} 行（其中货款 {goods_n} 行）",
        data={
            "bill_rows": detail_n,
            "goods_rows": goods_n,
            "goods_receipt_yuan": fen_to_float(goods_receipt_fen),
            "goods_refund_yuan": fen_to_float(goods_refund_fen),
            "goods_net_yuan": fen_to_float(goods_receipt_fen - goods_refund_fen),
            "bill_receipt_yuan": fen_to_float(bill_receipt_fen),
            "bill_refund_yuan": fen_to_float(bill_refund_fen),
            "bill_period_label": batch.totals["bill_period_label"],
            "match_window": [match_s, match_e],
            "exported": exported,
            "exported_files": exported_files,
            "downloads": download_files,
            "batch_dir": str(out_dir),
            "detail_parquet": str(batch.detail_path),
            "goods_parquet": str(batch.goods_path),
            "stored_in_db": False,
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
    """氚云导入模板（账单层）：店铺名称/业务类别/资金方向/金额。

    注意：这里只产出**模板 sheet**（财务手工导入氚云用），氚云推送通道本身属占位暂缓能力。
    """
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


# ------------------------------------------------------------------ 占位阶段


def _match_to_int(value: Any, default: int = 0) -> int:
    """安全转 int（None/NaN → default）；不能用 int(v or 0)，NaN 是 truthy。"""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _match_period_cn(plat: str, period: str, s_date: str, e_date: str) -> str:
    """业务账期中文标签。唯品会带日（按旬结算，check_dates L47 特例）。"""
    if plat == "vip" and len(s_date) >= 10 and len(e_date) >= 10:
        return (f"{s_date[:4]}年{s_date[5:7]}{s_date[8:10]}-"
                f"{e_date[:4]}年{e_date[5:7]}{e_date[8:10]}")
    return period_label(period, s_date=s_date, e_date=e_date) or period


def _vip_label_from_name(name: str) -> str:
    """唯品会交付文件名 → 带日账期标签（唯品会_{label}唯品中国_账单整理.xlsx）。"""
    s = str(name or "")
    if s.startswith("唯品会_") and "唯品中国" in s:
        return s[len("唯品会_"):s.index("唯品中国")]
    return ""


def _export_match_book(out_dir: Path, *, file_name: str, res: dict[str, Any],
                       detail: pd.DataFrame, batch_id: str, period: str,
                       plat: str, shop_code: str, drafts_subdir: str = "") -> dict[str, Any]:
    """落一份 SKU 对账产物：6-sheet Excel（批次根）+ parquet/csv 底稿 + manifest。"""
    import json as _json
    import shutil

    drafts = out_dir / (drafts_subdir or artifacts.DRAFTS_DIRNAME)
    exported_files: list[str] = []
    download_files: list[dict] = []
    path = exporters.export_match_result(
        out_dir, file_name=file_name,
        pivot_shop=res["pivot_shop"], pivot_product=res["pivot_product"],
        pivot_match=res["pivot_match"], receipt=res["receipt"], refund=res["refund"],
        oms_matched=res["oms_matched"],
    )
    exported_files.append(str(path))

    # 底稿：明细与三张透视、回传表（金额=分），供回归与排查
    draft_files: list[str] = []
    for stem, df in (
        (artifacts.MATCH_DETAIL_STEM, detail),
        (artifacts.MATCH_PIVOT_SHOP_STEM, res["pivot_shop"]),
        (artifacts.MATCH_PIVOT_PRODUCT_STEM, res["pivot_product"]),
        (artifacts.MATCH_PIVOT_MATCH_STEM, res["pivot_match"]),
        (artifacts.MATCH_WRITEBACK_STEM, res["oms_matched"]),
    ):
        p, _n = artifacts.write_table(drafts, stem, df, list(df.columns))
        draft_files.append(str(p))

    # 下载副本（宿主 downloads 路由）
    from .config import runtime_dir
    dl_dir = runtime_dir() / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for p in exported_files:
        src = Path(p)
        dl_name = f"{stamp}_{src.name}"
        try:
            shutil.copy2(src, dl_dir / dl_name)
            download_files.append({
                "name": src.name,
                "url": f"/agent/extensions/recon_workbench/files/downloads/{dl_name}",
            })
        except OSError:
            pass

    batch = artifacts.BillBatch(
        batch_id=batch_id, dir=out_dir, platform=plat, shop_code=shop_code,
        period=period, created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        drafts_subdir=drafts_subdir, status="success",
    )
    batch.totals = {k: v for k, v in res["stats"].items() if isinstance(v, (int, float, bool))}
    batch.files = {
        "excel": exported_files[0],
        "excel_list": _json.dumps(exported_files, ensure_ascii=False),
        "draft_files": _json.dumps(draft_files, ensure_ascii=False),
        "downloads": _json.dumps(download_files, ensure_ascii=False),
    }
    batch.notes.append("SKU对账产物（不带成本表阶段）；金额底稿为分，Excel 为元。")
    artifacts.write_manifest(batch)
    return {"exported": exported_files[0], "exported_files": exported_files,
            "downloads": download_files, "batch_dir": str(out_dir)}


def run_match(
    *,
    platform: str,
    shop_code: str = "",
    shop_name: str = "",
    period: str = "",
    s_date: str = "",
    e_date: str = "",
    bill_batch_id: str = "",
    lookback_months: int | None = None,
    export: bool = True,
    progress: ProgressFn = _noop_progress,
) -> StageResult:
    """阶段 2：SKU 对账（不带成本表）。2026-11-17 sku-match-revival spec 启用。

    输入 = 账单整理批次（bill_goods / B2B 明细 sheet）；OMS 只读取数（向前 6 个月）；
    结果落 {period}/sku匹配/未匹配成本/{批次名}/，不写任何库表、不写生产库。
    顺序纪律（dormant 实测教训）：match_all(finalize=False) → 分摊对平门 →
    finalize_detail 终判 → 产品回填 → 标准成本 → 透视 → recount。
    """
    t0 = time.time()
    plat = parse_platform(platform)
    batch_id = repo.new_batch_id("match")
    progress(5, f"SKU对账 {plat} / {shop_code or '(按批次)'} / {period}")

    src = artifacts.resolve_batch(batch_id=bill_batch_id, period=period,
                                  platform=plat, shop_code=shop_code)
    if src is None:
        raise DataSourceEmpty(
            "没有可用的账单整理批次，请先执行「账单整理」",
            detail={"platform": plat, "shop_code": shop_code, "period": period,
                    "bill_batch_id": bill_batch_id},
        )
    period = period or src.period
    shop_code = shop_code or src.shop_code
    shop_name = shop_name or src.shop_name or shop_code
    s_date = s_date or src.s_date
    e_date = e_date or src.e_date

    rules = rules_store.load_rules_map(plat)
    adapter = get_adapter(plat, rules)
    display = adapter.display_name or plat
    safe = lambda v: "".join(ch for ch in str(v or "") if ch not in '\\/:*?"<>|')
    out_dir = artifacts.build_match_batch_dir(period, plat, shop_code,
                                              shop_name=shop_name, display_name=display)

    # ---------------- B2B 分流（爱库存/唯品会：不查 OMS，逐行直接给意见）
    if getattr(adapter, "is_b2b", False):
        progress(20, "B2B 渠道：读账单明细（不查 OMS）")
        frames = b2b.load_b2b_inputs(src.dir, plat)
        # 单旬冒烟/回归：显式 bill_batch_id 时收敛到该旬。唯品会一个批次目录累积
        # 三旬文件（默认全跑），每旬底稿子目录各有 manifest（batch_id + files.excel）；
        # 按 batch_id 找到对应旬的交付文件名，只跑那一份。
        if bill_batch_id and len(frames) > 1:
            want = ""
            for mp in sorted((src.dir / artifacts.DRAFTS_DIRNAME).glob(f"*/{artifacts.MANIFEST_NAME}")):
                man = artifacts.read_manifest(mp)
                if str(man.get("batch_id") or "") == bill_batch_id:
                    want = str((man.get("files") or {}).get("excel") or "")
                    break
            if want:
                picked = [(p, d) for p, d in frames if p.name == Path(want).name]
                if picked:
                    frames = picked
        all_stats: dict[str, int] = {}
        exported_files: list[str] = []
        downloads: list[dict] = []
        n_xun = 0
        for xls_path, df in frames:
            n_xun += 1
            if plat == "vip":
                label = _vip_label_from_name(xls_path.name) or _match_period_cn(plat, period, s_date, e_date)
                sub = f"{artifacts.DRAFTS_DIRNAME}/{label}" if label else ""
                fname = f"唯品会_{safe(label)}唯品中国_账单匹配结果.xlsx"
            else:
                label = _match_period_cn(plat, period, s_date, e_date)
                sub = ""
                fname = f"爱库存_{safe(label)}_账单匹配结果.xlsx"
            progress(40, f"B2B 匹配 {xls_path.name}")
            res = b2b.match_b2b_frame(df, platform=plat, period_cn=label)
            if export:
                info = _export_match_book(
                    out_dir, file_name=fname, res=res, detail=res["detail"],
                    batch_id=f"{batch_id}-{n_xun}" if len(frames) > 1 else batch_id,
                    period=period, plat=plat, shop_code=shop_code, drafts_subdir=sub)
                exported_files.extend(info["exported_files"])
                downloads.extend(info["downloads"])
            for k, v in res["stats"].items():
                if isinstance(v, int):
                    all_stats[k] = all_stats.get(k, 0) + v
        duration = time.time() - t0
        joblog.save_job_log(
            batch_id, stage="match", status="success", platform=plat,
            shop_code=shop_code, period=period,
            bill_rows=all_stats.get("receipt_rows", 0) + all_stats.get("refund_rows", 0),
            message=f"B2B 对账完成（{n_xun} 份）：收款 {all_stats.get('receipt_rows', 0)} 行 / "
                    f"退款 {all_stats.get('refund_rows', 0)} 行；不回传数据库",
            duration_sec=duration,
        )
        progress(100, "B2B 对账完成")
        return StageResult(
            stage="match", status="success", batch_id=batch_id,
            message=f"B2B 对账完成（{n_xun} 份）：收款 {all_stats.get('receipt_rows', 0)} 行 / "
                    f"退款 {all_stats.get('refund_rows', 0)} 行",
            data={**all_stats, "exported": exported_files[0] if exported_files else "",
                  "exported_files": exported_files, "downloads": downloads,
                  "batch_dir": str(out_dir), "stored_in_db": False,
                  "duration_sec": round(duration, 2)},
        )

    # ---------------- B2C 主链
    goods = artifacts.read_goods(src)
    if goods.empty:
        raise DataSourceEmpty(
            "账单整理批次里没有货款行（bill_goods 为空）",
            detail={"batch_id": src.batch_id, "goods_path": str(src.goods_path)},
        )
    bill_df = pd.DataFrame({
        "订单编号": goods["order_no"].astype("string").fillna(""),
        "资金日期": goods["fund_date"].astype("string").fillna(""),
        "业务类别": BIZ_GOODS,
        "收款": pd.to_numeric(goods["receipt_fen"], errors="coerce").fillna(0).astype("int64"),
        "退款": pd.to_numeric(goods["refund_fen"], errors="coerce").fillna(0).astype("int64"),
    })
    # 匹配窗口：京东用钱包备注资金日期 min~max ≠ 用户账期（JD001 实测教训）
    win_s, win_e = artifacts.match_window(batch_id=src.batch_id)
    match_s, match_e = win_s or s_date, win_e or e_date
    period_cn = _match_period_cn(plat, period, s_date, e_date)
    bill_split = matching.split_bill(bill_df, period=period_cn)
    progress(20, f"账单货款：收款 {len(bill_split.get('收款', []))} 单 / "
                 f"退款 {len(bill_split.get('退款', []))} 单")

    progress(30, "查询巨益出入库单（只读）")
    lb = int(lookback_months) if lookback_months else DEFAULT_LOOKBACK_MONTHS
    oms = db.fetch_oms_rows(shop_code, s_date, e_date, lookback_months=lb)
    total_oms = sum(len(v) for v in oms.values())
    if total_oms == 0:
        joblog.save_job_log(
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

    progress(50, "收款/退款与出入库单匹配")
    matched = matching.match_all(bill_split, oms, period=period_cn,
                                 s_date=match_s, e_date=match_e,
                                 shop_name=shop_name, shop_code=shop_code,
                                 finalize=False)
    detail = matched.detail
    diff_fen = 0
    if not detail.empty:
        progress(62, "货款分摊与合计校验")
        alloc_in = detail.copy()
        # 补录行货款金额为 None（无账单金额），不能 fillna(0)——分摊靠 isna 区分。
        # 空串同样归 None：秒退重匹配未命中 OMS 退款单的行货款被 _blank_join 置 ""，
        # 基准里这批行货款金额是空白（2026-11-17 HYK 实测 11 行）。
        alloc_in["货款金额"] = alloc_in["货款金额"].map(
            lambda v: None if v is None or (isinstance(v, float) and v != v)
            or (isinstance(v, str) and not v.strip()) else _match_to_int(v))
        alloc_in["结算金额"] = pd.to_numeric(alloc_in.get("结算金额", 0),
                                             errors="coerce").fillna(0).astype("int64")
        alloc_res = allocation.allocate(alloc_in)
        alloc_res.verify_or_raise(f"{plat}/{shop_code}/{period}")
        detail = alloc_res.frame
        diff_fen = alloc_res.diff_fen
        # 终判必须在分摊之后（cond2/cond3/fallback 都读分摊金额）
        detail = matching.finalize_detail(detail, period=period_cn)

    progress(72, "匹配商品信息与标准成本")
    product_df = pd.DataFrame()
    order_date_map: dict[str, str] = {}
    if not detail.empty:
        skus = [s for s in detail["sku"].astype("string").fillna("").unique().tolist() if s]
        if skus:
            try:
                product_df = db.fetch_product_info(skus)
            except ReconError:
                product_df = pd.DataFrame()
        orders = [o for o in detail["订单编号"].astype("string").fillna("").unique().tolist()
                  if o and o != "无订单号"]
        if orders:
            try:
                order_date_map = db.fetch_order_dates(orders)
            except ReconError:
                order_date_map = {}
        detail = cost.enrich_from_db(detail, product_df, order_date_map, is_b2b=False)
        # 不带成本表阶段：cost_map 空 → 采购单价/发货成本 0；标准成本=数量×DB单价
        cost_res = cost.apply_cost(detail, {}, mode=cost.MODE_STYLE)
        detail = cost_res.frame
    else:
        cost_res = cost.CostResult(frame=detail)

    progress(84, "生成店铺/款号透视")
    # B2C：原脚本透视层 fillna("未知") 就地改写明细（无 .copy()），故 mutate_detail=True，
    # 明细 sheet 的品类/品牌也显示「未知」；透视先算，再切收/退款侧导出。
    p_shop = pivots.pivot_shop(detail, mutate_detail=True)
    p_product = pivots.pivot_product(detail, mutate_detail=True)
    p_match = pivots.pivot_match(detail)
    src_col = detail.get("数据来源", pd.Series(index=detail.index, dtype="object")).astype("string")
    rec = detail[src_col == SRC_RECEIPT].copy() if not detail.empty else detail
    ref = detail[src_col == SRC_REFUND].copy() if not detail.empty else detail
    res = {
        "receipt": rec, "refund": ref,
        "pivot_shop": p_shop,
        "pivot_product": p_product,
        "pivot_match": p_match,
        "oms_matched": matching.build_oms_writeback(rec, ref, period=period_cn),
    }
    stats = matching.recount(rec, ref, res["oms_matched"])
    stats.update({k: v for k, v in matched.stats.items() if k.startswith("supplement_")})
    res["stats"] = stats   # _export_match_book 写 manifest.totals 用

    exported_files: list[str] = []
    downloads: list[dict] = []
    exported = ""
    if export:
        progress(92, "导出对账结果 Excel")
        fname = f"{safe(display)}_{safe(shop_name)}_{safe(shop_code)}_{safe(period_cn)}_账单匹配结果.xlsx"
        info = _export_match_book(out_dir, file_name=fname, res=res, detail=detail,
                                  batch_id=batch_id, period=period, plat=plat,
                                  shop_code=shop_code)
        exported = info["exported"]
        exported_files = info["exported_files"]
        downloads = info["downloads"]

    unmatched = int(stats.get("unmatched_rows") or 0)
    status = "success" if unmatched == 0 else "partial"
    duration = time.time() - t0
    stats.update({
        "oms_rows": int(total_oms), "bill_rows": int(len(bill_df)),
        "diff_fen": int(diff_fen),
        "std_cost_yuan": fen_to_float(cost_res.total_std_cost_fen),
        "writeback_rows": int(len(res["oms_matched"])),
        "exported": exported, "exported_files": exported_files, "downloads": downloads,
        "batch_dir": str(out_dir), "stored_in_db": False,
        "duration_sec": round(duration, 2),
    })
    joblog.save_job_log(
        batch_id, stage="match", status=status, platform=plat,
        shop_code=shop_code, period=period, bill_rows=int(len(bill_df)),
        matched_rows=int(stats.get("matched_rows") or 0), unmatched_rows=unmatched,
        diff_fen=int(diff_fen),
        message=f"匹配 {stats.get('matched_rows', 0)} / 未匹配 {unmatched} / 分摊差异 {diff_fen} 分",
        duration_sec=duration,
    )
    progress(100, f"匹配完成：已匹配 {stats.get('matched_rows', 0)}，未匹配 {unmatched}")
    return StageResult(
        stage="match", status=status, batch_id=batch_id,
        message=(f"匹配完成：已匹配 {stats.get('matched_rows', 0)} 行，未匹配 {unmatched} 行"
                 + (f"；秒退 {stats.get('instant_refund_rows', 0)} 行"
                    if stats.get("instant_refund_rows") else "")),
        data=stats,
    )


#: cost 阶段守恒校验列（spec A8：成本回填不得改动这些列，逐列合计恒等）
COST_CONSERVE_COLUMNS = ("货款金额", "货款分摊金额", "结算金额", "数量", "标准成本")


def _cost_conserve_check(before: pd.DataFrame, after: pd.DataFrame, label: str) -> None:
    """回填前后逐列合计守恒（A8）。货款金额空值按 0 计（补录行语义）。"""
    for c in COST_CONSERVE_COLUMNS:
        if c not in before.columns or c not in after.columns:
            continue
        b = pd.to_numeric(before[c], errors="coerce").fillna(0).sum()
        a = pd.to_numeric(after[c], errors="coerce").fillna(0).sum()
        if int(round(float(b))) != int(round(float(a))):
            raise ReconError(
                f"成本回填守恒校验失败：{label} 列「{c}」合计变化 {b} → {a}",
                detail={"column": c, "before": float(b), "after": float(a)},
            )


def _default_cost_file(period: str) -> Path:
    """成本表缺省查找（spec C5）：结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx。"""
    ym = artifacts.period_month(period)
    return artifacts.output_dir() / ym / f"物料成本_{ym.replace('-', '')}.xlsx"


def _verify_book_sheets(path: Path) -> None:
    """迁移/落盘前校验：6 sheet 完整（spec C4，临时名导出后校验再转正）。"""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True)
    names = set(wb.sheetnames)
    wb.close()
    need = {exporters.SHEET_SHOP, exporters.SHEET_PRODUCT, exporters.SHEET_MATCH,
            exporters.SHEET_RECEIPT, exporters.SHEET_REFUND, exporters.SHEET_OMS}
    missing = need - names
    if missing:
        raise ReconError(f"导出校验失败：缺少 sheet {sorted(missing)}",
                         detail={"file": str(path), "sheets": sorted(names)})


def _run_cost_unit(unit: dict[str, Any], *, batch_dir: Path, cost_map: dict,
                   cost_file: str, sheet: str, period: str, plat: str,
                   shop_code: str, include_tax: bool, batch_id: str,
                   progress: ProgressFn) -> dict[str, Any]:
    """一个处理单元（VIP 单旬 / 普通整批 / AKC 渠道批次）的成本匹配。

    读 match 底稿 → cost 阶段回填 → 守恒自检 → 重新透视（A3）→
    临时名导出 → 6-sheet 校验 → 转正 → cost 底稿 + manifest 留痕。
    失败抛异常，由调用方决定留守粒度（VIP 全旬成功才迁移）。
    """
    import json as _json

    drafts = Path(unit["drafts_dir"])
    detail = artifacts.read_match_draft(drafts)
    if detail.empty:
        raise DataSourceEmpty(f"底稿 match_detail 为空：{drafts}", detail={"dir": str(drafts)})
    label = unit.get("label") or ""
    tag = f"{plat}/{shop_code or batch_dir.name}/{label or period}"

    progress(40, f"成本回填 {tag}（{len(detail)} 行）")
    cost_res = cost.apply_cost_stage(detail, cost_map, platform=plat, mode=cost.MODE_STYLE)
    filled = cost_res.frame
    _cost_conserve_check(detail, filled, tag)

    progress(60, f"重新透视 {tag}")
    # A3：必须从占位符重写后的明细重新透视（分组键已变）；mutate_detail=False
    # （cost 阶段占位符已重写，无需再 fillna 写回）。
    p_shop = pivots.pivot_shop(filled, include_tax=include_tax, stage="cost")
    p_product = pivots.pivot_product(filled, include_tax=include_tax, stage="cost")
    p_match = pivots.pivot_match(filled, stage="cost")
    src_col = filled.get("数据来源", pd.Series(index=filled.index, dtype="object")).astype("string")
    rec = filled[src_col == SRC_RECEIPT].copy()
    ref = filled[src_col == SRC_REFUND].copy()
    oms = artifacts._read_table(artifacts.resolve_table_path(drafts, artifacts.MATCH_WRITEBACK_STEM))

    # 交付文件名沿用 match 产物名（影刀同名写入 成本匹配/ 的口径；状态机迁移目录）
    man = unit.get("manifest") or {}
    src_excel = str((man.get("files") or {}).get("excel") or "")
    fname = Path(src_excel).name if src_excel else f"{batch_dir.name}_账单匹配结果.xlsx"

    progress(80, f"导出 {tag}")
    tmp = batch_dir / (fname + ".cost-tmp.xlsx")
    final = batch_dir / fname
    try:
        exporters.export_cost_result(
            batch_dir, file_name=tmp.name,
            pivot_shop=p_shop, pivot_product=p_product, pivot_match=p_match,
            receipt=rec, refund=ref, oms_matched=oms, platform=plat)
        _verify_book_sheets(tmp)
        tmp.replace(final)          # 同目录原子转正；失败时原交付件留守
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    # 下载副本（宿主 downloads 路由，与 match 阶段同口径，供 UI 下载）
    import shutil as _shutil
    from .config import runtime_dir
    dl_dir = runtime_dir() / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    downloads: list[dict] = []
    try:
        dl_name = f"{stamp}_{final.name}"
        _shutil.copy2(final, dl_dir / dl_name)
        downloads.append({"name": final.name,
                          "url": f"/agent/extensions/recon_workbench/files/downloads/{dl_name}"})
    except OSError:
        pass

    # cost 底稿（分域）+ manifest 留痕（cost_file/sheet/缺失款号/税率）
    for stem, df in (("cost_detail", filled), ("cost_pivot_shop", p_shop),
                     ("cost_pivot_product", p_product), ("cost_pivot_match", p_match)):
        artifacts.write_table(drafts, stem, df, list(df.columns))
    missing = list(cost_res.missing_styles or [])
    if missing:
        artifacts.write_table(drafts, "cost_missing_styles",
                              pd.DataFrame({"款号": missing}), ["款号"])
    new_man = dict(man)
    new_man.update({
        "batch_id": str(man.get("batch_id") or batch_id),
        "stage": "cost",
        "status": "partial" if (cost_res.missing_no_cost or cost_res.missing_no_style) else "success",
        "cost_file": str(cost_file), "cost_sheet": str(sheet),
        "include_tax": bool(include_tax),
        "missing_no_style": int(cost_res.missing_no_style),
        "missing_no_cost": int(cost_res.missing_no_cost),
        "missing_styles": missing,
        "cost_created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    files = dict(new_man.get("files") or {})
    files["excel"] = str(final)
    files["downloads"] = _json.dumps(downloads, ensure_ascii=False)
    new_man["files"] = files
    mp = drafts / artifacts.MANIFEST_NAME
    tmp_mp = mp.with_suffix(".json.tmp")
    tmp_mp.write_text(_json.dumps(new_man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_mp.replace(mp)

    return {
        "label": label, "rows": int(len(filled)),
        "receipt_rows": int(len(rec)), "refund_rows": int(len(ref)),
        "missing_no_style": int(cost_res.missing_no_style),
        "missing_no_cost": int(cost_res.missing_no_cost),
        "missing_styles": missing,
        "std_cost_fen": int(cost_res.total_std_cost_fen),
        "ship_cost_fen": int(cost_res.total_ship_cost_fen),
        "exported": str(final),
    }


def run_cost(
    *,
    period: str = "",
    cost_file: str = "",
    bill_batch_id: str = "",
    platform: str = "",
    shop_code: str = "",
    include_tax: bool = False,
    progress: ProgressFn = _noop_progress,
) -> StageResult:
    """阶段 3：实际成本匹配（2026-11-18 cost-match-stage spec 启用，文件态）。

    输入 = sku匹配/未匹配成本/{批次} 的 match 底稿（分域全列，C2.0）+
    财务成本表（物料成本_YYYYMM.xlsx，{月}月末 sheet，规格型号=SPU 不含税单价）。
    输出 = 同目录同名 6-sheet 交付件（cost 列集 13/18/14 + 明细 32）+ cost 底稿；
    全部单元成功后整批迁移 未匹配成本→已匹配成本（VIP 全旬成功才迁移，A6）。
    已匹配成本/ 内重跑：原地覆盖不迁移。不写任何库表。
    """
    t0 = time.time()
    period = str(period or "").strip()
    if not period:
        raise ReconError("成本匹配必须指定账期 period（YYYY-MM）")
    batch_id = repo.new_batch_id("cost")
    plat = parse_platform(platform) if platform else ""
    progress(5, f"成本匹配 {period}")

    # ---- 成本表（缺省查找 + 用户指定双通道；异形表转换层留接口，Q3）----
    cf = Path(str(cost_file or "").strip()) if str(cost_file or "").strip() else _default_cost_file(period)
    if not cf.is_file():
        raise DataSourceEmpty(
            f"成本表不存在：{cf}",
            detail={"hint": "缺省查找 结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx；"
                            "或在参数 cost_file 指定路径。财务异形表请先转换成固定格式（Q3）。",
                    "period": period},
        )
    sheet = cost.cost_sheet_for_period(period)
    try:
        cost_map = cost.load_cost_base(str(cf), mode=cost.MODE_STYLE, sheet_name=sheet,
                                       include_tax=include_tax)
    except ReconError as exc:
        # C5：成本表缺失/sheet 不存在 → failed 可读终态 + joblog 留痕（不抛栈）
        msg = f"成本匹配失败：{exc}"
        joblog.save_job_log(batch_id, stage="cost", status="failed",
                            platform=plat or "all", shop_code=shop_code, period=period,
                            message=msg[:2000], duration_sec=time.time() - t0)
        return StageResult(stage="cost", status="failed", batch_id=batch_id, message=msg,
                           data={"period": period, "cost_file": str(cf), "cost_sheet": str(sheet)})
    if not cost_map:
        msg = f"成本匹配失败：成本表 {cf.name} 的 sheet「{sheet}」无有效款号单价行"
        joblog.save_job_log(batch_id, stage="cost", status="failed",
                            platform=plat or "all", shop_code=shop_code, period=period,
                            message=msg, duration_sec=time.time() - t0)
        return StageResult(stage="cost", status="failed", batch_id=batch_id, message=msg,
                           data={"period": period, "cost_file": str(cf), "cost_sheet": str(sheet)})
    progress(15, f"成本表就绪：{cf.name} / {sheet} / {len(cost_map)} 款")

    # ---- 批次发现（未匹配成本 + 已匹配成本重跑场景）----
    batches = artifacts.list_sku_batches(
        period, states=(artifacts.SKU_STATE_PENDING_COST, artifacts.SKU_STATE_COSTED),
        platform=plat, shop_code=shop_code)
    if bill_batch_id:
        target = str(bill_batch_id).strip()
        batches = [b for b in batches
                   if target in (b.batch_id, b.dir.name)
                   or any(str((u.get("manifest") or {}).get("batch_id") or "") == target
                          for u in artifacts.sku_batch_units(b.dir))]
    if not batches:
        raise DataSourceEmpty(
            "没有可做成本匹配的 SKU 对账批次，请先执行「SKU对账」",
            detail={"period": period, "platform": plat, "shop_code": shop_code,
                    "bill_batch_id": bill_batch_id},
        )

    exported_files: list[str] = []
    unit_reports: list[dict] = []
    failed_batches: list[dict] = []
    migrated: list[str] = []
    all_missing: set[str] = set()
    n_ok = n_partial = 0

    for b in batches:
        bplat = b.platform or plat
        units = artifacts.sku_batch_units(b.dir)
        if not units:
            failed_batches.append({"batch": b.dir.name, "reason": "无 match 底稿（半截批次？）"})
            continue
        in_costed = b.dir.parent.name == artifacts.SKU_STATE_COSTED
        unit_results: list[dict] = []
        unit_errors: list[dict] = []
        for u in units:
            try:
                r = _run_cost_unit(
                    u, batch_dir=b.dir, cost_map=cost_map, cost_file=str(cf),
                    sheet=str(sheet), period=period, plat=bplat,
                    shop_code=b.shop_code, include_tax=include_tax,
                    batch_id=batch_id, progress=progress)
                unit_results.append(r)
                exported_files.append(r["exported"])
                all_missing.update(r["missing_styles"])
            except Exception as exc:
                unit_errors.append({"label": u.get("label") or b.dir.name,
                                    "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        if unit_errors:
            # VIP 单旬失败 → 整目录留守（A6）；其余批次粒度同理。
            # 成功单元仍计入 unit_reports（终态 partial 而非 failed——有真实产出）。
            unit_reports.extend(unit_results)
            failed_batches.append({"batch": b.dir.name, "units_failed": unit_errors,
                                   "units_ok": [r["label"] or "(整批)" for r in unit_results]})
            continue
        # 全部单元成功 → 迁移（已匹配成本内重跑原地覆盖不迁移）
        if not in_costed:
            try:
                dst = artifacts.migrate_sku_batch(b.dir)
                migrated.append(str(dst))
                exported_files = [str(dst / Path(f).name) if Path(f).parent == b.dir else f
                                  for f in exported_files]
            except OSError as exc:
                # 交付件已生成（原地），只是迁移失败 → partial，成功单元计入
                unit_reports.extend(unit_results)
                failed_batches.append({"batch": b.dir.name, "reason": f"迁移失败：{exc}"})
                continue
        unit_reports.extend(unit_results)
        if any(r["missing_no_cost"] or r["missing_no_style"] for r in unit_results):
            n_partial += 1
        else:
            n_ok += 1

    duration = time.time() - t0
    total_rows = sum(r["rows"] for r in unit_reports)
    ship_fen = sum(r["ship_cost_fen"] for r in unit_reports)
    if not unit_reports:
        status = "failed"
        msg = "成本匹配失败：" + "；".join(
            f"{f['batch']}({f.get('reason') or f.get('units_failed')})" for f in failed_batches)[:500]
    elif failed_batches:
        status = "partial"
        msg = (f"成本匹配部分完成：{len(unit_reports)} 单元成功 / {len(failed_batches)} 批次留守；"
               f"缺失款号 {len(all_missing)} 个")
    elif all_missing:
        status = "partial"
        msg = f"成本匹配完成（{len(unit_reports)} 单元），{len(all_missing)} 个款号成本表无此款（填0+特殊情况说明）"
    else:
        status = "success"
        msg = f"成本匹配完成：{len(unit_reports)} 单元 / {total_rows} 行，发货成本 {fen_to_float(ship_fen)} 元"

    joblog.save_job_log(
        batch_id, stage="cost", status=status, platform=plat or "all",
        shop_code=shop_code, period=period, bill_rows=total_rows,
        message=msg + (f"；迁移 {len(migrated)} 批" if migrated else ""),
        duration_sec=duration,
    )
    progress(100, msg)
    return StageResult(
        stage="cost", status=status, batch_id=batch_id, message=msg,
        data={
            "period": period, "cost_file": str(cf), "cost_sheet": str(sheet),
            "include_tax": bool(include_tax),
            "units_ok": len(unit_reports), "rows": total_rows,
            "ship_cost_yuan": fen_to_float(ship_fen),
            "missing_styles": sorted(all_missing),
            "exported": exported_files[0] if exported_files else "",
            "exported_files": exported_files,
            "migrated": migrated, "failed_batches": failed_batches,
            "unit_reports": unit_reports,
            "stored_in_db": False, "duration_sec": round(duration, 2),
        },
    )


def run_writeback(*_args: Any, **_kwargs: Any) -> StageResult:
    """数据库回传 —— 占位暂缓。实现在 core/pipeline_dormant.run_writeback。"""
    return deferred_result("writeback")


def run_chuanyun(*_args: Any, **_kwargs: Any) -> StageResult:
    """氚云对接 —— 占位暂缓（账单整理已导出氚云导入模板 sheet）。"""
    return deferred_result("chuanyun")


def run_full(params: dict[str, Any], progress: ProgressFn = _noop_progress) -> dict[str, Any]:
    """全链路 —— 占位暂缓。

    只跑账单整理这一段真实可用的，其余段以 deferred 列出，让调用方一眼看到边界，
    而不是拿到一个「全链路成功」的假终态。
    """
    prog = progress or _noop_progress
    bill_stage = str(params.get("stage") or "bill").strip().lower()
    if bill_stage != "full":
        return deferred_result(bill_stage).__dict__ | {"ok": False}

    prog(5, "全链路：账单整理可执行，匹配/成本/回传暂缓")
    res = run_bill(
        platform=str(params.get("platform") or ""),
        shop_code=str(params.get("shop_code") or ""),
        shop_name=str(params.get("shop_name") or ""),
        period=str(params.get("period") or ""),
        s_date=str(params.get("s_date") or ""),
        e_date=str(params.get("e_date") or ""),
        file_paths=_as_list(params.get("file_paths")),
        extra_files=_as_file_map(params.get("extra_files")),
        batch_id=str(params.get("batch_id") or ""),
        export=str(params.get("export", "1")) not in ("0", "false", "False"),
        progress=prog,
    )
    deferred = [DEFERRED_STAGES[k] for k in ("writeback", "chuanyun")]
    data = dict(res.data or {})
    data["deferred_stages"] = deferred
    data["note"] = DEFERRED_NOTE
    return {
        "ok": res.status != "failed",
        "status": res.status,
        "message": f"{res.message}；后续 {len(deferred)} 个环节暂缓开放（{'、'.join(deferred)}）",
        "row_count": int(data.get("bill_rows") or 0),
        "stage": "full",
        "batch_id": res.batch_id,
        "data": data,
    }


# ------------------------------------------------------------------ 参数解析


def _as_list(value: Any) -> list[str]:
    """'a;b' / ['a','b'] / JSON 数组 → list[str]（去空、去首尾空白）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    s = str(value).strip()
    if not s:
        return []
    if s.startswith("["):
        import json

        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v or "").strip()]
        except ValueError:
            pass
    for sep in (";", "；", "\n", "|"):
        if sep in s:
            return [p.strip() for p in s.split(sep) if p.strip()]
    return [s]


def _as_file_map(value: Any) -> dict[str, list[str]]:
    """附加数据源：JSON 字符串或 dict → {kind: [paths]}。"""
    if not value:
        return {}
    if isinstance(value, dict):
        raw: Any = value
    else:
        import json

        s = str(value).strip()
        if not s:
            return {}
        try:
            raw = json.loads(s)
        except ValueError as exc:
            raise ReconError(
                "附加数据源不是合法 JSON",
                detail={"value": s[:200], "error": str(exc),
                        "hint": '形如 {"wechat":"D:\\\\微信账单","ecard":"D:\\\\京东E卡"}'},
            ) from exc
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): _as_list(v) for k, v in raw.items() if str(k or "").strip()}


__all__ = [
    "ACTIVE_STAGES",
    "DEFERRED_NOTE",
    "DEFERRED_STAGES",
    "StageResult",
    "capability_status",
    "deferred_result",
    "run_bill",
    "run_chuanyun",
    "run_cost",
    "run_full",
    "run_match",
    "run_writeback",
    "artifacts",
    "exporters",
    "joblog",
    "repo",
    "rules_store",
]
