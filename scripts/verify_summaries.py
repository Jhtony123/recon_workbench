# -*- coding: utf-8 -*-
"""独立透视汇总工具验证（2026-11-18）。

对象：core/summaries.py（bill_pivot_summary / sku_pivot_summary，
承接影刀 cost_match.final_bill_pivot / final_pivot）。

纪律：纯本地文件聚合，本脚本用临时 results 根 + 伪造批次 Excel，
不碰真实结果目录、不连数据库、不推氚云。

用法：python scripts/verify_summaries.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, r"D:\DesktopAgent")

import pandas as pd  # noqa: E402

from core import artifacts, exporters, summaries  # noqa: E402

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


tmp = Path(tempfile.mkdtemp(prefix="sum_verify_"))
_orig_output_dir = artifacts.output_dir
artifacts.output_dir = lambda: tmp
_orig_runtime = summaries.runtime_dir
summaries.runtime_dir = lambda: tmp / "runtime"


def make_bill_batch(name, pivot_rows, *, sheet_name="抖音账单透视", first_sheet_junk=True):
    """伪造账单整理批次：交付 Excel（透视 sheet 位置可变，验证按列识别）。"""
    root = artifacts.bill_root("2026-10")
    d = root / name
    (d / "底稿").mkdir(parents=True, exist_ok=True)
    fp = d / f"{name}_账单整理.xlsx"
    cols = list(summaries.BILL_PIVOT_COLUMNS)
    with pd.ExcelWriter(fp, engine="openpyxl") as w:
        if first_sheet_junk:
            pd.DataFrame({"店铺名称": ["x"], "业务类别": ["货款"], "资金方向": ["收款"],
                          "金额": [1.0]}).to_excel(w, sheet_name="氚云导入模板", index=False)
        pd.DataFrame(pivot_rows, columns=cols).to_excel(w, sheet_name=sheet_name, index=False)
    man = {"batch_id": f"bill-{name}", "platform": "douyin", "shop_code": "DY001",
           "shop_name": "测试店", "period": "2026-10", "status": "success",
           "files": {"excel": str(fp), "excel_list": json.dumps([str(fp)], ensure_ascii=False)}}
    (d / "底稿" / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    return d, fp


def make_sku_batch(name, state, *, shop_rows, product_rows, match_rows, rec_rows, ref_rows,
                   multi_file=False):
    """伪造 SKU 批次：交付 Excel 5 sheet（match/cost 同名）。"""
    root = artifacts.sku_root("2026-10", state)
    d = root / name
    (d / "底稿").mkdir(parents=True, exist_ok=True)
    files = []
    payloads = [(f"{name}_账单匹配结果.xlsx", shop_rows, product_rows, match_rows, rec_rows, ref_rows)]
    if multi_file:  # VIP 多旬：两份交付件
        payloads.append((f"{name}_旬2_账单匹配结果.xlsx", shop_rows, product_rows, match_rows,
                         rec_rows, ref_rows))
    for fname, s, p, m, r, f in payloads:
        fp = d / fname
        with pd.ExcelWriter(fp, engine="openpyxl") as w:
            pd.DataFrame(s).to_excel(w, sheet_name=exporters.SHEET_SHOP, index=False)
            pd.DataFrame(p).to_excel(w, sheet_name=exporters.SHEET_PRODUCT, index=False)
            pd.DataFrame(m).to_excel(w, sheet_name=exporters.SHEET_MATCH, index=False)
            pd.DataFrame(r).to_excel(w, sheet_name=exporters.SHEET_RECEIPT, index=False)
            pd.DataFrame(f).to_excel(w, sheet_name=exporters.SHEET_REFUND, index=False)
            pd.DataFrame({"id": ["1"]}).to_excel(w, sheet_name=exporters.SHEET_OMS, index=False)
        files.append(fp)
    man = {"batch_id": f"match-{name}", "platform": "douyin", "shop_code": "DY001",
           "period": "2026-10", "status": "success",
           "files": {"excel": str(files[0]),
                     "excel_list": json.dumps([str(x) for x in files], ensure_ascii=False)}}
    (d / "底稿" / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    # 底稿 match_detail（sku_batch_units 识别单元用）
    pd.DataFrame({"订单编号": ["1"], "货款分摊金额": [1]}).to_csv(
        d / "底稿" / "match_detail.csv", index=False, encoding="utf-8-sig")
    return d, files


try:
    # ---------------- 账单透视汇总 ----------------
    rows_a = [["店A", "DY001", "2026年10月-2026年10月", "抖音", "货款", 1000.0, 100.0, 900.0],
              ["店A", "DY001", "2026年10月-2026年10月", "抖音", "佣金", -50.0, 0.0, -50.0]]
    rows_b = [["店B", "DY002", "2026年10月-2026年10月", "抖音", "货款", 2000.5, 200.25, 1800.25]]
    d_a, fp_a = make_bill_batch("2026-10-抖音-店A-DY001", rows_a)
    d_b, fp_b = make_bill_batch("2026-10-抖音-店B-DY002", rows_b,
                                sheet_name="账单透视", first_sheet_junk=False)
    # 无透视 sheet 的批次 → skipped 不报错
    d_c = artifacts.bill_root("2026-10") / "2026-10-抖音-无透视-DY003"
    d_c.mkdir(parents=True, exist_ok=True)
    fp_c = d_c / "x_账单整理.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(fp_c, sheet_name="明细", index=False)

    res = summaries.bill_pivot_summary(period="2026-10")
    check("账单汇总 success", res["status"] == "success", res["message"])
    check("账单汇总 3 行（2+1）", res["rows"] == 3, str(res["rows"]))
    check("账单汇总 合计（收2950.5/退300.25/净2650.25）",
          res["totals"]["收款"] == 2950.5 and res["totals"]["退款"] == 300.25
          and res["totals"]["现金流"] == 2650.25, str(res["totals"]))
    check("账单汇总 店铺数=2（无透视批次不计）", res["totals"]["店铺数"] == 2)
    check("账单汇总 无透视文件 skipped", any(s.get("file") == fp_c.name for s in res["skipped"]))
    out = pd.read_excel(res["file"], sheet_name="账单整理透视汇总表")
    check("输出 8 列列序与影刀一致", list(out.columns) == list(summaries.BILL_PIVOT_COLUMNS))
    check("输出落 汇总/ 目录", Path(res["file"]).parent.name == "汇总")
    check("下载副本存在", res["downloads"] and Path(
        tmp / "runtime" / "downloads" / res["downloads"][0]["url"].rsplit("/", 1)[-1]).is_file())
    check("批次产物未被修改", fp_a.is_file() and fp_b.is_file())

    # 单批次过滤 + 平台过滤
    res1 = summaries.bill_pivot_summary(period="2026-10", bill_batch_id=d_a.name)
    check("bill_batch_id 过滤只汇总1批", res1["rows"] == 2 and res1["batches"] == 1)
    try:
        summaries.bill_pivot_summary(period="2026-10", platform="jd")
        check("platform 过滤无批次→可读报错", False)
    except Exception as exc:
        check("platform 过滤无批次→可读报错", "账单整理" in str(exc))

    # period 必填 / 无批次
    try:
        summaries.bill_pivot_summary(period="")
        check("账单汇总 period必填", False)
    except Exception as exc:
        check("账单汇总 period必填", "period" in str(exc))
    try:
        summaries.bill_pivot_summary(period="2099-01")
        check("账单汇总 无批次可读报错", False)
    except Exception as exc:
        check("账单汇总 无批次可读报错", "账单整理" in str(exc))

    # ---------------- SKU 透视汇总 ----------------
    shop_rows = [{"店铺名称": "店A", "处理意见": "结转成本", "货款分摊金额": 900.0, "数量": 9},
                 {"店铺名称": "店A", "处理意见": "冲成本", "货款分摊金额": 100.0, "数量": 1}]
    product_rows = [{"店铺名称": "店A", "款号": "K1", "货款分摊金额": 1000.0, "数量": 10}]
    match_rows = [{"店铺名称": "店A", "款号": "K1", "货款分摊金额": 1000.0, "结算金额": 1100.0}]
    rec_rows = [{"店铺名称": "店A", "订单日期": "2026-09-15", "货款分摊金额": 400.0},
                {"店铺名称": "店A", "订单日期": "2026-10-02", "货款分摊金额": 500.0},
                {"店铺名称": "店A", "订单日期": "", "货款分摊金额": 100.0}]
    ref_rows = [{"店铺名称": "店A", "订单日期": "2026-10-20", "货款分摊金额": 100.0}]
    d_s1, fs1 = make_sku_batch("2026-10-抖音-店A-DY001", artifacts.SKU_STATE_COSTED,
                               shop_rows=shop_rows, product_rows=product_rows,
                               match_rows=match_rows, rec_rows=rec_rows, ref_rows=ref_rows)
    d_s2, fs2 = make_sku_batch("2026-10-抖音-店B-DY002", artifacts.SKU_STATE_PENDING_COST,
                               shop_rows=[{"店铺名称": "店B", "处理意见": "结转成本",
                                           "货款分摊金额": 2000.0, "数量": 20}],
                               product_rows=[{"店铺名称": "店B", "款号": "K2",
                                              "货款分摊金额": 2000.0, "数量": 20}],
                               match_rows=[{"店铺名称": "店B", "款号": "K2",
                                            "货款分摊金额": 2000.0, "结算金额": 2200.0}],
                               rec_rows=[{"店铺名称": "店B", "订单日期": "2026-10-05",
                                          "货款分摊金额": 2000.0}],
                               ref_rows=[])
    # VIP 多旬（两份额外翻倍）
    d_vip, fs_vip = make_sku_batch("2026-10-唯品会-唯品中国", artifacts.SKU_STATE_WRITTEN_BACK,
                                   shop_rows=[{"店铺名称": "唯品中国", "处理意见": "结转成本",
                                               "货款分摊金额": 300.0, "数量": 3}],
                                   product_rows=[{"店铺名称": "唯品中国", "款号": "K3",
                                                  "货款分摊金额": 300.0, "数量": 3}],
                                   match_rows=[{"店铺名称": "唯品中国", "款号": "K3",
                                                "货款分摊金额": 300.0, "结算金额": 330.0}],
                                   rec_rows=[{"店铺名称": "唯品中国", "订单日期": "2026-10-08",
                                              "货款分摊金额": 300.0}],
                                   ref_rows=[], multi_file=True)

    res = summaries.sku_pivot_summary(period="2026-10")
    check("SKU汇总 success", res["status"] == "success", res["message"])
    check("SKU汇总 三态全扫 4 文件（1+1+2旬）", res["files"] == 4 and res["batches"] == 3,
          f"files={res['files']} batches={res['batches']}")
    # 原 final_pivot 口径：三张透视纯 concat 不再聚合 → 店A 2 + 店B 1 + VIP 1×2旬 = 5
    check("SKU汇总 店铺透视 5 行（2+1+1×2旬，concat不聚合）", res["rows"]["店铺数据透视汇总"] == 5,
          str(res["rows"]))
    check("SKU汇总 款号透视 4 行", res["rows"]["款号数据透视汇总"] == 4)
    check("SKU汇总 匹配明细 4 行", res["rows"]["数据透视明细汇总"] == 4)

    book = pd.ExcelFile(res["file"])
    check("输出 4 sheet 名与影刀一致",
          book.sheet_names == ["店铺数据透视汇总", "款号数据透视汇总", "数据透视明细汇总", "按订单年月透视"],
          str(book.sheet_names))
    ym = book.parse("按订单年月透视")
    check("订单年月透视列（收款金额/退款金额）",
          set(["店铺名称", "订单年月", "收款金额", "退款金额"]) <= set(ym.columns), str(ym.columns))
    ya = ym[ym["店铺名称"] == "店A"].set_index("订单年月")
    check("店A 202609 收款400", float(ya.loc["202609", "收款金额"]) == 400.0)
    check("店A 202610 收款500/退款100（outer merge）",
          float(ya.loc["202610", "收款金额"]) == 500.0
          and float(ya.loc["202610", "退款金额"]) == 100.0)
    check("店A 无日期桶 收款100", float(ya.loc["无日期", "收款金额"]) == 100.0)
    check("店A 202609 退款 NaN→0（outer merge 补零）",
          float(ya.loc["202609", "退款金额"]) == 0.0)
    yb = ym[ym["店铺名称"] == "店B"]
    check("店B 只有收款行（无退款）", len(yb) == 1 and float(yb.iloc[0]["收款金额"]) == 2000.0)
    yv = ym[ym["店铺名称"] == "唯品中国"]
    check("VIP 两旬收款合并 600（300×2）",
          float(yv["收款金额"].sum()) == 600.0, str(yv))
    check("SKU汇总 totals 收款合计 3600（1000+2000+600）",
          res["totals"]["收款金额_订单年月"] == 3600.0, str(res["totals"]))
    check("SKU汇总 店铺透视货款合计 3600（1000+2000+300×2）",
          res["totals"]["货款分摊金额_店铺透视"] == 3600.0)
    check("SKU汇总 店铺数=3", res["totals"]["店铺数"] == 3)
    check("SKU汇总 输出落 汇总/", Path(res["file"]).parent.name == "汇总")
    check("SKU汇总 批次产物未修改", all(f.is_file() for f in fs1 + fs2 + fs_vip))

    # state 单态过滤
    res_c = summaries.sku_pivot_summary(period="2026-10", state=artifacts.SKU_STATE_COSTED)
    check("state=已匹配成本 只1批", res_c["batches"] == 1 and res_c["files"] == 1)
    # bill_batch_id 过滤（VIP 目录名）
    res_v = summaries.sku_pivot_summary(period="2026-10", bill_batch_id=d_vip.name)
    check("bill_batch_id 过滤 VIP 2旬文件", res_v["files"] == 2 and res_v["batches"] == 1)
    # 自吞防护：汇总输出在 汇总/ 目录，不会被再次扫入
    res_again = summaries.sku_pivot_summary(period="2026-10")
    check("重跑不吞上次汇总输出（行数不变）",
          res_again["rows"] == res["rows"], str(res_again["rows"]))

    try:
        summaries.sku_pivot_summary(period="")
        check("SKU汇总 period必填", False)
    except Exception as exc:
        check("SKU汇总 period必填", "period" in str(exc))

    # ---------------- actions 接线 ----------------
    import actions  # noqa: E402
    r = actions.bill_pivot_summary(period="2026-10")
    check("actions.bill_pivot_summary 归一 ok", r["ok"] and r["data"]["rows"] == 3, r["summary"])
    r = actions.sku_pivot_summary(period="2026-10")
    check("actions.sku_pivot_summary 归一 ok", r["ok"] and r["data"]["rows"]["按订单年月透视"] > 0,
          r["summary"])
    r = actions.bill_pivot_summary(period="")
    check("actions period缺失→fail不抛", r["ok"] is False and "period" in r["summary"])
finally:
    artifacts.output_dir = _orig_output_dir
    summaries.runtime_dir = _orig_runtime
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n==== verify_summaries: PASS={PASS} FAIL={FAIL} ====")
sys.exit(1 if FAIL else 0)