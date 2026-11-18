# -*- coding: utf-8 -*-
"""T3 验收：迁移状态机失败注入测试（不跑真实大文件，monkeypatch 单元执行）。

用例：
  F1 成本表缺 sheet → failed 可读报错，批次留守；
  F2 VIP 单旬失败 → 整目录留守 未匹配成本/，成功旬产物在、失败旬无半截交付；
  F3 迁移失败（模拟磁盘只读 OSError）→ 留守；
  F4 clear_history 不误删 sku匹配/（含 已匹配成本/）；
  F5 成功迁移无残留（HYK 已实测，这里断言目录状态）。
用法: python scripts/test_cost_migration.py
"""
import sys, os, json, shutil
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

import pandas as pd
from core import pipeline, artifacts, cost

PERIOD = "2026-10"
COST_FILE = r"C:\Users\贺杰\Desktop\2026.10月账单\物料成本_202610.xlsx"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t3_failinject.log"
TMP = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\tmp_cost_tests"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}", flush=True)


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    os.makedirs(TMP, exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)

    vip_name = "2026-10-唯品会-唯品中国"
    vip_pending = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_PENDING_COST) / vip_name
    vip_costed = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_COSTED) / vip_name

    # ---------- F1 成本表缺 sheet ----------
    bad = os.path.join(TMP, "物料成本_bad.xlsx")
    pd.DataFrame({"物料编码": ["QS"], "物料名称": ["x"], "规格型号": ["925308C050001"],
                  "数量": [None], "单价": [10.0]}).to_excel(bad, sheet_name="1月末", index=False)
    try:
        res = pipeline.run_cost(period=PERIOD, cost_file=bad,
                                bill_batch_id=vip_name)
        check("F1 缺sheet终态failed", res.status == "failed", res.message[:120])
    except Exception as e:
        check("F1 缺sheet可读报错", "sheet" in str(e).lower() or "10月末" in str(e),
              f"{type(e).__name__}: {str(e)[:120]}")
    check("F1 VIP目录留守", vip_pending.is_dir() and not vip_costed.exists())

    # ---------- F2 VIP 单旬失败 → 整目录留守 ----------
    real_unit = pipeline._run_cost_unit
    calls = {"n": 0}

    def fake_unit(unit, **kw):
        calls["n"] += 1
        label = unit.get("label") or ""
        if "1011" in label:      # 第三旬注入失败
            raise RuntimeError("注入：第三旬成本回填失败")
        # 其余旬：只写一份最小交付占位（不真跑 30 万行），验证状态机
        batch_dir = kw["batch_dir"]
        man = unit.get("manifest") or {}
        src_excel = str((man.get("files") or {}).get("excel") or "")
        fname = os.path.basename(src_excel) if src_excel else f"{label}.xlsx"
        p = batch_dir / fname
        if not p.exists():
            pd.DataFrame({"x": [1]}).to_excel(p, index=False)
        return {"label": label, "rows": 1, "receipt_rows": 1, "refund_rows": 0,
                "missing_no_style": 0, "missing_no_cost": 0, "missing_styles": [],
                "std_cost_fen": 0, "ship_cost_fen": 0, "exported": str(p)}

    pipeline._run_cost_unit = fake_unit
    try:
        res = pipeline.run_cost(period=PERIOD, cost_file=COST_FILE, bill_batch_id=vip_name)
        check("F2 单旬失败终态partial", res.status == "partial", res.message[:120])
        fb = (res.data or {}).get("failed_batches") or []
        check("F2 汇报标明失败旬", any("1011" in json.dumps(f, ensure_ascii=False) for f in fb),
              json.dumps(fb, ensure_ascii=False)[:200])
        check("F2 整目录留守未匹配成本", vip_pending.is_dir() and not vip_costed.exists())
        check("F2 无 cost-tmp 残留", not list(vip_pending.glob("*.cost-tmp.xlsx")))
    finally:
        pipeline._run_cost_unit = real_unit

    # ---------- F3 迁移失败 → 留守 ----------
    real_migrate = artifacts.migrate_sku_batch

    def fake_migrate(batch_dir, **kw):
        raise OSError("注入：磁盘只读，move 失败")

    # 用 HYK（已在 已匹配成本/，重跑不迁移）不合适；改用 AKC 批次做迁移注入。
    # AKC 真实跑一遍成本匹配（3727+1980 行，秒级），迁移函数注入失败。
    artifacts.migrate_sku_batch = fake_migrate
    pipeline.migrate_sku_batch = fake_migrate if hasattr(pipeline, "migrate_sku_batch") else None
    try:
        # run_cost 内部走 artifacts.migrate_sku_batch
        res = pipeline.run_cost(period=PERIOD, cost_file=COST_FILE,
                                bill_batch_id="2026-10-爱库存")
        akc_pending = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_PENDING_COST) / "2026-10-爱库存"
        akc_costed = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_COSTED) / "2026-10-爱库存"
        check("F3 迁移失败终态partial", res.status == "partial", res.message[:120])
        check("F3 AKC留守未匹配成本", akc_pending.is_dir() and not akc_costed.exists())
        check("F3 交付件已生成(原地)", any(akc_pending.glob("*.xlsx")))
    finally:
        artifacts.migrate_sku_batch = real_migrate

    # ---------- F4 clear_history 不误删 sku匹配 ----------
    before = {p.name for p in artifacts.sku_root(PERIOD, artifacts.SKU_STATE_COSTED).iterdir()}
    r = artifacts.clear_history(period="2099-01")   # 不存在的账期，双保险
    after = {p.name for p in artifacts.sku_root(PERIOD, artifacts.SKU_STATE_COSTED).iterdir()}
    check("F4 clear_history不动已匹配成本", before == after and "2026-10-好衣库-好衣库_秋水伊人-HYK001" in after)

    # ---------- F5 成功迁移无残留（HYK 实测状态断言） ----------
    hyk_pending = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_PENDING_COST) / "2026-10-好衣库-好衣库_秋水伊人-HYK001"
    hyk_costed = artifacts.sku_root(PERIOD, artifacts.SKU_STATE_COSTED) / "2026-10-好衣库-好衣库_秋水伊人-HYK001"
    check("F5 HYK迁移无残留", not hyk_pending.exists() and hyk_costed.is_dir()
          and any(hyk_costed.glob("*.xlsx")))

    n_ok = sum(1 for _, ok in RESULTS if ok)
    print(f"\nSUMMARY {n_ok}/{len(RESULTS)}", flush=True)
    print("ALL_OK" if n_ok == len(RESULTS) else "HAS_FAIL", flush=True)


if __name__ == "__main__":
    main()
