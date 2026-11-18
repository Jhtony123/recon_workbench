# -*- coding: utf-8 -*-
"""T8 验收：经宿主 MCP 通道真实触发 chuanyun Job（**恒 dry-run，零 HTTP 不向氚云发请求**）。

  · chuanyun 全账期预检（2026-10，全部账单整理批次）
  · chuanyun 单批次预检（HYK）
  · capability_status 五能力 active 一致性
  · 批次目录无 氚云导入成功/ 子目录（dry-run 不移动文件）
用法: python scripts/t8_chuanyun_dryrun.py
"""
import sys, os, json, time, urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765/agent/extensions/recon_workbench"
LOG = os.path.join(os.path.dirname(__file__), "t8_chuanyun_dryrun.log")


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
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    results = []

    # ---- 全账期 dry-run 预检 ----
    r = call_tool("recon_workbench__chuanyun_sync", {"period": "2026-10"})
    print("chuanyun(全账期,dry-run) 触发:", json.dumps(r, ensure_ascii=False)[:400], flush=True)
    jid = ((r.get("data") or {}).get("job_id")) or ""
    hyk_batch = ""
    if jid:
        job = wait_job(jid)
        st = job.get("status")
        data = (job.get("result") or {}).get("data") or {}
        print(f"chuanyun Job 终态: {st} | {str(job.get('message'))[:300]}", flush=True)
        print(f"  total_files={data.get('total_files')} ok={data.get('ok')} "
              f"failed={data.get('failed')} skipped={data.get('skipped')} "
              f"dry_run={data.get('dry_run')} pushed_to_h3={data.get('pushed_to_h3')} "
              f"master_loaded={data.get('master_loaded')}", flush=True)
        reports = data.get("file_reports") or []
        for rep in reports[:5]:
            print(f"  [{rep.get('status')}] {rep.get('file')} stats={rep.get('stats')} "
                  f"reason={str(rep.get('reason') or '')[:100]}", flush=True)
        previews = data.get("previews") or []
        print(f"  payload预览文件数: {len(previews)}", flush=True)
        results.append(("chuanyun_all", st in ("success", "partial", "skipped")
                        and data.get("dry_run") is True
                        and data.get("pushed_to_h3") is False
                        and (data.get("total_files") or 0) > 0))
        # 找一个 HYK 批次做单批预检
        for rep in reports:
            if "HYK" in str(rep.get("batch") or ""):
                hyk_batch = rep["batch"]
                break
    else:
        results.append(("chuanyun_all", False))

    # ---- 单批次 dry-run（HYK，找不到则取第一个成功批次）----
    if not hyk_batch:
        r = call_tool("recon_workbench__bill_batches", {"period": "2026-10", "limit": 100})
        rows = (r.get("data") or {}).get("rows") or []
        for b in rows:
            if "HYK" in str(b.get("shop_code") or "") or "好衣库" in str(b.get("shop_name") or ""):
                hyk_batch = b.get("batch_id") or ""
                break
        if not hyk_batch and rows:
            hyk_batch = rows[0].get("batch_id") or ""
    r = call_tool("recon_workbench__chuanyun_sync",
                  {"period": "2026-10", "bill_batch_id": hyk_batch})
    jid = ((r.get("data") or {}).get("job_id")) or ""
    if jid:
        job = wait_job(jid)
        st = job.get("status")
        data = (job.get("result") or {}).get("data") or {}
        print(f"chuanyun(单批 {hyk_batch}) 终态: {st} | {str(job.get('message'))[:200]} "
              f"| files={data.get('total_files')} ok={data.get('ok')}", flush=True)
        results.append(("chuanyun_single", st in ("success", "skipped")
                        and data.get("pushed_to_h3") is False))
    else:
        results.append(("chuanyun_single", False))

    # ---- capability 一致性 ----
    r = call_tool("recon_workbench__capability_status", {})
    active = (r.get("data") or {}).get("active") or []
    print("capability active:", active, flush=True)
    results.append(("capability", "氚云对接" in active and len(active) == 5))

    # ---- dry-run 不移动文件：批次目录内不应出现 氚云导入成功/ ----
    r = call_tool("recon_workbench__get_settings", {})
    results_dir = ((r.get("data") or {}).get("results_dir")
                   or (r.get("data") or {}).get("effective_dir") or "")
    moved_dirs = []
    if results_dir:
        root = Path(results_dir) / "2026-10" / "账单整理"
        if root.is_dir():
            moved_dirs = [str(p) for p in root.glob("*/氚云导入成功")]
    print(f"氚云导入成功/ 子目录数: {len(moved_dirs)}（dry-run 后应为 0）", flush=True)
    results.append(("no_move", len(moved_dirs) == 0))

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nSUMMARY {n_ok}/{len(results)}", flush=True)
    print("ALL_OK" if n_ok == len(results) else "HAS_FAIL", flush=True)


if __name__ == "__main__":
    main()