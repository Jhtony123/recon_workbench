# -*- coding: utf-8 -*-
"""T5 补充取证：KS001/DY020 退款匹配 货款金额 got=0/base=None 的行级定性。

判定：基准空白行是否=补录行（无账单金额）；我方填0是否改变合计（守恒）。
同时核对 HYK 成本基准同列行为（此前探针报 0 空白）。
"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
import openpyxl
from collections import Counter

COST_BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证\成本匹配"
SKU_BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证"
GOT_ROOT = r"C:\Users\贺杰\Desktop\账单\2026-10\sku匹配\已匹配成本"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t5_gross_probe.log"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def rows_of(path, sheet):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb[sheet].iter_rows(values_only=True))
    wb.close()
    return list(rows[0]), rows[1:]


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    for code in ("KS001", "DY020", "HYK001"):
        print(f"\n===== {code} =====", flush=True)
        # 定位文件
        got = base = sku = None
        for d in os.listdir(GOT_ROOT):
            if code in d:
                for f in os.listdir(os.path.join(GOT_ROOT, d)):
                    if f.endswith(".xlsx"):
                        got = os.path.join(GOT_ROOT, d, f)
        for f in os.listdir(COST_BASE):
            if code in f:
                base = os.path.join(COST_BASE, f)
        for f in os.listdir(SKU_BASE):
            if code in f and f.endswith(".xlsx"):
                sku = os.path.join(SKU_BASE, f)
        for sh in ("收款匹配", "退款匹配"):
            gh, gr = rows_of(got, sh)
            bh, br = rows_of(base, sh)
            gi, bi = gh.index("货款金额"), bh.index("货款金额")
            g_blank = sum(1 for r in gr if r[gi] is None)
            b_blank = sum(1 for r in br if r[bi] is None)
            g_zero = sum(1 for r in gr if r[gi] == 0)
            b_zero = sum(1 for r in br if r[bi] == 0)
            g_sum = sum(float(r[gi] or 0) for r in gr)
            b_sum = sum(float(r[bi] or 0) for r in br)
            print(f"  {sh}: got 空白={g_blank} 零={g_zero} 合计={g_sum:.2f} | base 空白={b_blank} 零={b_zero} 合计={b_sum:.2f}", flush=True)
            # 基准空白行的特征（处理意见/巨益单据匹配状态/是否秒退）
            if b_blank:
                cc = Counter()
                for r in br:
                    if r[bi] is None:
                        cc[(str(r[bh.index("处理意见")]), str(r[bh.index("巨益单据匹配状态")]), str(r[bh.index("是否秒退判断")]))] += 1
                print(f"    base空白行特征: {cc.most_common(5)}", flush=True)
                # 对应我方行是否=0
                samples = [(r[bh.index("订单编号")], r[bh.index("单号")]) for r in br if r[bi] is None][:3]
                print(f"    base空白行样例(订单,单号): {samples}", flush=True)
            # SKU 阶段基准同列
            if sku:
                sh_, sr = rows_of(sku, sh)
                si = sh_.index("货款金额")
                s_blank = sum(1 for r in sr if r[si] is None)
                print(f"    SKU阶段基准 空白={s_blank}", flush=True)
    print("\nPROBE_DONE", flush=True)


if __name__ == "__main__":
    main()
