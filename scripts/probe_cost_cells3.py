# -*- coding: utf-8 -*-
"""T2 取证3：SKU阶段基准吊牌价类型 + VIP/AKC 成本基准占位符形态（B2B 空串语义）。"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
import openpyxl
from collections import Counter

SKU_BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证"
COST_BASE = os.path.join(SKU_BASE, "成本匹配")
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t2_probe3.log"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def col_types(rows, hdr, cols, label):
    ix = {c: hdr.index(c) for c in cols if c in hdr}
    print(f"-- {label}", flush=True)
    for c, i in ix.items():
        cnt = Counter((type(r[i]).__name__, repr(r[i])[:24]) for r in rows[1:])
        print(f"  {c}:", cnt.most_common(5), flush=True)


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    # 1) SKU 阶段 HYK 基准：吊牌价/id/款号 类型
    p = os.path.join(SKU_BASE, "好衣库_好衣库_秋水伊人_HYK001_2026年10月-2026年10月_账单匹配结果.xlsx")
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    for sh in ("收款匹配",):
        rows = list(wb[sh].iter_rows(values_only=True))
        col_types(rows, list(rows[0]), ("吊牌价", "id", "款号", "采购单价", "品牌", "品类"), f"SKU基准 {sh}")
    wb.close()

    # 2) VIP 上旬成本基准
    p = os.path.join(COST_BASE, "唯品会_2026年1001-2026年1010唯品中国_账单匹配结果.xlsx")
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    print("\n== VIP 上旬成本基准 ==", wb.sheetnames, flush=True)
    for sh in ("收款匹配", "退款匹配"):
        rows = list(wb[sh].iter_rows(values_only=True))
        hdr = list(rows[0])
        print(f"-- {sh}: {len(rows)-1} 行", flush=True)
        col_types(rows, hdr, ("款号", "吊牌价", "商品名称", "年份", "季节", "品牌", "品类", "id", "sku"), sh)
        ix = {c: hdr.index(c) for c in ("品牌", "采购单价", "发货成本", "标准成本", "结算金额", "店铺名称")}
        fr = [r for r in rows[1:] if r[ix["品牌"]] == "FR"]
        nonfr = [r for r in rows[1:] if r[ix["品牌"]] != "FR"]
        print(f"  FR行={len(fr)} 非FR行={len(nonfr)}", flush=True)
        if fr:
            bad_std = sum(1 for r in fr if r[ix["标准成本"]] != r[ix["结算金额"]])
            bad_ship = sum(1 for r in fr if r[ix["发货成本"]] != r[ix["标准成本"]])
            pu_nonzero = sum(1 for r in fr if r[ix["采购单价"]] not in (0, None))
            print(f"  FR: 标准成本!=结算 {bad_std} 行; 发货!=标准 {bad_ship} 行; 采购单价非0 {pu_nonzero} 行", flush=True)
            r0 = fr[0]
            print("  FR样例:", {c: repr(r0[i])[:30] for c, i in ix.items()}, flush=True)
        if nonfr:
            r0 = nonfr[0]
            print("  非FR样例:", {c: repr(r0[i])[:30] for c, i in ix.items()}, flush=True)
            hi = sum(1 for r in nonfr if isinstance(r[ix["采购单价"]], float) and abs(r[ix["采购单价"]] - round(r[ix["采购单价"]], 2)) > 1e-9)
            print(f"  非FR 采购单价高精度行={hi}", flush=True)
        # 店铺名称形态
        names = Counter(r[ix["店铺名称"]] for r in rows[1:])
        print("  店铺名称:", names.most_common(5), flush=True)
    # 透视占位
    rows = list(wb["款号数据透视"].iter_rows(values_only=True))
    hdr = list(rows[0])
    print("-- 款号透视 列头:", hdr, flush=True)
    for r in rows[1:]:
        if r[hdr.index("款号")] in (None, "", "无款号", "None", "nan", "未知"):
            print("  空款号组:", r, flush=True)
    rows = list(wb["匹配透视"].iter_rows(values_only=True))
    hdr = list(rows[0])
    n = 0
    for r in rows[1:]:
        if r[hdr.index("款号")] in (None, "", "无款号", "None", "nan", "未知") and n < 3:
            print("  匹配透视空款号组:", r, flush=True); n += 1
    wb.close()

    # 3) AKC 成本基准
    p = os.path.join(COST_BASE, "爱库存_2026年10月-2026年10月爱库存_账单匹配结果.xlsx")
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    print("\n== AKC 成本基准 ==", wb.sheetnames, flush=True)
    for sh in ("收款匹配", "退款匹配"):
        rows = list(wb[sh].iter_rows(values_only=True))
        hdr = list(rows[0])
        print(f"-- {sh}: {len(rows)-1} 行", flush=True)
        col_types(rows, hdr, ("款号", "吊牌价", "商品名称", "年份", "季节", "品牌", "品类", "id"), sh)
    rows = list(wb["匹配透视"].iter_rows(values_only=True))
    hdr = list(rows[0])
    n = 0
    for r in rows[1:]:
        if r[hdr.index("款号")] in (None, "", "无款号", "None", "nan", "未知") and n < 3:
            print("  匹配透视空款号组:", r, flush=True); n += 1
    n = 0
    for r in rows[1:]:
        if r[hdr.index("季节")] in (None, "", "无季节") and n < 3:
            print("  匹配透视空季节组:", r, flush=True); n += 1
    wb.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
