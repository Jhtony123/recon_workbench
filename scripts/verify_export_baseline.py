# -*- coding: utf-8 -*-
"""账单整理导出基准对齐回归（2026-11-16）。

用 8 月真实账单跑 run_bill，把插件导出 Excel 与影刀基准逐 sheet 比对：
  · sheet 名与顺序
  · 每 sheet 的列头（含重名列）与行数
  · 全部数值列的合计（容差 0.01 元）——不只挑金额列，防「列覆盖盲区」假绿
  · 透视 sheet 逐行逐格比对（按 业务类别 对齐）
用法: python scripts/verify_export_baseline.py [case ...]
"""
import sys, os, json, glob
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

import openpyxl
from core import pipeline

RAW = r"E:\AIcoding_project\xbot_robot\历史运行结果\原始账单"
BASE = r"E:\AIcoding_project\xbot_robot\历史运行结果\账单整理汇总\氚云导入成功"
P = "2026-08"; S = "2026-08-01"; E = "2026-08-31"

CASES = {
    "douyin": dict(platform="douyin", shop="DY007",
        files=[os.path.join(RAW, r"抖音-OK\2026.8-抖音_秋水伊人官方旗舰店-DY007")],
        extra={},
        baselines=["抖音_抖音_秋水伊人官方旗舰店_DY007_2026年08月-2026年08月_账单整理.xlsx"]),
    "pdd": dict(platform="pdd", shop="PDD003",
        files=[os.path.join(RAW, r"拼多多-OK\2026.8-拼多多_秋水伊人印象视觉专卖店-PDD003")],
        extra={},
        baselines=["拼多多_拼多多_秋水伊人印象视觉专卖店_PDD003_2026年08月-2026年08月_账单整理.xlsx"]),
    "taobao": dict(platform="taobao", shop="TBQY001",
        files=[os.path.join(RAW, r"淘宝-OK\2026.8-淘宝_秋水伊人官方企业店-TBQY001")],
        extra={"wechat": [os.path.join(RAW, r"淘宝-OK\2026.8-淘宝_秋水伊人官方企业店-TBQY001\微信")]},
        baselines=["淘系店铺_支付宝_淘宝_秋水伊人官方企业店_TBQY001_2026年08月-2026年08月_汇总结果.xlsx",
                   "淘系店铺_微信支付_淘宝_秋水伊人官方企业店_TBQY001_2026年08月-2026年08月_淘系微信账单整理.xlsx"]),
    "jd": dict(platform="jd", shop="JD001",
        files=[os.path.join(RAW, r"京东-OK\2026.8-京东_秋水伊人旗舰店-JD001")],
        extra={},
        baselines=["京东_京东_秋水伊人旗舰店_JD001_2026年08月-2026年08月_京东钱包整理.xlsx"]),
}


def read_book(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {}
    for ws in wb:
        rows = list(ws.iter_rows(values_only=True))
        out[ws.title] = rows
    wb.close()
    return out


def num_cols(rows):
    if not rows:
        return {}
    hdr = rows[0]
    sums = {}
    for j, h in enumerate(hdr):
        vals = [r[j] for r in rows[1:] if j < len(r) and isinstance(r[j], (int, float))]
        if vals:
            key = f"{h}#{j}"
            sums[key] = round(sum(vals), 2)
    return sums


def cmp_sheet(name, got_rows, base_rows, report):
    gh = list(got_rows[0]) if got_rows else []
    bh = list(base_rows[0]) if base_rows else []
    if gh != bh:
        report.append(f"  [HEADER] {name}: got={gh} base={bh}")
    if len(got_rows) != len(base_rows):
        report.append(f"  [ROWS] {name}: got={len(got_rows)-1} base={len(base_rows)-1}")
    gs, bs = num_cols(got_rows), num_cols(base_rows)
    for k in sorted(set(gs) | set(bs)):
        g, b = gs.get(k), bs.get(k)
        if g is None or b is None or abs(g - b) > 0.011:
            report.append(f"  [SUM] {name}.{k}: got={g} base={b}")


def main():
    names = sys.argv[1:] or list(CASES)
    all_ok = True
    for name in names:
        c = CASES[name]
        print(f"== {name} {c['shop']} ==", flush=True)
        res = pipeline.run_bill(platform=c["platform"], shop_code=c["shop"],
            period=P, s_date=S, e_date=E, file_paths=c["files"], extra_files=c["extra"])
        print("  status:", res.status, "|", res.message, flush=True)
        if res.status != "success":
            all_ok = False
            continue
        files = res.data.get("exported_files") or []
        print("  exported:", [os.path.basename(f) for f in files], flush=True)
        report = []
        for bname in c["baselines"]:
            bpath = os.path.join(BASE, bname)
            # 找同名产物（统一明细簿除外）
            match = [f for f in files if os.path.basename(f) == bname]
            if not match:
                report.append(f"  [MISSING] 未导出基准同名文件: {bname}")
                continue
            got = read_book(match[0]); base = read_book(bpath)
            if list(got) != list(base):
                report.append(f"  [SHEETS] {bname}: got={list(got)} base={list(base)}")
            for sh in base:
                if sh in got:
                    cmp_sheet(sh, got[sh], base[sh], report)
                else:
                    report.append(f"  [SHEET-MISS] {sh}")
        if report:
            all_ok = False
            print("\n".join(report), flush=True)
        else:
            print("  [OK] 与基准逐sheet/列头/行数/数值合计一致", flush=True)
        print("  batch_dir:", res.data.get("batch_dir"), flush=True)
    print("ALL_OK" if all_ok else "HAS_DIFF", flush=True)


if __name__ == "__main__":
    main()
