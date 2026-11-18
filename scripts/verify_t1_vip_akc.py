# -*- coding: utf-8 -*-
"""T1 比对（只比对不重跑）：VIP 三旬 + AKC 产物 vs 影刀 SKU 阶段基准。

run_vip_match_oct.py 的 run_match 部分已完成（三旬 manifest=success），
本脚本补跑被预算中断的比对段。白名单：冲发出商品数量（影刀笔误修正）。
用法: python scripts/verify_t1_vip_akc.py [vip|akc]
"""
import sys, os, time
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
sys.path.insert(0, os.path.join(r"D:\DesktopAgent\extensions\user\recon_workbench", "scripts"))

from core import artifacts
from run_match_oct_all import cmp_book, find_base

PERIOD = "2026-10"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t1_verify.log"


class _Tee:
    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, s):
        self.stream.write(s)
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        self.stream.flush()
        self.fh.flush()


def compare_files(files, shop_code, platform, tag_prefix=""):
    diffs_all, whites_all = [], []
    for f in files:
        bn = os.path.basename(f)
        bp = find_base(bn, shop_code, platform)
        if not bp:
            diffs_all.append(f"[NO-BASE] {bn}")
            continue
        t0 = time.time()
        rep, wht = cmp_book(f, bp)
        diffs_all += [f"{bn}: {r}" for r in rep]
        whites_all += [f"{bn}: {w}" for w in wht]
        print(f"  比对 {bn} 用时 {round(time.time()-t0,1)}s", flush=True)
    tag = "OK" if not diffs_all else "DIFF"
    print(f"  [{tag_prefix}{tag}] 白名单差异 {len(whites_all)} 项", flush=True)
    for r in diffs_all[:40]:
        print("  " + r, flush=True)
    for w in whites_all[:10]:
        print("  [WHITE] " + w, flush=True)
    return tag


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    which = sys.argv[1:] or ["vip", "akc"]
    summary = []
    root = artifacts.sku_root(PERIOD)
    if "vip" in which:
        print("===== vip 三旬产物 vs 基准 =====", flush=True)
        vdir = root / "2026-10-唯品会-唯品中国"
        files = sorted(str(p) for p in vdir.glob("*.xlsx"))
        print(f"{len(files)} 份", flush=True)
        tag = compare_files(files, "VIP00101", "vip", "VIP-")
        summary.append(("vip", tag))
    if "akc" in which:
        print("\n===== akc 产物复核 =====", flush=True)
        adir = root / "2026-10-爱库存"
        files = sorted(str(p) for p in adir.glob("*.xlsx"))
        print(f"{len(files)} 份", flush=True)
        tag = compare_files(files, "", "akc", "AKC-")
        summary.append(("akc", tag))
    print("\n================ SUMMARY ================", flush=True)
    for key, tag in summary:
        print(f"{tag:8s} {key}", flush=True)
    print("ALL_OK" if all(t == "OK" for _, t in summary) else "HAS_DIFF", flush=True)


if __name__ == "__main__":
    main()
