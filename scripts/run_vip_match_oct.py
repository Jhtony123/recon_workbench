# -*- coding: utf-8 -*-
"""T1 补跑上游：唯品会 10 月多旬 SKU 对账（B2B 分支，含 9 月下旬跨期旬）+ AKC 产物复核。

VIP：run_match 默认全旬（账单整理批次目录内 3 份旬文件），产物落 未匹配成本/，不清目录。
AKC：已有产物只复核不重跑（最小必要重跑集）。
比对基准：SKU匹配汇总 顶层 唯品会_* / 爱库存_* 账单匹配结果.xlsx。
白名单：冲发出商品数量（影刀笔误修正）、AKC 品牌字典漂移 QS→FR（文本列）。
用法: python scripts/run_vip_match_oct.py
"""
import sys, os, time, traceback
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
sys.path.insert(0, os.path.join(r"D:\DesktopAgent\extensions\user\recon_workbench", "scripts"))

from core import pipeline, artifacts
from run_match_oct_all import cmp_book, find_base, BASE_DIR

PERIOD = "2026-10"


def compare_files(files, shop_code, platform, tag_prefix=""):
    diffs_all, whites_all = [], []
    for f in files:
        bn = os.path.basename(f)
        bp = find_base(bn, shop_code, platform)
        if not bp:
            diffs_all.append(f"[NO-BASE] {bn}")
            continue
        rep, wht = cmp_book(f, bp)
        diffs_all += [f"{bn}: {r}" for r in rep]
        whites_all += [f"{bn}: {w}" for w in wht]
    tag = "OK" if not diffs_all else "DIFF"
    print(f"  [{tag_prefix}{tag}] 白名单差异 {len(whites_all)} 项", flush=True)
    for r in diffs_all[:30]:
        print("  " + r, flush=True)
    for w in whites_all[:10]:
        print("  [WHITE] " + w, flush=True)
    return tag


def main():
    summary = []
    # ---------- VIP 全旬 ----------
    print("===== vip 多旬 SKU 对账 =====", flush=True)
    t0 = time.time()
    try:
        res = pipeline.run_match(platform="vip", shop_code="VIP00101",
                                 shop_name="唯品中国", period=PERIOD)
        data = res.data or {}
        print(f"status={res.status} | {res.message} | {round(time.time()-t0,1)}s", flush=True)
        files = data.get("exported_files") or []
        print(f"exported {len(files)} 份:", flush=True)
        for f in files:
            print("  " + os.path.basename(f), flush=True)
        tag = compare_files(files, "VIP00101", "vip", "VIP-")
        summary.append(("vip", tag, res.message))
    except Exception as e:
        traceback.print_exc()
        summary.append(("vip", "EXC", str(e)[:200]))

    # ---------- AKC 复核（不重跑） ----------
    print("\n===== akc 已有产物复核 =====", flush=True)
    akc_dir = artifacts.sku_root(PERIOD) / "2026-10-爱库存"
    files = sorted(str(p) for p in akc_dir.glob("*.xlsx"))
    if not files:
        print("  [MISS] 未匹配成本/2026-10-爱库存 无产物", flush=True)
        summary.append(("akc", "MISS", "无产物"))
    else:
        for f in files:
            print("  " + os.path.basename(f), flush=True)
        tag = compare_files(files, "", "akc", "AKC-")
        summary.append(("akc", tag, f"{len(files)} 份复核"))

    print("\n================ SUMMARY ================", flush=True)
    for key, tag, msg in summary:
        print(f"{tag:8s} {key:6s} {msg}", flush=True)
    ok = all(t == "OK" for _, t, _ in summary)
    print("ALL_OK" if ok else "HAS_DIFF", flush=True)


if __name__ == "__main__":
    main()
