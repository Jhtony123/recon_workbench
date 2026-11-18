# -*- coding: utf-8 -*-
"""2026-10 全平台（除唯品会）SKU对账批量重跑 + 影刀基准一致性比对。

批次参数取自 账单整理/底稿/manifest.json（platform/shop_code/shop_name/period/s/e）。
基准: 桌面\\2026.10月账单\\SKU匹配汇总-已有结果可作为对照验证 顶层同名文件。
白名单（已人工确认的预期差异）:
  · 各透视「冲发出商品数量」——影刀源码笔误修正（数量被金额覆盖）
  · 订单日期格式 / AKC 品牌字典漂移（文本列，不影响数值合计）
用法: python scripts/run_match_oct_all.py [platform:shop ...]  不带参数=全量(除vip)
"""
import sys, os, json, time, traceback
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

import openpyxl
from core import pipeline, artifacts

BILL_ROOT = r"C:\Users\贺杰\Desktop\账单\2026-10\账单整理"
BASE_DIR = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证"
WHITELIST_SUM_COLS = ("冲发出商品数量",)


def read_book(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb}
    wb.close()
    return out


def num_cols(rows):
    sums = {}
    for j, h in enumerate(rows[0]):
        vals = []
        for r in rows[1:]:
            v = r[j] if j < len(r) else None
            if v is None or v == "":
                continue
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        if vals:
            sums[str(h)] = round(sum(vals), 2)
    return sums


def cmp_book(got_path, base_path):
    got, base = read_book(got_path), read_book(base_path)
    report, white = [], []
    if list(got) != list(base):
        report.append(f"[SHEETS] got={list(got)} base={list(base)}")
    for sh in base:
        if sh not in got:
            report.append(f"[SHEET-MISS] {sh}")
            continue
        g, b = got[sh], base[sh]
        if list(g[0]) != list(b[0]):
            report.append(f"[HEADER] {sh}")
        if len(g) != len(b):
            report.append(f"[ROWS] {sh}: got={len(g)-1} base={len(b)-1}")
        gs, bs = num_cols(g), num_cols(b)
        for k in sorted(set(gs) | set(bs)):
            gv, bv = gs.get(k), bs.get(k)
            if gv is None or bv is None or abs(gv - bv) > 0.011:
                line = f"[SUM] {sh}.{k}: got={gv} base={bv}"
                (white if k in WHITELIST_SUM_COLS else report).append(line)
    return report, white


def _norm(n):
    stem, ext = os.path.splitext(os.path.basename(n))
    return stem.rstrip("0123456789") + ext


def find_base(bn, shop_code, platform):
    """基准定位：顶层同名 → 顶层去尾数字同名 → 顶层含店铺编码 → 递归含编码。"""
    top = [x for x in os.listdir(BASE_DIR) if x.lower().endswith(".xlsx")]
    if bn in top:
        return os.path.join(BASE_DIR, bn)
    cand = [x for x in top if _norm(x) == _norm(bn)]
    if cand:
        return os.path.join(BASE_DIR, cand[0])
    if shop_code:
        cand = [x for x in top if shop_code in x]
        if cand:
            return os.path.join(BASE_DIR, cand[0])
    for root, _dirs, files in os.walk(BASE_DIR):
        if os.path.abspath(root) == os.path.abspath(BASE_DIR):
            continue
        for x in files:
            if shop_code and shop_code in x and x.lower().endswith(".xlsx"):
                return os.path.join(root, x)
    if platform == "akc":
        cand = [x for x in top if x.startswith("爱库存_")]
        if cand:
            return os.path.join(BASE_DIR, cand[0])
    return ""


def main():
    cases = []
    for d in sorted(os.listdir(BILL_ROOT)):
        mp = os.path.join(BILL_ROOT, d, "底稿", "manifest.json")
        if not os.path.isfile(mp):
            continue
        man = json.load(open(mp, encoding="utf-8"))
        plat = man.get("platform") or ""
        if plat == "vip":
            continue
        cases.append(man)
    want = sys.argv[1:]
    if want:
        cases = [m for m in cases if f"{m['platform']}:{m['shop_code']}" in want]
    print(f"共 {len(cases)} 个批次（除vip）", flush=True)
    summary = []
    for man in cases:
        key = f"{man['platform']}:{man['shop_code']}"
        t0 = time.time()
        print(f"\n===== {key} {man.get('shop_name','')}", flush=True)
        try:
            res = pipeline.run_match(
                platform=man["platform"], shop_code=man["shop_code"],
                shop_name=man.get("shop_name") or "", period=man["period"],
                s_date=man.get("s_date") or "", e_date=man.get("e_date") or "")
        except Exception as e:
            print(f"[EXC] {type(e).__name__}: {str(e)[:200]}", flush=True)
            summary.append((key, "EXC", str(e)[:120], []))
            continue
        data = res.data or {}
        print(f"status={res.status} | {res.message} | {round(time.time()-t0,1)}s", flush=True)
        if res.status == "skipped":
            summary.append((key, "skipped", res.message, []))
            continue
        diffs_all, whites_all = [], []
        for f in data.get("exported_files") or []:
            bn = os.path.basename(f)
            bp = find_base(bn, man["shop_code"], man["platform"])
            if not bp:
                diffs_all.append(f"[NO-BASE] {bn}")
                continue
            rep, wht = cmp_book(f, bp)
            diffs_all += [f"{bn}: {r}" for r in rep]
            whites_all += wht
        tag = "OK" if not diffs_all else "DIFF"
        print(f"  [{tag}] 白名单差异 {len(whites_all)} 项", flush=True)
        for r in diffs_all[:20]:
            print("  " + r, flush=True)
        summary.append((key, tag, res.message, diffs_all))
    print("\n================ SUMMARY ================", flush=True)
    n_ok = sum(1 for s in summary if s[1] == "OK")
    for key, tag, msg, diffs in summary:
        print(f"{tag:7s} {key:20s} {msg}", flush=True)
    print(f"OK={n_ok} / {len(summary)}", flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
