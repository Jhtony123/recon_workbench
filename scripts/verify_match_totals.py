# -*- coding: utf-8 -*-
"""SKU对账产物三项金额校验（全平台，除vip）：
1) 秒收秒退：是否秒退判断=是 的行，按订单编号 收款货款合计 == 退款货款合计（逐单+总）
2) 同sheet内 货款金额合计 == 货款分摊金额合计（收款/退款分别；空值按0计）
3) 收款sheet货款合计 == 账单整理 bill_goods 收款合计；退款同理（京东三源已在账单层合并，同源应对平）
金额单位：Excel=元；bill_goods=分。容差 0.02 元（浮点累计）。
用法: python scripts/verify_match_totals.py [shop_code ...]
"""
import sys, os, json, math
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
import pandas as pd
from core import artifacts

SKU_ROOT = r"C:\Users\贺杰\Desktop\账单\2026-10\sku匹配\未匹配成本"
COSTED_ROOT = r"C:\Users\贺杰\Desktop\账单\2026-10\sku匹配\已匹配成本"
BILL_ROOT = r"C:\Users\贺杰\Desktop\账单\2026-10\账单整理"
TOL = 0.02


def fsum(vals):
    out = []
    for v in vals:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        s = str(v).strip()
        if s in ("", "nan", "None", "NaT"):
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return math.fsum(out)


def main():
    args = sys.argv[1:]
    costed_only = "--costed" in args   # 只校验成本阶段产物（已匹配成本/）
    want = set(a for a in args if not a.startswith("--"))
    rows = []
    # 2026-11-18：成本匹配启用后批次会迁移到 已匹配成本/，两态目录都扫；
    # 同名批次以 已匹配成本 优先（cost 阶段产物是最新口径）。
    dirs = {}
    roots = [COSTED_ROOT] if costed_only else [SKU_ROOT, COSTED_ROOT]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            dirs[d] = os.path.join(root, d)
    for d, dpath in sorted(dirs.items()):
        code = d.rsplit("-", 1)[-1]
        if want and code not in want:
            continue
        xls = [f for f in os.listdir(dpath) if f.endswith(".xlsx") and not f.endswith(".cost-tmp.xlsx")]
        if not xls:
            continue
        # VIP 多旬一目录多份：全部读入合并（bill_goods 也按旬合并，口径对齐）
        recs, refs = [], []
        for f in sorted(xls):
            p = os.path.join(dpath, f)
            recs.append(pd.read_excel(p, sheet_name="收款匹配"))
            refs.append(pd.read_excel(p, sheet_name="退款匹配"))
        rec = pd.concat(recs, ignore_index=True)
        ref = pd.concat(refs, ignore_index=True)
        issues = []
        # --- 1 秒退逐单相等
        r_yes = rec[rec["是否秒退判断"].astype(str).str.strip() == "是"]
        f_yes = ref[ref["是否秒退判断"].astype(str).str.strip() == "是"]
        g1 = r_yes.groupby(r_yes["订单编号"].astype(str))["货款金额"].apply(fsum)
        g2 = f_yes.groupby(f_yes["订单编号"].astype(str))["货款金额"].apply(fsum)
        bad = []
        for o in sorted(set(g1.index) | set(g2.index)):
            a, b = g1.get(o, 0.0), g2.get(o, 0.0)
            if abs(a - b) > TOL:
                bad.append((str(o), round(a, 2), round(b, 2)))
        t1r, t1f = fsum(r_yes["货款金额"]), fsum(f_yes["货款金额"])
        if bad or abs(t1r - t1f) > TOL:
            issues.append(f"[1秒退] 收={t1r:.2f} 退={t1f:.2f} 不等单数={len(bad)} 例={bad[:3]}")
        # --- 2 货款 vs 分摊
        for name, df in (("收款", rec), ("退款", ref)):
            a, b = fsum(df["货款金额"]), fsum(df["货款分摊金额"])
            if abs(a - b) > TOL:
                issues.append(f"[2分摊] {name}: 货款={a:.2f} 分摊={b:.2f} 差={a-b:.2f}")
        # --- 3 与账单整理对平
        bp = os.path.join(BILL_ROOT, d, "底稿")
        goods = None
        for ext in (".parquet", ".csv"):
            gp = os.path.join(bp, "bill_goods" + ext)
            if os.path.isfile(gp):
                goods = pd.read_parquet(gp) if ext == ".parquet" else pd.read_csv(gp)
                break
        if goods is None:
            # VIP 多旬：底稿按旬子目录隔离，逐旬 bill_goods 合并
            import glob
            parts = []
            for gp in sorted(glob.glob(os.path.join(bp, "*", "bill_goods.*"))):
                parts.append(pd.read_parquet(gp) if gp.endswith(".parquet") else pd.read_csv(gp))
            if parts:
                goods = pd.concat(parts, ignore_index=True)
        if goods is None:
            issues.append("[3账单] 无bill_goods")
        else:
            br = fsum(goods["receipt_fen"]) / 100.0
            bf = fsum(goods["refund_fen"]) / 100.0
            mr, mf = fsum(rec["货款金额"]), fsum(ref["货款金额"])
            if abs(mr - br) > TOL:
                issues.append(f"[3账单] 收款: 匹配={mr:.2f} 账单={br:.2f} 差={mr-br:.2f}")
            if abs(mf - bf) > TOL:
                issues.append(f"[3账单] 退款: 匹配={mf:.2f} 账单={bf:.2f} 差={mf-bf:.2f}")
        tag = "OK" if not issues else "FAIL"
        rows.append((code, tag, len(r_yes), len(f_yes), issues))
        print(f"{tag:4s} {code:8s} 秒退收{len(r_yes)}/退{len(f_yes)}行" +
              ("" if not issues else " | " + " ; ".join(issues)), flush=True)
    n_ok = sum(1 for r in rows if r[1] == "OK")
    print(f"\nSUMMARY OK={n_ok}/{len(rows)}", flush=True)
    print("VERIFY_DONE", flush=True)


if __name__ == "__main__":
    main()
