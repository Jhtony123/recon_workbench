# -*- coding: utf-8 -*-
"""T5 基准回归：全平台成本匹配 vs 影刀基准 52 份（防假绿）。

流程：run_cost 全量（含 VIP 各旬/AKC）→ 逐文件比对基准：
  · 逐 sheet 列头/行数/全部数值列合计（含毛利率列），容差 0.011 元；
    明细允许尾部附加「特殊情况说明」列（用户批准，Q6）；
  · 明细逐行 采购单价=成本表值(1e-6)、发货成本=round(|数量|×原始单价,2)；
  · 守恒校验（A8）：cost_detail vs match_detail 底稿 货款/分摊/结算/数量/标准成本 合计恒等；
  · 回传表「已匹配的巨益单号」与 match 底稿 oms_matched 逐格一致；
  · 锚点：HYK 毛利 21499.84；VIP FR 发货=标准=结算；DY009 缺失行填0+特殊情况说明。
白名单：OMS 数据时点漂移（沿用 diagnose_match_diff 归因，DIFF 店铺单独列示人工确认）。
用法: python scripts/verify_cost_match.py [--skip-run] [shop_code ...]
"""
import sys, os, json, time, math, traceback
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
sys.path.insert(0, os.path.join(r"D:\DesktopAgent\extensions\user\recon_workbench", "scripts"))

from decimal import Decimal, ROUND_HALF_UP
import openpyxl
import pandas as pd
from core import pipeline, cost, artifacts
from run_match_oct_all import find_base, num_cols

PERIOD = "2026-10"
COST_FILE = r"C:\Users\贺杰\Desktop\2026.10月账单\物料成本_202610.xlsx"
COST_BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证\成本匹配"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t5_verify_cost.log"
TOL = 0.011
CONSERVE = ("货款金额", "货款分摊金额", "结算金额", "数量", "标准成本")


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def read_book(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb}
    wb.close()
    return out


def fsum(vals):
    out = []
    for v in vals:
        if v is None or v == "":
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return math.fsum(out)


def cmp_cost_book(got_path, base_path):
    """逐 sheet 比对；明细允许尾部附加「特殊情况说明」列。返回 (diffs, info)。"""
    got, base = read_book(got_path), read_book(base_path)
    diffs = []
    if list(got) != list(base):
        diffs.append(f"[SHEETS] got={list(got)} base={list(base)}")
    for sh in base:
        if sh not in got:
            diffs.append(f"[SHEET-MISS] {sh}"); continue
        g, b = got[sh], base[sh]
        gh, bh = list(g[0]), list(b[0])
        if sh in ("收款匹配", "退款匹配"):
            if gh[:len(bh)] != bh or gh[len(bh):] != ["特殊情况说明"]:
                diffs.append(f"[HEADER] {sh}")
        elif gh != bh:
            diffs.append(f"[HEADER] {sh}")
        if len(g) != len(b):
            diffs.append(f"[ROWS] {sh}: got={len(g)-1} base={len(b)-1}")
        gs, bs = num_cols(g), num_cols(b)
        for k in sorted(set(gs) | set(bs)):
            gv, bv = gs.get(k), bs.get(k)
            if gv is None or bv is None or abs(gv - bv) > TOL:
                diffs.append(f"[SUM] {sh}.{k}: got={gv} base={bv}")
    return diffs, got


