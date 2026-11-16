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

from . import artifacts, exporters, joblog, pivots, repo, rules_store
from .config import BIZ_GOODS, period_label
from .errors import ReconError
from .money import fen_to_float, from_fen, sum_fen
from .platforms import ParseContext, get_adapter
from .platforms.registry import parse_platform

ProgressFn = Callable[[int, str], None]

#: 真正可运行的阶段
ACTIVE_STAGES = ("bill",)

#: 占位暂缓的阶段（含 UI 导航里的四个能力）
DEFERRED_STAGES = {
    "match": "SKU 对账",
    "cost": "成本匹配",
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
        }
    ]
    notes = {
        "match": "账单货款与巨益出入库单逐单匹配、货款分摊。算法已封存待启用。",
        "cost": "补商品信息与成本、三分处理意见、店铺/款号透视与毛利。算法已封存待启用。",
        "writeback": "已匹配单据回写 ADB 标记「已对账」与对账周期。算法已封存待启用。",
        "chuanyun": "对账结果推送氚云。氚云导入模板 sheet 已随账单整理导出，推送通道未接。",
    }
    for key in ("match", "cost", "writeback", "chuanyun"):
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


def run_match(*_args: Any, **_kwargs: Any) -> StageResult:
    """SKU 对账 —— 占位暂缓。实现在 core/pipeline_dormant.run_match。"""
    return deferred_result("match")


def run_cost(*_args: Any, **_kwargs: Any) -> StageResult:
    """成本匹配 —— 占位暂缓。实现在 core/pipeline_dormant.run_match 的成本段。"""
    return deferred_result("cost")


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
    deferred = [DEFERRED_STAGES[k] for k in ("match", "cost", "writeback", "chuanyun")]
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
