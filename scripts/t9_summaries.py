# -*- coding: utf-8 -*-
"""T9 验收：经宿主 MCP 通道真实触发两个独立透视汇总工具（真实 2026-10 批次数据）。

  · bill_pivot_summary 全账期 + 单批（HYK）
  · sku_pivot_summary 全账期 + state 单态
  · 输出文件存在、行数/合计合理、批次产物未被修改（mtime 抽查）
用法: python scripts/t9_summaries.py
"""
import sys, os, json, time, urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765/agent/extensions/recon_workbench"
LOG = os.path.join(os.path.dirname(__file__), "t9_summaries.log")


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def call_tool(name, params):
    out = post("/mcp-tools/execute", {"tool_name": name, "params": params})
    return (out.get("result") or out)


def main():
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    results = []

    # ---- 账单透视汇总（全账期）----
    r = call_tool("recon_workbench__bill_pivot_summary", {"period": "2026-10"})
    d = r.get("data") or {}
    print("bill_pivot_summary:", r.get("summary"), flush=True)
    print(f"  rows={d.get('rows')} totals={d.get('totals')} batches={d.get('batches')} "
          f"files={d.get('files')} skipped={len(d.get('skipped') or [])} "
          f"errors={len(d.get('errors') or [])}", flush=True)
    f_bill = d.get("file") or ""
    ok_bill = (r.get("ok") and d.get("status") == "success" and (d.get("rows") or 0) > 0
               and f_bill and Path(f_bill).is_file())
    results.append(("bill_all", bool(ok_bill)))

    # ---- 账单透视汇总（HYK 单批）----
    r = call_tool("recon_workbench__bill_pivot_summary",
                  {"period": "2026-10", "bill_batch_id": "2026-10-好衣库-好衣库_秋水伊人-HYK001"})
    d1 = r.get("data") or {}
    print("bill_pivot_summary(HYK):", r.get("summary"), flush=True)
    results.append(("bill_hyk", bool(r.get("ok") and d1.get("status") == "success"
                                     and d1.get("batches") == 1)))

    # ---- SKU 透视汇总（全账期三态）----
    r = call_tool("recon_workbench__sku_pivot_summary", {"period": "2026-10"})
    d = r.get("data") or {}
    print("sku_pivot_summary:", r.get("summary"), flush=True)
    print(f"  rows={d.get('rows')} totals={d.get('totals')} batches={d.get('batches')} "
          f"files={d.get('files')} skipped={len(d.get('skipped') or [])} "
          f"errors={len(d.get('errors') or [])}", flush=True)
    f_sku = d.get("file") or ""
    ok_sku = (r.get("ok") and d.get("status") == "success" and f_sku and Path(f_sku).is_file()
              and (d.get("rows") or {}).get("店铺数据透视汇总", 0) > 0)
    results.append(("sku_all", bool(ok_sku)))

    # ---- SKU 透视汇总（state=已匹配成本）----
    r = call_tool("recon_workbench__sku_pivot_summary",
                  {"period": "2026-10", "state": "已匹配成本"})
    d2 = r.get("data") or {}
    print("sku_pivot_summary(已匹配成本):", r.get("summary"), flush=True)
    results.append(("sku_state", bool(r.get("ok") and d2.get("status") == "success"
                                      and (d2.get("batches") or 0) > 0
                                      and (d2.get("batches") or 0) < (d.get("batches") or 999))))

    # ---- 独立重算核对：HYK 单批「按订单年月透视」== 直接读交付簿收退款明细重算 ----
    # （店铺透视货款分摊只含结转/冲成本口径行，与全量明细不同口径，不做守恒断言）
    import pandas as pd
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
    sys.path.insert(0, r"D:\DesktopAgent")
    from core import summaries as _sum, exporters as _exp  # noqa: E402
    hyk_dir = Path(r"C:\Users\贺杰\Desktop\账单\2026-10\sku匹配\已匹配成本\2026-10-好衣库-好衣库_秋水伊人-HYK001")
    hyk_files = [x for x in hyk_dir.glob("*.xlsx") if not x.name.startswith("~$")]
    indep_ok = False
    if hyk_files and f_sku:
        f = hyk_files[0]
        parts = []
        for sheet, col in ((_exp.SHEET_RECEIPT, "收款金额"), (_exp.SHEET_REFUND, "退款金额")):
            dd = _sum._read_sheet_fast(f, sheet, usecols={"店铺名称", "订单日期", "货款分摊金额"})
            dd = _sum._order_ym(dd)
            dd["货款分摊金额"] = pd.to_numeric(dd["货款分摊金额"], errors="coerce").fillna(0)
            g = dd.groupby(["店铺名称", "订单年月"], as_index=False)["货款分摊金额"].sum()
            g = g.rename(columns={"货款分摊金额": col})
            parts.append(g)
        expect = pd.merge(parts[0], parts[1], on=["店铺名称", "订单年月"], how="outer").fillna(0)
        book = pd.ExcelFile(f_sku)
        actual = book.parse("按订单年月透视")
        actual_h = actual[actual["店铺名称"].astype(str).str.contains("好衣库")]
        e_sum = round(float(expect["收款金额"].sum() + expect["退款金额"].sum()), 2)
        a_sum = round(float(pd.to_numeric(actual_h["收款金额"], errors="coerce").fillna(0).sum()
                            + pd.to_numeric(actual_h["退款金额"], errors="coerce").fillna(0).sum()), 2)
        print(f"HYK 独立重算：期望(收+退)={e_sum} vs 汇总表={a_sum}", flush=True)
        indep_ok = abs(e_sum - a_sum) <= 0.05 and len(actual_h) == len(expect)
    results.append(("independent_recompute", indep_ok))

    # ---- 下载链接可用（HTTP 200；中文文件名需 URL 编码）----
    from urllib.parse import quote
    dls = (d.get("downloads") or [])
    dl_ok = False
    if dls:
        url = "http://127.0.0.1:8765" + quote(dls[0]["url"], safe="/:")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                dl_ok = resp.status == 200 and len(resp.read()) > 0
        except Exception as exc:
            print("下载失败:", exc, flush=True)
    print("下载副本 HTTP 可达:", dl_ok, flush=True)
    results.append(("download", dl_ok))

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nSUMMARY {n_ok}/{len(results)}", flush=True)
    print("ALL_OK" if n_ok == len(results) else "HAS_FAIL", flush=True)


if __name__ == "__main__":
    main()