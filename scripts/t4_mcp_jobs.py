# -*- coding: utf-8 -*-
"""T4 验收：经宿主 MCP 通道真实触发 cost / match Job，轮询终态。

cost：HYK（已在 已匹配成本/，重跑原地覆盖，秒级）；
match：KS001 快手（重跑覆盖 未匹配成本/ 既有批次，验证 Q8 遗留接线）。
用法: python scripts/t4_mcp_jobs.py
"""
import sys, os, json, time, urllib.request

BASE = "http://127.0.0.1:8765/agent/extensions/recon_workbench"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t4_mcp_jobs.log"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def call_tool(name, params):
    out = post("/mcp-tools/execute", {"tool_name": name, "params": params})
    return (out.get("result") or out)


def wait_job(job_id, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = get(f"/jobs/{job_id}")
        job = out.get("job") or out
        st = str(job.get("status") or "").lower()
        if st in ("success", "failed", "skipped", "cancelled", "timed_out", "partial", "deferred"):
            return job
        time.sleep(3)
    return {"status": "timeout-poll"}


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    results = []
    # ---- cost Job（HYK 重跑覆盖）----
    r = call_tool("recon_workbench__cost_match",
                  {"period": "2026-10", "bill_batch_id": "2026-10-好衣库-好衣库_秋水伊人-HYK001",
                   "cost_file": r"C:\Users\贺杰\Desktop\2026.10月账单\物料成本_202610.xlsx"})
    print("cost_match 触发:", json.dumps(r, ensure_ascii=False)[:400], flush=True)
    jid = ((r.get("data") or {}).get("job_id")) or ""
    if jid:
        job = wait_job(jid)
        st = job.get("status")
        print(f"cost Job 终态: {st} | {str(job.get('message'))[:200]}", flush=True)
        results.append(("cost", st in ("success", "partial")))
    else:
        results.append(("cost", False))

    # ---- match Job（KS001 快手，验证 Q8 遗留接线：不再统一调 run_bill）----
    r = call_tool("recon_workbench__sku_match",
                  {"period": "2026-10", "platform": "kuaishou", "shop_code": "KS001"})
    print("sku_match 触发:", json.dumps(r, ensure_ascii=False)[:400], flush=True)
    jid = ((r.get("data") or {}).get("job_id")) or ""
    if jid:
        job = wait_job(jid)
        st = job.get("status")
        print(f"match Job 终态: {st} | {str(job.get('message'))[:200]}", flush=True)
        results.append(("match", st in ("success", "partial")))
    else:
        results.append(("match", False))

    # ---- capability_status 与真实一致 ----
    r = call_tool("recon_workbench__capability_status", {})
    active = (r.get("data") or {}).get("active") or []
    ok_cap = set(active) == {"账单整理", "SKU对账", "成本匹配"}
    print("capability active:", active, flush=True)
    results.append(("capability", ok_cap))

    # ---- sku_batches 工具 ----
    r = call_tool("recon_workbench__sku_batches", {"period": "2026-10"})
    rows = (r.get("data") or {}).get("rows") or []
    print(f"sku_batches: {len(rows)} 个批次", flush=True)
    results.append(("sku_batches", len(rows) > 0))

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nSUMMARY {n_ok}/{len(results)}", flush=True)
    print("ALL_OK" if n_ok == len(results) else "HAS_FAIL", flush=True)


if __name__ == "__main__":
    main()
