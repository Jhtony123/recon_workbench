"""独立透视汇总工具（承接 cost_match.final_bill_pivot / final_pivot，2026-11-18）。

两个纯本地聚合服务（不连数据库、不推氚云、不修改任何批次产物）：
  · bill_pivot_summary  店铺账单整理透视汇总：
      扫账单整理批次交付 Excel 里的 8 列透视 sheet（各平台 sheet 名不同，
      按列结构识别而非影刀的「第 2 个 sheet」位置假设），concat 成
      「账单整理透视汇总表.xlsx」（单 sheet，8 列，元）。
  · sku_pivot_summary   店铺sku匹配透视汇总：
      扫 SKU 批次交付 Excel 的 5 个 sheet（店铺数据透视/款号数据透视/匹配透视/
      收款匹配/退款匹配），三张透视 concat；收退款明细按 店铺名称×订单年月
      汇总货款分摊金额后 outer merge（原 final_pivot 口径），输出
      「sku匹配数据透视汇总表_{时间戳}.xlsx」4 sheet。

相对原脚本的优化适配：
  · 原 res_dir 平铺目录 glob → 按插件文件态布局发现批次
    （账单整理/{批次}、sku匹配/{状态}/{批次}），period 必填防跨账期叠加；
    SKU 缺省三态全扫（同一批次只会在一个状态目录，天然不重复），
    VIP 多旬交付件逐份计入；排除 ~$ 临时文件与 汇总/ 输出目录防自吞
  · 原 final_bill_pivot 读 sheet_name=1（位置假设，平台 sheet 序一变就错）
    → 按 8 列结构识别透视 sheet；缺列文件记入 skipped 清单而非整体抛错
  · 原读失败 print 吞掉 → 逐文件聚合 errors/skipped，汇总终态如实报
  · 输出落 {results}/{YYYY-MM}/汇总/（重跑带时间戳不覆盖），
    同步 downloads 副本供管理页/聊天下载
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pandas as pd

from . import artifacts, exporters
from .config import runtime_dir
from .errors import DataSourceEmpty, ReconError

#: 账单透视 8 列（原 final_bill_pivot required_columns，逐字一致）
BILL_PIVOT_COLUMNS = ("店铺名称", "店铺编码", "账单周期", "账单类型", "业务类别", "收款", "退款", "现金流")

#: SKU 汇总读的 5 个 sheet（原 final_pivot；match/cost 阶段同名）
SKU_SHEETS = (exporters.SHEET_SHOP, exporters.SHEET_PRODUCT, exporters.SHEET_MATCH,
              exporters.SHEET_RECEIPT, exporters.SHEET_REFUND)

SUMMARY_DIRNAME = "汇总"


def summary_root(period: str) -> Path:
    d = artifacts.output_dir() / artifacts.period_month(period) / SUMMARY_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deliver_excels(batch_dir: Path) -> list[Path]:
    """批次根目录交付 xlsx（不递归：底稿/汇总/下载副本都不算交付件）。"""
    return sorted(
        p for p in Path(batch_dir).glob("*.xlsx")
        if p.is_file() and not p.name.startswith("~$") and ".cost-tmp" not in p.name
    )


def _copy_download(path: Path) -> dict | None:
    try:
        dl_dir = runtime_dir() / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        dl_name = f"{time.strftime('%Y%m%d_%H%M%S')}_{path.name}"
        shutil.copy2(path, dl_dir / dl_name)
        return {"name": path.name,
                "url": f"/agent/extensions/recon_workbench/files/downloads/{dl_name}"}
    except OSError:
        return None


def _num(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


# ------------------------------------------------------------------ 账单整理透视汇总

def bill_pivot_summary(
    *,
    period: str,
    platform: str = "",
    shop_code: str = "",
    bill_batch_id: str = "",
    progress=None,
) -> dict:
    """店铺账单整理透视汇总（原 cost_match.final_bill_pivot）。

    返回 {status, message, file, downloads, rows, totals, batches, files, skipped, errors}。
    """
    t0 = time.time()
    period = str(period or "").strip()
    if not period:
        raise ReconError("账单透视汇总必须指定账期 period（YYYY-MM）")

    batches = artifacts.list_batches(period=period, platform=platform,
                                     shop_code=shop_code, limit=500)
    if bill_batch_id:
        target = str(bill_batch_id).strip()
        batches = [b for b in batches if target in (b.batch_id, b.dir.name)]
    if not batches:
        raise DataSourceEmpty("没有可汇总的账单整理批次，请先执行「账单整理」",
                              detail={"period": period, "platform": platform,
                                      "shop_code": shop_code})

    parts: list[pd.DataFrame] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    n_files = 0
    n = len(batches)
    for i, b in enumerate(batches):
        excels = artifacts._json_list(b.files.get("excel_list")) or (
            [b.files["excel"]] if b.files.get("excel") else [])
        if not excels:
            excels = [str(p) for p in _deliver_excels(b.dir)]
        if not excels:
            skipped.append({"batch": b.dir.name, "reason": "批次无交付 Excel"})
            continue
        for fp in excels:
            p = Path(fp)
            n_files += 1
            try:
                xl = pd.ExcelFile(p)
            except Exception as exc:
                errors.append({"file": p.name, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            hit = False
            for name in xl.sheet_names:
                try:
                    df = xl.parse(name)
                except Exception:
                    continue
                if not all(c in df.columns for c in BILL_PIVOT_COLUMNS):
                    continue
                hit = True
                sub = df.loc[:, list(BILL_PIVOT_COLUMNS)].copy()
                sub = _num(sub, ("收款", "退款", "现金流"))
                parts.append(sub)
            if not hit:
                skipped.append({"batch": b.dir.name, "file": p.name,
                                "reason": "未找到 8 列账单透视 sheet"})
        if progress:
            progress(int((i + 1) * 90 / n), f"账单透视汇总 {i + 1}/{n} 批次")

    if not parts:
        return {
            "status": "skipped",
            "message": f"没有可汇总的账单透视（{n_files} 个文件，{len(skipped)} 跳过 / {len(errors)} 读失败）",
            "file": "", "downloads": [], "rows": 0, "totals": {},
            "batches": len(batches), "files": n_files,
            "skipped": skipped, "errors": errors,
            "duration_sec": round(time.time() - t0, 2),
        }

    merged = pd.concat(parts, ignore_index=True).loc[:, list(BILL_PIVOT_COLUMNS)]
    out = summary_root(period) / f"账单整理透视汇总表_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    with pd.ExcelWriter(str(out), engine=exporters._excel_engine()) as writer:
        merged.to_excel(writer, sheet_name="账单整理透视汇总表", index=False)
    dl = _copy_download(out)
    totals = {
        "收款": round(float(merged["收款"].sum()), 2),
        "退款": round(float(merged["退款"].sum()), 2),
        "现金流": round(float(merged["现金流"].sum()), 2),
        "店铺数": int(merged["店铺名称"].nunique()),
    }
    if progress:
        progress(100, f"账单透视汇总完成：{len(merged)} 行")
    return {
        "status": "success",
        "message": (f"账单整理透视汇总完成：{len(batches)} 批次 / {n_files} 文件 → "
                    f"{len(merged)} 行（收款 {totals['收款']} / 退款 {totals['退款']} / "
                    f"现金流 {totals['现金流']} 元）"),
        "file": str(out), "downloads": [dl] if dl else [],
        "rows": len(merged), "totals": totals,
        "batches": len(batches), "files": n_files,
        "skipped": skipped, "errors": errors,
        "duration_sec": round(time.time() - t0, 2),
    }


# ------------------------------------------------------------------ SKU 匹配透视汇总

def _calamine_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("python_calamine") is not None


def _sheet_names(path: Path) -> list[str]:
    """sheet 名清单：calamine 优先（Rust 引擎，大簿秒级），回落 openpyxl read_only。"""
    if _calamine_available():
        from python_calamine import CalamineWorkbook
        return list(CalamineWorkbook.from_path(str(path)).sheet_names)
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _read_sheet_fast(path: Path, sheet: str, usecols=None) -> pd.DataFrame:
    """读单 sheet：calamine 优先（VIP 67MB 大簿 openpyxl 要分钟级，calamine 秒级），
    缺失/失败回落 openpyxl read_only 流式读。usecols=None 读全列。
    """
    if _calamine_available():
        try:
            cols = list(usecols) if usecols else None
            return pd.read_excel(str(path), sheet_name=sheet, usecols=cols,
                                 engine="calamine")
        except Exception:
            pass  # 回落 openpyxl（引擎对个别簿的兼容差异不该让汇总整体失败）
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            return pd.DataFrame()
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            return pd.DataFrame()
        header = [str(h) if h is not None else f"col{i}" for i, h in enumerate(header)]
        if usecols:
            idx = [i for i, h in enumerate(header) if h in usecols]
            cols = [header[i] for i in idx]
            data = [[r[i] if i < len(r) else None for i in idx] for r in rows]
        else:
            cols = header
            data = [list(r) for r in rows]
        return pd.DataFrame(data, columns=cols)
    finally:
        wb.close()


def _order_ym(df: pd.DataFrame) -> pd.DataFrame:
    """订单日期 → 订单年月（原 final_pivot：'%Y%m'，无日期='无日期'）。"""
    if "订单日期" in df.columns:
        d = pd.to_datetime(df["订单日期"], errors="coerce")
        df["订单年月"] = d.map(lambda x: x.strftime("%Y%m") if pd.notna(x) else "无日期")
    else:
        df["订单年月"] = "无日期"
    return df


def _shop_month_pivot(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "店铺名称" not in df.columns or "货款分摊金额" not in df.columns:
        return pd.DataFrame(columns=["店铺名称", "订单年月", "货款分摊金额"])
    return pd.pivot_table(df, index=["店铺名称", "订单年月"], values=["货款分摊金额"],
                          aggfunc="sum").reset_index()


def sku_pivot_summary(
    *,
    period: str,
    platform: str = "",
    shop_code: str = "",
    bill_batch_id: str = "",
    state: str = "",
    progress=None,
) -> dict:
    """店铺sku匹配透视汇总（原 cost_match.final_pivot）。

    缺省扫三态（未匹配成本/已匹配成本/已回传数据库）——同一批次只会在一个
    状态目录，不会重复计；state 可指定单态。cost 阶段产物优先（同目录原地覆盖，
    读到的就是最新交付件）。
    """
    t0 = time.time()
    period = str(period or "").strip()
    if not period:
        raise ReconError("SKU透视汇总必须指定账期 period（YYYY-MM）")
    st = str(state or "").strip()
    states = (st,) if st in artifacts.SKU_STATES else artifacts.SKU_STATES

    batches = artifacts.list_sku_batches(period, states=states,
                                         platform=platform, shop_code=shop_code)
    if bill_batch_id:
        target = str(bill_batch_id).strip()
        batches = [b for b in batches if target in (b.batch_id, b.dir.name)]
    if not batches:
        raise DataSourceEmpty("没有可汇总的 SKU 对账批次，请先执行「SKU对账」",
                              detail={"period": period, "states": list(states)})

    lists: dict[str, list[pd.DataFrame]] = {s: [] for s in SKU_SHEETS}
    skipped: list[dict] = []
    errors: list[dict] = []
    n_files = 0
    n = len(batches)
    for i, b in enumerate(batches):
        excels = _deliver_excels(b.dir)  # VIP 多旬逐份；底稿/汇总不递归
        if not excels:
            skipped.append({"batch": b.dir.name, "reason": "批次无交付 Excel"})
            continue
        for p in excels:
            n_files += 1
            try:
                names = set(_sheet_names(p))
            except Exception as exc:
                errors.append({"file": p.name, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            hit = False
            for sheet in SKU_SHEETS:
                if sheet not in names:
                    continue
                try:
                    # 明细只取汇总所需 3 列（大簿提速）；透视读全列
                    usecols = ({"店铺名称", "订单日期", "货款分摊金额"}
                               if sheet in (exporters.SHEET_RECEIPT, exporters.SHEET_REFUND)
                               else None)
                    df = _read_sheet_fast(p, sheet, usecols=usecols)
                except Exception as exc:
                    errors.append({"file": p.name, "sheet": sheet,
                                   "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
                    continue
                hit = True
                lists[sheet].append(df)
            if not hit:
                skipped.append({"batch": b.dir.name, "file": p.name,
                                "reason": "无 SKU 对账 5 sheet（非匹配结果文件）"})
        if progress:
            progress(int((i + 1) * 90 / n), f"SKU透视汇总 {i + 1}/{n} 批次")

    def _cat(sheet: str) -> pd.DataFrame:
        frames = [d for d in lists[sheet] if d is not None and not d.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    df_shop = _cat(exporters.SHEET_SHOP)
    df_product = _cat(exporters.SHEET_PRODUCT)
    df_match = _cat(exporters.SHEET_MATCH)
    df_receipt = _order_ym(_cat(exporters.SHEET_RECEIPT))
    df_refund = _order_ym(_cat(exporters.SHEET_REFUND))
    df_receipt = _num(df_receipt, ("货款分摊金额",))
    df_refund = _num(df_refund, ("货款分摊金额",))

    p_rec = _shop_month_pivot(df_receipt)
    p_pay = _shop_month_pivot(df_refund)
    if p_rec.empty and p_pay.empty:
        merged = pd.DataFrame(columns=["店铺名称", "订单年月", "收款金额", "退款金额"])
    else:
        merged = pd.merge(p_rec, p_pay, on=["店铺名称", "订单年月"], how="outer",
                          suffixes=("_receipt", "_pay"))
        merged = merged.rename(columns={"货款分摊金额_receipt": "收款金额",
                                        "货款分摊金额_pay": "退款金额"})
        merged = _num(merged, ("收款金额", "退款金额"))

    if df_shop.empty and df_product.empty and df_match.empty and merged.empty:
        return {
            "status": "skipped",
            "message": f"没有可汇总的 SKU 透视（{n_files} 个文件，{len(skipped)} 跳过 / {len(errors)} 读失败）",
            "file": "", "downloads": [], "rows": {}, "totals": {},
            "batches": len(batches), "files": n_files,
            "skipped": skipped, "errors": errors,
            "duration_sec": round(time.time() - t0, 2),
        }

    out = summary_root(period) / f"sku匹配数据透视汇总表_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    with pd.ExcelWriter(str(out), engine=exporters._excel_engine()) as writer:
        df_shop.to_excel(writer, sheet_name="店铺数据透视汇总", index=False)
        df_product.to_excel(writer, sheet_name="款号数据透视汇总", index=False)
        df_match.to_excel(writer, sheet_name="数据透视明细汇总", index=False)
        merged.to_excel(writer, sheet_name="按订单年月透视", index=False)
    dl = _copy_download(out)

    def _sum(df: pd.DataFrame, col: str) -> float:
        if df.empty or col not in df.columns:
            return 0.0
        return round(float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum()), 2)

    rows = {"店铺数据透视汇总": len(df_shop), "款号数据透视汇总": len(df_product),
            "数据透视明细汇总": len(df_match), "按订单年月透视": len(merged)}
    totals = {
        "货款分摊金额_店铺透视": _sum(df_shop, "货款分摊金额"),
        "收款金额_订单年月": _sum(merged, "收款金额"),
        "退款金额_订单年月": _sum(merged, "退款金额"),
        "店铺数": int(merged["店铺名称"].nunique()) if not merged.empty else 0,
    }
    if progress:
        progress(100, f"SKU透视汇总完成：{sum(rows.values())} 行")
    return {
        "status": "success",
        "message": (f"SKU匹配透视汇总完成：{len(batches)} 批次 / {n_files} 文件 → "
                    f"店铺透视 {rows['店铺数据透视汇总']} / 款号透视 {rows['款号数据透视汇总']} / "
                    f"匹配明细 {rows['数据透视明细汇总']} / 订单年月 {rows['按订单年月透视']} 行"),
        "file": str(out), "downloads": [dl] if dl else [],
        "rows": rows, "totals": totals,
        "batches": len(batches), "files": n_files,
        "skipped": skipped, "errors": errors,
        "duration_sec": round(time.time() - t0, 2),
    }