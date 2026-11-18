# -*- coding: utf-8 -*-
"""清空历史改版验证（2026-11-18：只清插件内信息，不删输出目录文件）。

对象：core/artifacts.clear_history + core/joblog.clear_records（条件过滤）
     + actions.clear_history（confirm 门）+ UI 精简静态检查。

纪律：临时 results 根 + 临时 runtime 目录 + 伪造批次/留痕/下载副本，
不碰真实结果目录、不连数据库、不发 HTTP。

用法：python scripts/verify_clear_history.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, r"D:\DesktopAgent")

import pandas as pd  # noqa: E402

from core import artifacts, config as cfg, joblog  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[OK] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


tmp = Path(tempfile.mkdtemp(prefix="clear_verify_"))
results = tmp / "results"
runtime = tmp / "runtime"
results.mkdir(parents=True)
runtime.mkdir(parents=True)

_orig_output_dir = artifacts.output_dir
_orig_runtime_dir = cfg.runtime_dir
artifacts.output_dir = lambda: results
cfg.runtime_dir = lambda: runtime


def make_bill_batch(name, period="2026-10", platform="douyin", shop="DY001"):
    d = artifacts.bill_root(period) / name
    (d / "底稿").mkdir(parents=True, exist_ok=True)
    fp = d / f"{name}_账单整理.xlsx"
    with pd.ExcelWriter(fp, engine="openpyxl") as w:
        pd.DataFrame({"店铺名称": ["店A"], "业务类别": ["货款"], "资金方向": ["收款"],
                      "金额": [100.0]}).to_excel(w, sheet_name="氚云导入模板", index=False)
        pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="账单透视", index=False)
    pd.DataFrame({"order_no": ["1"], "receipt_fen": [10000]}).to_csv(
        d / "底稿" / "bill_detail.csv", index=False, encoding="utf-8-sig")
    man = {"batch_id": f"bill-{name}", "platform": platform, "shop_code": shop,
           "shop_name": "测试店", "period": period, "status": "success",
           "detail_rows": 1, "goods_rows": 1,
           "files": {"excel": str(fp), "excel_list": json.dumps([str(fp)], ensure_ascii=False)}}
    (d / "底稿" / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    return d, fp


def snapshot(root: Path) -> dict:
    """目录内全部文件的 (相对路径, 大小, mtime_ns) 快照。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


