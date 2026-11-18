# -*- coding: utf-8 -*-
"""T5 差异归因：cost DIFF 店铺 vs SKU 阶段基准交叉比对。

判定逻辑：cost 阶段明细除回填列（采购单价/发货成本/占位符/特殊情况说明）外
与 match 阶段逐格一致（守恒已证）。若 cost 产物的差异在「我方 match 底稿 vs
SKU 阶段基准」中同样存在 → OMS 数据时点漂移继承（白名单）；否则是 cost 逻辑回归。

用法: python scripts/attribute_cost_diff.py [shop_code ...]
"""
import sys, os, math
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
sys.path.insert(0, os.path.join(r"D:\DesktopAgent\extensions\user\recon_workbench", "scripts"))

import openpyxl
import pandas as pd
from core import artifacts
from run_match_oct_all import find_base, num_cols, BASE_DIR

PERIOD = "2026-10"
COST_BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证\成本匹配"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t5_attribute.log"
TOL = 0.011
# cost 阶段有意改变的列（回填/占位符/附加列），不参与继承判定
COST_CHANGED = {"采购单价", "发货成本", "款号", "吊牌价", "商品名称", "年份", "季节",
                "品牌", "品类", "id", "货款金额", "特殊情况说明"}


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def read_book(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb}
    wb.close()
    return out


def sheet_diffs(g, b, allow_extra=()):
    """两 sheet 比对 → diff 标签集合（列头/行数/数值列合计）。"""
    tags = set()
    gh, bh = list(g[0]), list(b[0])
    if gh[:len(bh)] != bh or tuple(gh[len(bh):]) != tuple(allow_extra):
        tags.add("HEADER")
    if len(g) != len(b):
        tags.add("ROWS")
    gs, bs = num_cols(g), num_cols(b)
    for k in sorted(set(gs) | set(bs)):
        if k in COST_CHANGED:
            continue
        gv, bv = gs.get(k), bs.get(k)
        if gv is None or bv is None or abs(gv - bv) > TOL:
            tags.add(f"SUM:{k}")
    return tags


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    want = set(sys.argv[1:])
    batches = artifacts.list_sku_batches(
        PERIOD, states=(artifacts.SKU_STATE_PENDING_COST, artifacts.SKU_STATE_COSTED))
    inherited, unknown = [], []
    for b in batches:
        code = b.shop_code or b.dir.name
        if want and not any(w in code or w in b.dir.name for w in want):
            continue
        files = sorted(str(p) for p in b.dir.glob("*.xlsx") if not p.name.endswith(".cost-tmp.xlsx"))
        if not files:
            continue
        for f in files:
            bn = os.path.basename(f)
            cp = os.path.join(COST_BASE, bn)
            if not os.path.isfile(cp):
                top = [x for x in os.listdir(COST_BASE) if x.lower().endswith(".xlsx")]
                hit = [x for x in top if b.shop_code and b.shop_code in x]
                if not hit and b.platform == "akc":
                    hit = [x for x in top if x.startswith("爱库存_")]
                cp = os.path.join(COST_BASE, hit[0]) if hit else ""
            sp = find_base(bn, b.shop_code, b.platform)
            if not cp or not os.path.isfile(cp) or not sp:
                continue
            cost_book, sku_book = read_book(f), read_book(cp)
            # 我方 match 底稿 → 与 SKU 基准同构的收/退款 sheet（分→元）
            units = artifacts.sku_batch_units(b.dir)
            match_parts = {"收款匹配": [], "退款匹配": []}
            hdr = None
            for u in units:
                m = artifacts.read_match_draft(u["drafts_dir"])
                if m.empty:
                    continue
                for side in ("收款匹配", "退款匹配"):
                    sub = m[m["数据来源"] == side]
                    if sub.empty:
                        continue
                    if hdr is None:
                        hdr = [c for c in sub.columns if c not in ("数据来源", "店铺编码", "成本缺失原因")]
                    rows = []
                    for _, r in sub.iterrows():
                        row = []
                        for c in hdr:
                            v = r.get(c, "")
                            if c in ("货款金额", "货款分摊金额", "结算金额", "标准成本单价", "标准成本", "发货成本", "吊牌价"):
                                try:
                                    fv = float(v)
                                    row.append(None if math.isnan(fv) else fv / 100.0)
                                except (TypeError, ValueError):
                                    row.append(None)
                            elif c == "数量":
                                try:
                                    row.append(int(float(v)))
                                except (TypeError, ValueError):
                                    row.append(0)
                            else:
                                s = "" if v is None else str(v)
                                row.append(None if s.strip().lower() in ("", "nan", "none", "nat") else s)
                        rows.append(tuple(row))
                    match_parts[side].extend(rows)
            verdict = []
            for sh in ("店铺数据透视", "款号数据透视", "匹配透视", "收款匹配", "退款匹配", "已匹配的巨益单号"):
                if sh not in cost_book or sh not in sku_book:
                    continue
                extra = ("特殊情况说明",) if sh in ("收款匹配", "退款匹配") else ()
                cd = sheet_diffs(cost_book[sh], sku_book[sh], extra)
                if not cd:
                    continue
                if sh in ("收款匹配", "退款匹配") and match_parts[sh]:
                    mine = [tuple(hdr)] + match_parts[sh]
                    md = sheet_diffs(mine, sku_book[sh])
                    new = cd - md
                    # HEADER 差异若仅因附加列/占位文本，单独看
                    if new:
                        verdict.append(f"{sh}: cost新增差异 {sorted(new)}")
                    else:
                        verdict.append(f"{sh}: 全部继承SKU阶段漂移({len(cd)}项)")
                else:
                    verdict.append(f"{sh}: 透视/回传差异 {sorted(cd)[:6]}（透视由明细派生，明细继承则同源）")
            has_new = any("cost新增差异" in v for v in verdict)
            tag = "NEW-DIFF" if has_new else ("INHERITED" if verdict else "OK")
            print(f"[{tag}] {code} {bn[:40]}", flush=True)
            for v in verdict[:12]:
                print("   " + v, flush=True)
            (unknown if has_new else inherited).append(code)
    print(f"\nSUMMARY 继承漂移={len(set(inherited))} 店 / 新增差异={len(set(unknown))} 店", flush=True)
    print("NEW_DIFF_SHOPS:", sorted(set(unknown)), flush=True)
    print("ATTR_DONE", flush=True)


if __name__ == "__main__":
    main()
