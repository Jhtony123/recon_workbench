# -*- coding: utf-8 -*-
"""T2 实施前口径探针：HYK 成本匹配基准 vs 插件 match 底稿，逐列取证。

只读不写产物。输出落工作区日志，供 cost.py 阶段分支实现对照。
"""
import sys, os, json
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

import pandas as pd
import openpyxl
from core import artifacts

BASE = r"C:\Users\贺杰\Desktop\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证\成本匹配"
HYK = "好衣库_好衣库_秋水伊人_HYK001_2026年10月-2026年10月_账单匹配结果.xlsx"
LOG = r"D:\DesktopAgent\data\workspaces\cc9812b469e44f9dbd90077e949870d7\output\files\logs\t2_probe.log"


class _Tee:
    def __init__(self, s, f):
        self.s, self.f = s, f

    def write(self, x):
        self.s.write(x); self.f.write(x); self.f.flush()

    def flush(self):
        self.s.flush(); self.f.flush()


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    fh = open(LOG, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)

    p = os.path.join(BASE, HYK)
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    print("== sheets ==", wb.sheetnames, flush=True)
    books = {}
    for ws in wb:
        rows = list(ws.iter_rows(values_only=True))
        books[ws.title] = rows
        print(f"\n-- {ws.title}: {len(rows)-1} 行 / {len(rows[0])} 列", flush=True)
        print("   列头:", list(rows[0]), flush=True)
    wb.close()

    # 明细占位符/精度取证
    for sh in ("收款匹配", "退款匹配"):
        rows = books[sh]
        hdr = list(rows[0])
        df = pd.DataFrame(rows[1:], columns=hdr)
        print(f"\n== {sh} 取证 ==", flush=True)
        for col in ("款号", "吊牌价", "商品名称", "年份", "季节", "品牌", "品类", "id", "sku"):
            if col not in df.columns:
                continue
            vc = df[col].map(lambda v: "<NA>" if v is None else str(v)).value_counts()
            print(f"  {col} top8: {dict(list(vc.items())[:8])}", flush=True)
        # 采购单价精度
        pu = df["采购单价"].tolist()
        hi = [v for v in pu if isinstance(v, float) and abs(v - round(v, 2)) > 1e-9]
        print(f"  采购单价 非2位小数行数={len(hi)} 样例={hi[:5]}", flush=True)
        print(f"  采购单价 空值行数={sum(1 for v in pu if v is None)}", flush=True)
        print(f"  发货成本 空值行数={sum(1 for v in df['发货成本'] if v is None)}", flush=True)
        print(f"  数量 负数行数={sum(1 for v in df['数量'] if isinstance(v,(int,float)) and v<0)}", flush=True)
        print(f"  货款金额 空值行数={sum(1 for v in df['货款金额'] if v is None)}", flush=True)

    # 透视锚点
    rows = books["店铺数据透视"]
    print("\n== 店铺数据透视 全行 ==", flush=True)
    for r in rows:
        print("  ", r, flush=True)

    # match 底稿列
    d = artifacts.sku_root("2026-10") / "2026-10-好衣库-好衣库_秋水伊人-HYK001" / "底稿"
    mp = d / "match_detail.csv"
    if mp.is_file():
        mdf = pd.read_csv(mp, dtype=str, encoding="utf-8-sig")
        print(f"\n== match_detail.csv {len(mdf)} 行 / {len(mdf.columns)} 列 ==", flush=True)
        print("   列:", list(mdf.columns), flush=True)
        for col in ("款号", "吊牌价", "品牌", "品类", "id", "采购单价", "标准成本", "数据来源"):
            if col in mdf.columns:
                vc = mdf[col].fillna("<NA>").value_counts()
                print(f"  {col} top6: {dict(list(vc.items())[:6])}", flush=True)
    else:
        print("[MISS]", mp, flush=True)
    print("\nPROBE_DONE", flush=True)


if __name__ == "__main__":
    main()
