# -*- coding: utf-8 -*-
"""T2 精确取证：HYK 成本基准 id/吊牌价/年份/季节 单元格真实类型与字面值。"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
import openpyxl

BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证\成本匹配"
HYK = "好衣库_好衣库_秋水伊人_HYK001_2026年10月-2026年10月_账单匹配结果.xlsx"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t2_probe_cells.log"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    wb = openpyxl.load_workbook(os.path.join(BASE, HYK), read_only=True, data_only=True)
    for sh in ("收款匹配", "退款匹配"):
        ws = wb[sh]
        rows = list(ws.iter_rows(values_only=True))
        hdr = list(rows[0])
        ix = {c: hdr.index(c) for c in ("id", "sku", "吊牌价", "款号", "年份", "季节", "品牌", "品类", "采购单价", "数量", "货款金额", "订单日期", "存货异动时间")}
        print(f"== {sh} ==", flush=True)
        # id 空值行的真实类型
        from collections import Counter
        cnt = Counter()
        samples = {}
        for r in rows[1:]:
            v = r[ix["id"]]
            key = (type(v).__name__, repr(v)[:40])
            cnt[key] += 1
        for k, n in cnt.most_common(6):
            print("  id:", k, "x", n, flush=True)
        # 吊牌价类型分布
        cnt2 = Counter()
        for r in rows[1:]:
            v = r[ix["吊牌价"]]
            cnt2[(type(v).__name__, repr(v)[:30])] += 1
        for k, n in cnt2.most_common(6):
            print("  吊牌价:", k, "x", n, flush=True)
        # 无款号行的整行样例（前2行关键列）
        shown = 0
        for r in rows[1:]:
            if r[ix["款号"]] == "无款号" and shown < 2:
                print("  无款号行:", {c: repr(r[i])[:40] for c, i in ix.items()}, flush=True)
                shown += 1
        # 正常行样例
        for r in rows[1:]:
            if r[ix["款号"]] != "无款号":
                print("  正常行:", {c: repr(r[i])[:40] for c, i in ix.items()}, flush=True)
                break
    # 透视里吊牌价/款号空组的形态
    ws = wb["款号数据透视"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = list(rows[0])
    print("\n== 款号透视 无款号组 ==", flush=True)
    for r in rows[1:]:
        if r[hdr.index("款号")] in (None, "无款号", "None", "nan"):
            print("  ", r, flush=True)
    ws = wb["匹配透视"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = list(rows[0])
    print("\n== 匹配透视 无款号组(前4) ==", flush=True)
    n = 0
    for r in rows[1:]:
        if r[hdr.index("款号")] in (None, "无款号", "None", "nan") and n < 4:
            print("  ", r, flush=True); n += 1
    wb.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
