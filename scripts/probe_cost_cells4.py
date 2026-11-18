# -*- coding: utf-8 -*-
"""T2 取证4：VIP/AKC 匹配透视空组形态 + SKU/COST 基准日期列类型 + HYK 匹配透视全组键。"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
import openpyxl
from collections import Counter

SKU_BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证"
COST_BASE = os.path.join(SKU_BASE, "成本匹配")
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t2_probe4.log"


class _Tee:
    def __init__(self, s, f): self.s, self.f = s, f
    def write(self, x): self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self): self.s.flush(); self.f.flush()


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    for name in ("唯品会_2026年1001-2026年1010唯品中国_账单匹配结果.xlsx",
                 "爱库存_2026年10月-2026年10月爱库存_账单匹配结果.xlsx"):
        p = os.path.join(COST_BASE, name)
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        print(f"\n===== {name[:20]} 匹配透视 =====", flush=True)
        rows = list(wb["匹配透视"].iter_rows(values_only=True))
        hdr = list(rows[0])
        print("列头:", hdr, flush=True)
        ix = {c: hdr.index(c) for c in hdr}
        # 品类/季节/品牌/吊牌价 值分布（top）
        for c in ("品类", "季节", "品牌", "吊牌价", "款号", "年份"):
            cnt = Counter(repr(r[ix[c]])[:20] for r in rows[1:])
            print(f"  {c} top6:", cnt.most_common(6), flush=True)
        # 含 None/空/未知 的组样例
        n = 0
        for r in rows[1:]:
            vals = [r[ix[c]] for c in ("品类", "季节", "品牌", "吊牌价")]
            if any(v in (None, "", "None", "nan", "未知", "无季节", "无吊牌价") for v in vals) and n < 5:
                print("  特殊组:", r, flush=True); n += 1
        # 款号透视 吊牌价列类型
        rows2 = list(wb["款号数据透视"].iter_rows(values_only=True))
        hdr2 = list(rows2[0])
        cnt = Counter((type(r[hdr2.index("吊牌价")]).__name__, repr(r[hdr2.index("吊牌价")])[:20]) for r in rows2[1:])
        print("  款号透视吊牌价类型:", cnt.most_common(5), flush=True)
        wb.close()

    # HYK SKU 基准 vs COST 基准：日期列类型 + 匹配透视键集合
    for tag, base in (("SKU", SKU_BASE), ("COST", COST_BASE)):
        p = os.path.join(base, "好衣库_好衣库_秋水伊人_HYK001_2026年10月-2026年10月_账单匹配结果.xlsx")
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        rows = list(wb["收款匹配"].iter_rows(values_only=True))
        hdr = list(rows[0])
        print(f"\n== HYK {tag} 收款匹配 日期/文本列类型 ==", flush=True)
        for c in ("订单日期", "存货异动时间", "单号", "订单编号", "巨益单据匹配状态", "历史对账记录", "对账周期", "是否秒退判断", "处理意见", "账单周期", "店铺名称", "退款类型", "退款状态", "单据类型"):
            if c in hdr:
                cnt = Counter((type(r[hdr.index(c)]).__name__, repr(r[hdr.index(c)])[:26]) for r in rows[1:])
                print(f"  {c}:", cnt.most_common(3), flush=True)
        rows = list(wb["匹配透视"].iter_rows(values_only=True))
        hdr = list(rows[0])
        keys = set()
        for r in rows[1:]:
            keys.add(tuple(repr(v)[:16] for v in r[:9]))
        print(f"  匹配透视 {len(rows)-1} 行 / 键 {len(keys)}", flush=True)
        wb.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
