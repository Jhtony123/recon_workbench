# -*- coding: utf-8 -*-
"""T7 验收：经宿主 MCP 通道真实触发 writeback Job（**恒 dry-run，不写生产库**）。

  · writeback 全账期预检（2026-10，全部 SKU 批次）
  · writeback 单批次预检（HYK）
  · capability_status / sku_batches(已回传数据库) 一致性
用法: python scripts/t7_writeback_dryrun.py
"""
import sys, os, json, time, urllib.request

BASE = "http://127.0.0.1:8765/agent/extensions/recon_workbench"
LOG = os.path.join(os.path.dirname(__file__), "t7_writeback_dryrun.log")


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


def wait_job(job_id, timeout=600):
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
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    results = []

    # ---- 全账期 dry-run 预检 ----
    r = call_tool("recon_workbench__writeback", {"period": "2026-10"})
    print("writeback(全账期,dry-run) 触发:", json.dumps(r, ensure_ascii=False)[:400], flush=True)
    jid = ((r.get("data") or {}).get("job_id")) or ""
    if jid:
        job = wait_job(jid)
        st = job.get("status")
        data = (job.get("result") or {}).get("data") or {}
        print(f"writeback Job 终态: {st} | {str(job.get('message'))[:300]}", flush=True)
        print(f"  total={data.get('total')} ok={data.get('ok')} failed={data.get('failed')} "
              f"units={data.get('units')} dry_run={data.get('dry_run')} "
              f"stored_in_db={data.get('stored_in_db')} migrated={data.get('migrated')}", flush=True)
        prev = data.get("sql_preview") or []
        print("  SQL预览:", (prev[0] if prev else "(空)"), flush=True)
        results.append(("writeback_all", st == "success" and data.get("dry_run") is True
                        and data.get("stored_in_db") is False and not data.get("migrated")
                        and (data.get("total") or 0) > 0))
    else:
        results.append(("writeback_all", False))

    # ---- 单批次 dry-run（HYK）----
    r = call_tool("recon_workbench__writeback",
                  {"period": "2026-10",
                   "bill_batch_id": "2026-10-好衣库-好衣库_秋水伊人-HYK001"})
    jid = ((r.get("data") or {}).get("job_id")) or ""
    if jid:
        job = wait_job(jid)
        st = job.get("status")
        data = (job.get("result") or {}).get("data") or {}
        print(f"writeback(HYK) 终态: {st} | {str(job.get('message'))[:200]} | total={data.get('total')}", flush=True)
        results.append(("writeback_hyk", st == "success" and (data.get("total") or 0) > 0
                        and data.get("stored_in_db") is False))
    else:
        results.append(("writeback_hyk", False))

    # ---- capability 一致性 ----
    r = call_tool("recon_workbench__capability_status", {})
    active = (r.get("data") or {}).get("active") or []
    print("capability active:", active, flush=True)
    results.append(("capability", "数据库回传" in active and "氚云对接" in active))

    # ---- 已回传数据库/ 目录应为空（dry-run 不迁移）----
    r = call_tool("recon_workbench__sku_batches", {"period": "2026-10", "state": "已回传数据库"})
    rows = (r.get("data") or {}).get("rows") or []
    print(f"已回传数据库/ 批次数: {len(rows)}（dry-run 后应为 0）", flush=True)
    results.append(("no_migration", len(rows) == 0))

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nSUMMARY {n_ok}/{len(results)}", flush=True)
    print("ALL_OK" if n_ok == len(results) else "HAS_FAIL", flush=True)


if __name__ == "__main__":
    main()