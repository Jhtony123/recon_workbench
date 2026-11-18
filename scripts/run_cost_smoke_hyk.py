# -*- coding: utf-8 -*-
"""T2 验收：HYK 单店成本匹配冒烟 + 影刀成本基准逐 sheet 比对。

锚点（spec 验收）：毛利 21499.84、id 空白 47 行、采购单价=表值(1e-6)、
发货成本=round(|数量|×原始单价,2)、守恒校验、6-sheet 落盘、明细 32 列。
用法: python scripts/run_cost_smoke_hyk.py
"""
import sys, os, time
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
sys.path.insert(0, os.path.join(r"D:\DesktopAgent\extensions\user\recon_workbench", "scripts"))

import openpyxl
import pandas as pd
from core import pipeline, cost, artifacts

COST_FILE = r"C:\Users\贺杰\Desktop\2026.10月账单\物料成本_202610.xlsx"
BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证\成本匹配\好衣库_好衣库_秋水伊人_HYK001_2026年10月-2026年10月_账单匹配结果.xlsx"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t2_smoke_hyk.log"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


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


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)

    t0 = time.time()
    res = pipeline.run_cost(period="2026-10", cost_file=COST_FILE,
                            bill_batch_id="2026-10-好衣库-好衣库_秋水伊人-HYK001")
    print(f"status={res.status} | {res.message} | {round(time.time()-t0,1)}s", flush=True)
    data = res.data or {}
    files = data.get("exported_files") or []
    print("exported:", [os.path.basename(f) for f in files], flush=True)
    print("migrated:", data.get("migrated"), flush=True)
    if res.status == "failed" or not files:
        print("SMOKE_FAIL", flush=True)
        return
    got_path = files[0]

    # ---- 逐 sheet 比对（明细允许尾部附加「特殊情况说明」列）----
    got, base = read_book(got_path), read_book(BASE)
    report = []
    if list(got) != list(base):
        report.append(f"[SHEETS] got={list(got)} base={list(base)}")
    for sh in base:
        g, b = got.get(sh), base[sh]
        if g is None:
            report.append(f"[SHEET-MISS] {sh}"); continue
        gh, bh = list(g[0]), list(b[0])
        if sh in ("收款匹配", "退款匹配"):
            if gh[:len(bh)] != bh or gh[len(bh):] != ["特殊情况说明"]:
                report.append(f"[HEADER] {sh}: got={gh}")
        elif gh != bh:
            report.append(f"[HEADER] {sh}: got={gh} base={bh}")
        if len(g) != len(b):
            report.append(f"[ROWS] {sh}: got={len(g)-1} base={len(b)-1}")
        gs, bs = num_cols(g), num_cols(b)
        for k in sorted(set(gs) | set(bs)):
            gv, bv = gs.get(k), bs.get(k)
            if gv is None or bv is None or abs(gv - bv) > 0.011:
                report.append(f"[SUM] {sh}.{k}: got={gv} base={bv}")
    for r in report[:40]:
        print(r, flush=True)

    # ---- 明细逐行锚点 ----
    cmap = cost.load_cost_base(COST_FILE, mode=cost.MODE_STYLE, sheet_name="10月末")
    anchors_ok = True
    for sh in ("收款匹配", "退款匹配"):
        rows = got[sh]
        hdr = list(rows[0])
        df = pd.DataFrame(rows[1:], columns=hdr)
        # id 空白行数
        id_blank = int(df["id"].isna().sum())
        # 采购单价=表值（1e-6）；发货成本公式
        bad_price = bad_ship = 0
        for _, r in df.iterrows():
            k = str(r["款号"] or "")
            qty = abs(int(r["数量"] or 0))
            pu = float(r["采购单价"] or 0)
            ship = float(r["发货成本"] or 0)
            hit = cmap.get(k)
            if k in ("无款号", ""):
                expect = 0.0
            elif hit is None:
                expect = 0.0
            else:
                expect = float(hit["unit_cost_raw"])
            if abs(pu - expect) > 1e-6:
                bad_price += 1
            from decimal import Decimal, ROUND_HALF_UP
            exp_ship = float((Decimal(str(expect)) * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if abs(ship - exp_ship) > 0.011:
                bad_ship += 1
        print(f"[{sh}] id空白={id_blank} 采购单价!=表值 {bad_price} 行 发货成本公式不符 {bad_ship} 行", flush=True)
        if bad_price or bad_ship:
            anchors_ok = False
    # 毛利锚点
    shop_rows = got["店铺数据透视"]
    hdr = list(shop_rows[0])
    gi = hdr.index("账单结算口径毛利")
    carry = [r for r in shop_rows[1:] if r[hdr.index("处理意见")] == "结转成本"]
    gross = float(carry[0][gi]) if carry else None
    print(f"[锚点] 结转毛利={gross}（期望 21499.84）", flush=True)
    if gross is None or abs(gross - 21499.84) > 0.011:
        anchors_ok = False

    ok = not report and anchors_ok
    print("SMOKE_OK" if ok else "SMOKE_DIFF", flush=True)


if __name__ == "__main__":
    main()