try:
    d1, fp1 = make_bill_batch("2026-10-抖音-测试店-DY001")
    d2, fp2 = make_bill_batch("2026-09-抖音-测试店2-DY002", period="2026-09", shop="DY002")
    # sku匹配/ 与 汇总/ 产物
    sku_dir = artifacts.sku_root("2026-10", artifacts.SKU_STATE_COSTED) / "2026-10-抖音-测试店-DY001"
    (sku_dir / "底稿").mkdir(parents=True, exist_ok=True)
    (sku_dir / "sku.xlsx").write_bytes(b"fake")
    sum_dir = results / "2026-10" / "汇总"
    sum_dir.mkdir(parents=True, exist_ok=True)
    (sum_dir / "账单整理透视汇总表_x.xlsx").write_bytes(b"fake")
    # 用户手工目录（非批次）。放在年月层下、账单整理/ 之外——
    # iter_batch_dirs 只下钻 账单整理/，不会把它当批次候选
    # （results 根目录直挂是旧布局批次位置，list_batches 按既有兼容行为会列出，不属本次改版范围）
    manual = results / "2026-10" / "我的手工笔记"
    manual.mkdir(parents=True)
    (manual / "note.txt").write_text("user file", encoding="utf-8")

    before = snapshot(results)

    # 留痕：3 条（10月DY001 / 9月DY002 / 10月writeback全平台）
    joblog.save_job_log("bill-1", stage="bill", status="success", platform="douyin",
                        shop_code="DY001", period="2026-10", bill_rows=1)
    joblog.save_job_log("bill-2", stage="bill", status="success", platform="douyin",
                        shop_code="DY002", period="2026-09", bill_rows=2)
    joblog.save_job_log("wb-1", stage="writeback", status="success", platform="all",
                        shop_code="", period="2026-10", matched_rows=303)
    # downloads 副本 2 个
    dl = runtime / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    (dl / "a.xlsx").write_bytes(b"x")
    (dl / "b.xlsx").write_bytes(b"y")

    # ---------------- 条件清理：只删命中留痕 ----------------
    r = artifacts.clear_history(period="2026-09")
    check("条件清理 scoped=True", r["scoped"] is True)
    check("条件清理只删1条留痕", r["joblog_removed"] == 1, str(r))
    check("条件清理 batches_deleted=0", r["batches_deleted"] == 0)
    check("条件清理 output_files_kept=True", r["output_files_kept"] is True)
    check("条件清理 downloads 保留", r["downloads_removed"] == 0 and len(list(dl.iterdir())) == 2)
    recs = joblog.read_records()
    check("条件清理后剩2条（10月两条）", len(recs) == 2
          and all(x["period"] == "2026-10" for x in recs))
    check("条件清理 输出目录零变化", snapshot(results) == before)

    # 平台+店铺组合条件
    r = artifacts.clear_history(platform="douyin", shop_code="DY001")
    check("组合条件只删 bill-1", r["joblog_removed"] == 1
          and [x["batch_id"] for x in joblog.read_records()] == ["wb-1"])

    # ---------------- 全清：留痕+downloads，输出目录仍零变化 ----------------
    r = artifacts.clear_history()
    check("全清留痕1条", r["joblog_removed"] == 1 and joblog.read_records() == [])
    check("全清 downloads 2个", r["downloads_removed"] == 2 and not list(dl.iterdir()))
    check("全清 batches_deleted=0（不删批次）", r["batches_deleted"] == 0 and r["batches"] == [])
    check("全清 输出目录逐文件零变化（含mtime）", snapshot(results) == before)
    check("交付Excel仍在", fp1.is_file() and fp2.is_file())
    check("底稿仍在", (d1 / "底稿" / "bill_detail.csv").is_file()
          and (d1 / "底稿" / "manifest.json").is_file())
    check("sku匹配/汇总/手工目录仍在", sku_dir.is_dir() and sum_dir.is_dir() and manual.is_dir())

    # 清记录后批次清单仍可见（文件态，预期行为）
    batches = artifacts.list_batches(limit=50)
    check("清记录后批次仍可见（2个）", len(batches) == 2, str([b.dir.name for b in batches]))
    cov = artifacts.coverage(limit=50)
    check("清记录后覆盖统计仍在", cov["batch_count"] == 2 and cov["detail_rows"] == 2)

    # ---------------- actions 层：confirm 门 + 归一出口 ----------------
    import actions  # noqa: E402
    joblog.save_job_log("bill-3", stage="bill", status="success", platform="douyin",
                        shop_code="DY001", period="2026-10")
    r = actions.clear_history(confirm="")
    check("无confirm拒绝执行", r["ok"] is False and "confirm=yes" in r["summary"])
    check("拒绝后留痕未动", len(joblog.read_records()) == 1)
    r = actions.clear_history(confirm="yes")
    check("confirm=yes 执行成功", r["ok"] is True and r["data"]["joblog_removed"] == 1, r["summary"])
    check("summary 明示输出目录保留", "保留" in r["summary"], r["summary"])
    check("执行后输出目录仍零变化", snapshot(results) == before)
    r = actions.clear_history(confirm="yes", period="2026-10")
    check("条件清理归一 ok（0条也成功）", r["ok"] is True and r["data"]["scoped"] is True)

    # ---------------- UI 精简静态检查 ----------------
    html = (PLUGIN / "ui" / "index.html").read_text(encoding="utf-8")
    js = (PLUGIN / "ui" / "app.js").read_text(encoding="utf-8")
    cfgj = json.loads((PLUGIN / "config.json").read_text(encoding="utf-8"))
    pages = [p["id"] for p in cfgj["ui"]["pages"]]
    check("config.json 只剩4页", pages == ["home", "sources", "jobs", "settings"], str(pages))
    check("html 无五能力页 section",
          not re.search(r'data-page="(bill|match|cost|writeback|chuanyun)"', html))
    check("html 导航只剩4项", len(re.findall(r'<a href="#/', html)) == 4)
    check("js PAGES 只剩4页", 'const PAGES = ["home", "sources", "jobs", "settings"];' in js)
    check("js 无五能力页 loader/flow",
          not re.search(r"loadBillPage|loadMatchPage|loadCostPage|loadWritebackPage|"
                        r"loadChuanyunPage|runMatchFlow|runCostFlow|runWritebackFlow|runChuanyunFlow", js))
    check("js 无占位卡残留", "PLACEHOLDER_META" not in js and "renderPlaceholder" not in js)
    check("html 清空文案=只清插件内记录", "只清<b>插件内记录</b>" in html)
    check("js 清空 toast 明示文件保留", "输出目录文件全部保留" in js)
    # 保留页关键节点仍在
    for node in ("home-metrics", "home-capabilities", "shop-preview", "rule-table",
                 "job-log", "job-progress", "set-results-dir", "btn-clear-history"):
        check(f"保留节点 {node}", f'id="{node}"' in html)
finally:
    artifacts.output_dir = _orig_output_dir
    cfg.runtime_dir = _orig_runtime_dir
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n==== verify_clear_history: PASS={PASS} FAIL={FAIL} ====")
sys.exit(1 if FAIL else 0)