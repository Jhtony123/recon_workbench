# -*- coding: utf-8 -*-
"""唯品会三旬交付件 vs 10月对照基准：只比对不重跑（run_oct.py vip 崩溃后补比对）。"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, os.path.join(r"D:\DesktopAgent\extensions\user\recon_workbench", "scripts"))
from run_oct import BASE, read_book, cmp_sheet, _norm

GOT_DIR = r"C:\Users\贺杰\Desktop\账单\2026-10\账单整理\2026-10-唯品会-唯品中国"
got_files = [os.path.join(GOT_DIR, n) for n in os.listdir(GOT_DIR) if n.endswith(".xlsx")]
baselines = [n for n in sorted(os.listdir(BASE)) if n.startswith("唯品会_")]
all_ok = True
for bname in baselines:
    match = [f for f in got_files if _norm(f) == _norm(bname)]
    print(f"== {bname} ==", flush=True)
    if not match:
        print("  [MISSING]", flush=True); all_ok = False; continue
    got = read_book(match[0]); base = read_book(os.path.join(BASE, bname))
    report = []
    if list(got) != list(base):
        report.append(f"  [SHEETS] got={list(got)} base={list(base)}")
    for sh in base:
        if sh in got:
            cmp_sheet(sh, got[sh], base[sh], report)
        else:
            report.append(f"  [SHEET-MISS] {sh}")
    if report:
        all_ok = False
        print("\n".join(report[:60]), flush=True)
    else:
        print("  [OK] 逐sheet/列头/行数/全部数值列合计一致", flush=True)
print("ALL_OK" if all_ok else "HAS_DIFF", flush=True)
