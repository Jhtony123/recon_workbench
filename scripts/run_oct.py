# -*- coding: utf-8 -*-
"""2026-10 账单整理批量执行 + 对照基准比对（2026-11-16 新目录布局首跑）。

源:   C:\\Users\\贺杰\\Desktop\\2026.10月账单\\2026年10月原始账单（12 平台目录带 -OK）
基准: 同级 帐单整理汇总-已有结果可作为对照验证（影刀 10 月真实产出 52 份）
比对: sheet 名与顺序 / 列头 / 行数 / 全部数值列合计（容差 0.011 元，防假绿）
用法: python scripts/run_oct.py [case ...]   不带参数=全量（vip 建议单独后台跑）
"""
import sys, os, re
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

import openpyxl
from core import pipeline
from core.platforms.registry import parse_platform

ROOT = r"C:\Users\贺杰\Desktop\2026.10月账单"
RAW = os.path.join(ROOT, "2026年10月原始账单")
BASE = os.path.join(ROOT, "帐单整理汇总-已有结果可作为对照验证")
P = "2026-10"; S = "2026-10-01"; E = "2026-10-31"


def build_cases():
    cases = {}
    for pdir in sorted(os.listdir(RAW)):
        full = os.path.join(RAW, pdir)
        if not os.path.isdir(full):
            continue
        display = pdir.removesuffix("-OK")
        # 目录名别名：淘宝/天猫 共用淘系适配器（display_name=淘系店铺）
        plat = parse_platform({"淘宝": "淘系店铺", "天猫": "淘系店铺"}.get(display, display))
        if plat == "vip":
            # 父目录给多旬分发（含 9 月下旬 + 10 月上中旬）
            cases["vip"] = dict(platform="vip", shop="VIP00101", s="", e="",
                files=[full], extra={},
                base_pred=lambda n: n.startswith("唯品会_"))
            continue
        for sdir in sorted(os.listdir(full)):
            shop_dir = os.path.join(full, sdir)
            if not os.path.isdir(shop_dir):
                continue
            code = sdir.rsplit("-", 1)[-1]
            if plat == "akc":
                code = "AKC003"  # 目录名 AKC003和04：多店一簿
            extra = {}
            wx = os.path.join(shop_dir, "微信")
            if plat == "taobao" and os.path.isdir(wx):
                extra["wechat"] = [wx]
            s, e = (S, E)
            if plat == "jd":
                s, e = "", ""  # 只给 period，pipeline 自动推上月末~月末
            if plat == "akc":
                pred = lambda n: n.startswith("爱库存_")
            else:
                pred = (lambda c: (lambda n: f"_{c}_" in n))(code)
            cases[f"{plat}:{code}"] = dict(platform=plat, shop=code, s=s, e=e,
                files=[shop_dir], extra=extra, base_pred=pred)
    return cases


def read_book(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb}
    wb.close()
    return out


def num_cols(rows):
    if not rows:
        return {}
    sums = {}
    for j, h in enumerate(rows[0]):
        vals = [r[j] for r in rows[1:] if j < len(r) and isinstance(r[j], (int, float))]
        if vals:
            sums[f"{h}#{j}"] = round(sum(vals), 2)
    return sums


def cmp_sheet(name, got_rows, base_rows, report):
    gh = list(got_rows[0]) if got_rows else []
    bh = list(base_rows[0]) if base_rows else []
    if gh != bh:
        report.append(f"  [HEADER] {name}: got={gh}\n           base={bh}")
    if len(got_rows) != len(base_rows):
        report.append(f"  [ROWS] {name}: got={len(got_rows)-1} base={len(base_rows)-1}")
    gs, bs = num_cols(got_rows), num_cols(base_rows)
    for k in sorted(set(gs) | set(bs)):
        g, b = gs.get(k), bs.get(k)
        if g is None or b is None or abs(g - b) > 0.011:
            report.append(f"  [SUM] {name}.{k}: got={g} base={b}")


def _norm(n):
    stem, ext = os.path.splitext(os.path.basename(n))
    return stem.rstrip("0123456789") + ext


def main():
    cases = build_cases()
    names = sys.argv[1:] or list(cases)
    all_ok = True
    for name in names:
        if name not in cases:
            print(f"== {name} [SKIP] 未知用例 ==", flush=True)
            all_ok = False
            continue
        c = cases[name]
        print(f"== {name} ==", flush=True)
        try:
            res = pipeline.run_bill(platform=c["platform"], shop_code=c["shop"],
                period=P, s_date=c["s"], e_date=c["e"],
                file_paths=c["files"], extra_files=c["extra"])
        except Exception as exc:
            all_ok = False
            print(f"  [EXC] {type(exc).__name__}: {str(exc)[:300]}", flush=True)
            d = getattr(exc, "detail", None)
            if d:
                print("  detail:", str(d)[:500], flush=True)
            continue
        print("  status:", res.status, "|", str(res.message)[:200], flush=True)
        if res.status != "success":
            all_ok = False
            continue
        files = res.data.get("exported_files") or []
        print("  exported:", [os.path.basename(f) for f in files], flush=True)
        print("  batch_dir:", res.data.get("batch_dir"), flush=True)
        baselines = [n for n in sorted(os.listdir(BASE)) if c["base_pred"](n)]
        report = []
        if not baselines:
            report.append("  [NO-BASE] 基准目录无对应文件")
        for bname in baselines:
            match = [f for f in files if _norm(f) == _norm(bname)]
            if not match:
                report.append(f"  [MISSING] 未导出基准同名文件: {bname}")
                continue
            got = read_book(match[0]); base = read_book(os.path.join(BASE, bname))
            if list(got) != list(base):
                report.append(f"  [SHEETS] {bname}: got={list(got)} base={list(base)}")
            for sh in base:
                if sh in got:
                    cmp_sheet(sh, got[sh], base[sh], report)
                else:
                    report.append(f"  [SHEET-MISS] {sh}")
        if report:
            all_ok = False
            print("\n".join(report[:80]), flush=True)
        else:
            print(f"  [OK] {len(baselines)} 份基准逐sheet/列头/行数/数值合计一致", flush=True)
    print("ALL_OK" if all_ok else "HAS_DIFF", flush=True)


if __name__ == "__main__":
    main()
