# -*- coding: utf-8 -*-
"""差异归因：对 DIFF 店铺，按(订单编号,单号,sku,数据来源)键比 收款/退款匹配 行集合，
分类：仅我方多 / 仅基准多 / 双方都有但值不同；并看差异行的 巨益单据匹配状态/处理意见/存货异动时间 分布。
目的：判定差异是 OMS/字典数据漂移（行集合或属性随时间变化）还是代码逻辑回归。
用法: python scripts/diagnose_match_diff.py [shop_code ...]  默认几个代表店
"""
import sys, os, json
import openpyxl

SKU_ROOT = r"C:\Users\贺杰\Desktop\账单\2026-10\sku匹配\未匹配成本"
BASE_DIR = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证"
DEFAULT = ["DY009", "PDD002", "SPH003", "TM003", "YZ002", "TB003", "SHP004", "PDD003"]


def find_files(code):
    got = base = None
    for d in os.listdir(SKU_ROOT):
        if code in d:
            for f in os.listdir(os.path.join(SKU_ROOT, d)):
                if f.endswith(".xlsx"):
                    got = os.path.join(SKU_ROOT, d, f)
    for root, _dd, files in os.walk(BASE_DIR):
        for f in files:
            if code in f and f.endswith(".xlsx") and "成本匹配" not in root:
                base = os.path.join(root, f)
    return got, base


def rows_of(path, sheet):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    rows = [list(r) for r in it]
    wb.close()
    return hdr, rows


def key_of(hdr, r):
    def g(c):
        return str(r[hdr.index(c)]) if c in hdr and hdr.index(c) < len(r) else ""
    return (g("订单编号"), g("单号"), g("sku"), g("数据来源") if "数据来源" in hdr else g("单据类型"))


def main():
    codes = sys.argv[1:] or DEFAULT
    for code in codes:
        got, base = find_files(code)
        print(f"\n########## {code} got={bool(got)} base={bool(base)}", flush=True)
        if not got or not base:
            continue
        for sheet in ["收款匹配", "退款匹配"]:
            gh, gr = rows_of(got, sheet)
            bh, br = rows_of(base, sheet)
            # 基准无数据来源列：用 单据类型 方向近似（收款sheet全收款）——键去掉方向
            def k(h, r):
                t = key_of(h, r)
                return t[:3]
            gmap, bmap = {}, {}
            for r in gr:
                gmap.setdefault(k(gh, r), []).append(r)
            for r in br:
                bmap.setdefault(k(bh, r), []).append(r)
            only_g = [kk for kk in gmap if kk not in bmap]
            only_b = [kk for kk in bmap if kk not in gmap]
            both_diff = []
            for kk in gmap:
                if kk in bmap and len(gmap[kk]) == len(bmap[kk]) == 1:
                    rg, rb = gmap[kk][0], bmap[kk][0]
                    diffs = []
                    for c in ["结算金额", "数量", "标准成本", "吊牌价", "年份", "季节", "品牌", "巨益单据匹配状态", "处理意见", "是否秒退判断"]:
                        if c in gh and c in bh:
                            vg, vb = rg[gh.index(c)], rb[bh.index(c)]
                            if str(vg) != str(vb):
                                try:
                                    if abs(float(vg) - float(vb)) < 0.011:
                                        continue
                                except (TypeError, ValueError):
                                    pass
                                diffs.append(c)
                    if diffs:
                        both_diff.append((kk, diffs, rg, rb))
            print(f"== {sheet}: got={len(gr)} base={len(br)} 仅我方={len(only_g)} 仅基准={len(only_b)} 同键值不同={len(both_diff)}", flush=True)
            for kk in only_g[:3]:
                r = gmap[kk][0]
                st = r[gh.index("巨益单据匹配状态")] if "巨益单据匹配状态" in gh else ""
                op = r[gh.index("处理意见")] if "处理意见" in gh else ""
                tm = r[gh.index("存货异动时间")] if "存货异动时间" in gh else ""
                print(f"   +G {kk} 状态={st} 意见={op} 异动={tm}", flush=True)
            for kk in only_b[:3]:
                r = bmap[kk][0]
                st = r[bh.index("巨益单据匹配状态")] if "巨益单据匹配状态" in bh else ""
                op = r[bh.index("处理意见")] if "处理意见" in bh else ""
                tm = r[bh.index("存货异动时间")] if "存货异动时间" in bh else ""
                print(f"   +B {kk} 状态={st} 意见={op} 异动={tm}", flush=True)
            from collections import Counter
            cc = Counter()
            for kk, diffs, rg, rb in both_diff:
                cc[tuple(diffs)] += 1
            for combo, n in cc.most_common(6):
                print(f"   ~值不同 {n} 行: {combo}", flush=True)
            for kk, diffs, rg, rb in both_diff[:2]:
                print(f"     例 {kk}: " + "; ".join(
                    f"{c}: got={rg[gh.index(c)]} base={rb[bh.index(c)]}" for c in diffs[:6]), flush=True)
    print("DIAG_DONE", flush=True)


if __name__ == "__main__":
    main()