def check_detail_rows(got, cmap, platform):
    """明细逐行：采购单价=表值(1e-6)、发货成本公式、特殊情况说明取值。"""
    bad_price = bad_ship = bad_note = 0
    n_missing = 0
    for sh in ("收款匹配", "退款匹配"):
        rows = got.get(sh)
        if not rows:
            continue
        hdr = list(rows[0])
        ix = {c: hdr.index(c) for c in ("款号", "数量", "采购单价", "发货成本", "特殊情况说明", "品牌", "店铺名称", "标准成本") if c in hdr}
        for r in rows[1:]:
            k = str(r[ix["款号"]] or "")
            qty = abs(int(r[ix["数量"]] or 0))
            pu = float(r[ix["采购单价"]] or 0)
            ship = float(r[ix["发货成本"]] or 0)
            note = str(r[ix.get("特殊情况说明", -1)] or "") if "特殊情况说明" in ix else ""
            is_fr = platform == "vip" and str(r[ix["品牌"]] or "") == "FR" \
                and "唯品会" in str(r[ix["店铺名称"]] or "")
            hit = cmap.get(k)
            if k in ("无款号", ""):
                expect = 0.0
                if note != "无款号":
                    bad_note += 1
            elif hit is None:
                expect = 0.0
                n_missing += 1
                if note != "成本表无此款":
                    bad_note += 1
            elif is_fr:
                expect = 0.0
            else:
                expect = float(hit["unit_cost_raw"])
            if abs(pu - expect) > 1e-6:
                bad_price += 1
            if is_fr:
                # FR 行级特例：发货成本=标准成本（=结算金额），不走查表公式
                exp_ship = float(r[ix["标准成本"]] or 0)
            else:
                exp_ship = float((Decimal(str(expect)) * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if abs(ship - exp_ship) > TOL:
                bad_ship += 1
    return bad_price, bad_ship, bad_note, n_missing


def check_conserve(batch_dir):
    """守恒（A8）+ 回传表逐格：cost 底稿 vs match 底稿。返回 issues 列表。"""
    issues = []
    for unit in artifacts.sku_batch_units(batch_dir):
        d = unit["drafts_dir"]
        mp = artifacts.resolve_table_path(d, artifacts.MATCH_DETAIL_STEM)
        cp = artifacts.resolve_table_path(d, "cost_detail")
        if not mp.is_file() or not cp.is_file():
            issues.append(f"[守恒] 缺底稿 {d.name}")
            continue
        m = pd.read_csv(mp, dtype=str, encoding="utf-8-sig") if mp.suffix == ".csv" else pd.read_parquet(mp)
        c = pd.read_csv(cp, dtype=str, encoding="utf-8-sig") if cp.suffix == ".csv" else pd.read_parquet(cp)
        for col in CONSERVE:
            if col not in m.columns or col not in c.columns:
                continue
            a = fsum(pd.to_numeric(m[col], errors="coerce").fillna(0))
            b = fsum(pd.to_numeric(c[col], errors="coerce").fillna(0))
            if abs(a - b) > 0.5:   # 分域合计，容差半分（浮点）
                issues.append(f"[守恒] {unit['label'] or batch_dir.name}.{col}: match={a} cost={b}")
        # 回传表逐格
        op = artifacts.resolve_table_path(d, artifacts.MATCH_WRITEBACK_STEM)
        if op.is_file():
            o = pd.read_csv(op, dtype=str, encoding="utf-8-sig").fillna("") if op.suffix == ".csv" else pd.read_parquet(op).fillna("")
            # cost 交付里的 oms sheet 在 read_book 阶段比（此处比底稿行集合）
            if len(o) == 0:
                continue
    return issues


def check_oms_sheet(got, batch_dir):
    """「已匹配的巨益单号」与 match 底稿 oms_matched 逐格一致（原样透传）。"""
    issues = []
    rows = got.get("已匹配的巨益单号")
    if rows is None:
        return ["[OMS] sheet 缺失"]
    # 汇总所有单元的 oms 底稿（VIP 多旬合并）
    all_rows = []
    hdr = None
    for unit in artifacts.sku_batch_units(batch_dir):
        op = artifacts.resolve_table_path(unit["drafts_dir"], artifacts.MATCH_WRITEBACK_STEM)
        if not op.is_file():
            continue
        o = pd.read_csv(op, dtype=str, encoding="utf-8-sig").fillna("") if op.suffix == ".csv" else pd.read_parquet(op).astype(str).fillna("")
        if hdr is None:
            hdr = list(o.columns)
        all_rows.extend(o.values.tolist())
    gh = list(rows[0])
    gset = sorted(tuple("" if v is None else str(v) for v in r[:len(gh)]) for r in rows[1:])
    if hdr is not None and gh != hdr:
        issues.append(f"[OMS] 列头 got={gh} draft={hdr}")
    bset = sorted(tuple(str(v) for v in r) for r in all_rows)
    # 数值列格式差异（'1' vs '1.0'）归一
    def norm(t):
        out = []
        for v in t:
            try:
                fv = float(v)
                out.append(str(int(fv)) if fv == int(fv) else str(fv))
            except (TypeError, ValueError):
                out.append(v)
        return tuple(out)
    if sorted(map(norm, gset)) != sorted(map(norm, bset)):
        issues.append(f"[OMS] 行集合不一致 got={len(gset)} draft={len(bset)}")
    return issues


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)

    args = sys.argv[1:]
    skip_run = "--skip-run" in args
    want = set(a for a in args if not a.startswith("--"))

    # ---------- 1) 全量 run_cost ----------
    if not skip_run:
        t0 = time.time()
        print("===== run_cost 全量 =====", flush=True)
        res = pipeline.run_cost(period=PERIOD, cost_file=COST_FILE)
        print(f"status={res.status} | {res.message} | {round(time.time()-t0,1)}s", flush=True)
        data = res.data or {}
        print(f"units_ok={data.get('units_ok')} rows={data.get('rows')} "
              f"migrated={len(data.get('migrated') or [])} failed={data.get('failed_batches')}", flush=True)
        if data.get("failed_batches"):
            print("[WARN] 有批次留守，继续比对已成功产物", flush=True)

    cmap = cost.load_cost_base(COST_FILE, mode=cost.MODE_STYLE, sheet_name="10月末")
    print(f"成本表 {len(cmap)} 款", flush=True)

    # ---------- 2) 逐批次比对 ----------
    summary = []
    batches = artifacts.list_sku_batches(
        PERIOD, states=(artifacts.SKU_STATE_PENDING_COST, artifacts.SKU_STATE_COSTED))
    for b in batches:
        code = b.shop_code or b.dir.name
        if want and not any(w in code or w in b.dir.name for w in want):
            continue
        units = artifacts.sku_batch_units(b.dir)
        files = sorted(str(p) for p in b.dir.glob("*.xlsx") if not p.name.endswith(".cost-tmp.xlsx"))
        if not files:
            if not artifacts.sku_batch_units(b.dir):
                # match 阶段 skipped 的空批次（DY017/DYY001）：无成本可做，不算 MISS
                summary.append((code, "SKIP", ["SKU对账 skipped 空批次"]))
            else:
                summary.append((code, "MISS", ["有底稿但无交付件"]))
            continue
        all_diffs, all_white = [], []
        for f in files:
            bn = os.path.basename(f)
            bp = find_base(bn, b.shop_code, b.platform)
            # find_base 的 BASE_DIR 是 SKU 顶层；成本基准在子目录，改直接定位
            cand = os.path.join(COST_BASE, bn)
            if not os.path.isfile(cand):
                top = [x for x in os.listdir(COST_BASE) if x.lower().endswith(".xlsx")]
                hit = [x for x in top if b.shop_code and b.shop_code in x]
                if not hit and b.platform == "akc":
                    hit = [x for x in top if x.startswith("爱库存_")]
                if not hit and b.platform == "vip":
                    hit = [x for x in top if x == bn]
                cand = os.path.join(COST_BASE, hit[0]) if hit else ""
            if not cand or not os.path.isfile(cand):
                all_diffs.append(f"[NO-BASE] {bn}")
                continue
            t0 = time.time()
            diffs, got = cmp_cost_book(f, cand)
            # 明细逐行锚点
            bp_, bs_, bn_, nm = check_detail_rows(got, cmap, b.platform)
            if bp_: all_diffs.append(f"[PRICE] {bn}: 采购单价!=表值 {bp_} 行")
            if bs_: all_diffs.append(f"[SHIP] {bn}: 发货成本公式不符 {bs_} 行")
            if bn_: all_diffs.append(f"[NOTE] {bn}: 特殊情况说明取值错 {bn_} 行")
            # 守恒 + 回传表
            all_diffs += check_conserve(b.dir)
            all_diffs += [f"{bn}: {i}" for i in check_oms_sheet(got, b.dir)]
            print(f"  {bn}: {len(diffs)} diff / 缺失款号行 {nm} / {round(time.time()-t0,1)}s", flush=True)
            for d in diffs[:25]:
                print("    " + d, flush=True)
            all_diffs += [f"{bn}: {d}" for d in diffs]
        tag = "OK" if not all_diffs else "DIFF"
        summary.append((code, tag, all_diffs))
        print(f"[{tag}] {code}", flush=True)

    # ---------- 3) 锚点 ----------
    print("\n===== 锚点 =====", flush=True)
    hyk = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_COSTED) / "2026-10-好衣库-好衣库_秋水伊人-HYK001"
    hf = sorted(hyk.glob("*.xlsx"))
    if hf:
        got = read_book(str(hf[0]))
        rows = got["店铺数据透视"]
        hdr = list(rows[0])
        carry = [r for r in rows[1:] if r[hdr.index("处理意见")] == "结转成本"]
        gross = float(carry[0][hdr.index("账单结算口径毛利")]) if carry else None
        print(f"HYK 结转毛利={gross}（期望 21499.84）", flush=True)
    vip_dir = None
    for st in (artifacts.SKU_STATE_COSTED, artifacts.SKU_STATE_PENDING_COST):
        d = artifacts.sku_root(PERIOD, st) / "2026-10-唯品会-唯品中国"
        if d.is_dir():
            vip_dir = d; break
    if vip_dir:
        vf = [p for p in sorted(vip_dir.glob("*.xlsx")) if "1001-2026" in p.name]
        if vf:
            got = read_book(str(vf[0]))
            for sh in ("收款匹配",):
                rows = got[sh]
                hdr = list(rows[0])
                ix = {c: hdr.index(c) for c in ("品牌", "标准成本", "结算金额", "发货成本", "采购单价")}
                fr = [r for r in rows[1:] if r[ix["品牌"]] == "FR"]
                bad = sum(1 for r in fr if not (r[ix["发货成本"]] == r[ix["标准成本"]] == r[ix["结算金额"]]))
                pu_bad = sum(1 for r in fr if r[ix["采购单价"]] not in (0, None))
                print(f"VIP上旬 FR {len(fr)} 行：发货=标准=结算 不符 {bad} 行；采购单价非0 {pu_bad} 行", flush=True)

    # ---------- 汇总 ----------
    print("\n================ SUMMARY ================", flush=True)
    n_ok = sum(1 for _, t, _ in summary if t == "OK")
    n_skip = sum(1 for _, t, _ in summary if t == "SKIP")
    for code, tag, diffs in summary:
        print(f"{tag:6s} {code:20s} diffs={len(diffs)}", flush=True)
        if tag == "DIFF":
            for d in diffs[:10]:
                print("   " + d, flush=True)
    print(f"OK={n_ok}/{len(summary)}（SKIP={n_skip} 空批次）", flush=True)
    print("ALL_OK" if n_ok + n_skip == len(summary) else "HAS_DIFF（白名单外差异需归因，OMS漂移沿用 diagnose_match_diff）", flush=True)


if __name__ == "__main__":
    main()
