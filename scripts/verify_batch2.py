# -*- coding: utf-8 -*-
"""第二批平台基准对齐回归（2026-11-16）：快手/视频号/小红书/有赞/好衣库/爱库存/唯品会。

比对方法与第一批一致：sheet 名与顺序 / 列头（含重名列）/ 行数 / 全部数值列合计（容差0.011元）。
用法: python scripts/verify_batch2.py [case ...]   （vip 大文件建议单独跑）
"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

import openpyxl
from core import pipeline

RAW = r"E:\AIcoding_project\xbot_robot\历史运行结果\原始账单"
BASE = r"E:\AIcoding_project\xbot_robot\历史运行结果\账单整理汇总\氚云导入成功"
P = "2026-08"; S = "2026-08-01"; E = "2026-08-31"

CASES = {
    "ks": dict(platform="kuaishou", shop="KS001", s=S, e=E,
        files=[os.path.join(RAW, r"快手-OK\2026.8-秋水快手-KS001")], extra={},
        baselines=["快手_快手_秋水伊人官方旗舰店_KS001_2026年08月-2026年08月_账单整理.xlsx"]),
    "sph1": dict(platform="sph", shop="SPH001", s=S, e=E,
        files=[os.path.join(RAW, r"视频号-OK\2026.8-视频号_秋水伊人官方旗舰店-SPH001")], extra={},
        baselines=["视频号_视频号_秋水伊人官方旗舰店_SPH001_2026年08月-2026年08月_账单整理.xlsx"]),
    "sph3": dict(platform="sph", shop="SPH003", s=S, e=E,
        files=[os.path.join(RAW, r"视频号-OK\2026.8-视频号_MISS_COCOON官方旗舰店-SPH003")], extra={},
        baselines=["视频号_视频号_MISS_COCOON官方旗舰店_SPH003_2026年08月-2026年08月_账单整理.xlsx"]),
    "sph4": dict(platform="sph", shop="SHP004", s=S, e=E,
        files=[os.path.join(RAW, r"视频号-OK\2026.8-视频号_C3-SHP004")], extra={},
        baselines=["视频号_视频号_CHICC3官方店_SHP004_2026年08月-2026年08月_账单整理.xlsx"]),
    "sph5": dict(platform="sph", shop="SPH005", s=S, e=E,
        files=[os.path.join(RAW, r"视频号-OK\2026.8-视频号_MISS_COCOON女装店-SPH005")], extra={},
        baselines=["视频号_视频号_MISS COCOON女装店_SPH005_2026年08月-2026年08月_账单整理.xlsx"]),
    "xhs2": dict(platform="xhs", shop="XHS002", s=S, e=E,
        files=[os.path.join(RAW, r"小红书-OK\2026.8-小红书_秋水伊人旗舰店-XHS002")], extra={},
        baselines=["小红书结算单_小红书_秋水伊人旗舰店_XHS002_2026年08月-2026年08月_账单整理.xlsx"]),
    "xhs3": dict(platform="xhs", shop="XHS003", s=S, e=E,
        files=[os.path.join(RAW, r"小红书-OK\2026.8-小红书_MISS COCOON旗舰店-XHS003")], extra={},
        baselines=["小红书结算单_小红书_MISS COCOON旗舰店_XHS003_2026年08月-2026年08月_账单整理.xlsx"]),
    "xhs5": dict(platform="xhs", shop="XHS005", s=S, e=E,
        files=[os.path.join(RAW, r"小红书-OK\2026.8-小红书_C3-XHS005")], extra={},
        # 基准文件被用户改名为「账单整理1.xlsx」（2026-11-16 发现）
        baselines=["小红书结算单_小红书_C3官方旗舰店_XHS005_2026年08月-2026年08月_账单整理1.xlsx"]),
    # 原始目录解压文件已被用户删除（仅存 zip），改用工作区解压副本（2026-11-16）
    "yz2": dict(platform="youzan", shop="YZ002", s=S, e=E,
        files=[os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), r"input\youzan\YZ002")], extra={},
        baselines=["有赞_有赞_秋水伊人女装旗舰店_YZ002_2026年08月-2026年08月_账单整理.xlsx"]),
    "yz3": dict(platform="youzan", shop="YZ003", s=S, e=E,
        files=[os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), r"input\youzan\YZ003")], extra={},
        baselines=["有赞_有赞_missCOCOON官方旗舰店_YZ003_2026年08月-2026年08月_账单整理.xlsx"]),
    # 原始目录标 HYK002，但影刀基准与店铺映射均为 HYK001（好衣库_秋水伊人）
    "hyk": dict(platform="hyk", shop="HYK001", s=S, e=E,
        files=[os.path.join(RAW, r"好衣库-OK\2026.8-好衣库-HYK002")], extra={},
        baselines=["好衣库_好衣库_秋水伊人_HYK001_2026年08月-2026年08月_账单整理.xlsx"]),
    # 多店一簿：文件名不含店铺
    "akc": dict(platform="akc", shop="AKC003", s=S, e=E,
        files=[os.path.join(RAW, r"爱库存-OK\2026.8-爱库存-AKC003")], extra={},
        baselines=["爱库存_2026年08月-2026年08月_账单整理.xlsx"]),
    "vip1": dict(platform="vip", shop="VIP00101", s="2026-07-22", e="2026-07-31",
        files=[os.path.join(RAW, r"唯品会-OK\2026.07.22-2026.07.31")], extra={},
        baselines=[os.path.join("唯品会", "唯品会_2026年0722-2026年0731唯品中国_账单整理.xlsx")]),
    "vip2": dict(platform="vip", shop="VIP00101", s="2026-08-01", e="2026-08-10",
        files=[os.path.join(RAW, r"唯品会-OK\2026.08.01-2026.08.10")], extra={},
        baselines=[os.path.join("唯品会", "唯品会_2026年0801-2026年0810唯品中国_账单整理.xlsx")]),
    "vip3": dict(platform="vip", shop="VIP00101", s="2026-08-11", e="2026-08-21",
        files=[os.path.join(RAW, r"唯品会-OK\2026.08.11-2026.08.21")], extra={},
        baselines=[os.path.join("唯品会", "唯品会_2026年0811-2026年0821唯品中国_账单整理.xlsx")]),
}


def read_book(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb}
    wb.close()
    return out


def num_cols(rows):
    if not rows:
        return {}
    sums = {}
    for j, h in enumerate(rows[0]):
        vals = [r[j] for r in rows[1:] if j < len(r) and isinstance(r[j], (int, float))]
        if vals:
            sums[f"{h}#{j}"] = round(sum(vals), 2)
    return sums


def cmp_sheet(name, got_rows, base_rows, report):
    gh = list(got_rows[0]) if got_rows else []
    bh = list(base_rows[0]) if base_rows else []
    if gh != bh:
        report.append(f"  [HEADER] {name}: got={gh}\n           base={bh}")
    if len(got_rows) != len(base_rows):
        report.append(f"  [ROWS] {name}: got={len(got_rows)-1} base={len(base_rows)-1}")
    gs, bs = num_cols(got_rows), num_cols(base_rows)
    for k in sorted(set(gs) | set(bs)):
        g, b = gs.get(k), bs.get(k)
        if g is None or b is None or abs(g - b) > 0.011:
            report.append(f"  [SUM] {name}.{k}: got={g} base={b}")


def main():
    names = sys.argv[1:] or [k for k in CASES if not k.startswith("vip")]
    all_ok = True
    for name in names:
        c = CASES[name]
        print(f"== {name} {c['shop']} ==", flush=True)
        try:
            res = pipeline.run_bill(platform=c["platform"], shop_code=c["shop"],
                period=P, s_date=c["s"], e_date=c["e"], file_paths=c["files"], extra_files=c["extra"])
        except Exception as exc:
            all_ok = False
            print(f"  [EXC] {type(exc).__name__}: {str(exc)[:300]}", flush=True)
            d = getattr(exc, "detail", None)
            if d: print("  detail:", str(d)[:500], flush=True)
            continue
        print("  status:", res.status, "|", res.message, flush=True)
        if res.status != "success":
            all_ok = False
            continue
        files = res.data.get("exported_files") or []
        print("  exported:", [os.path.basename(f) for f in files], flush=True)
        report = []
        for bname in c["baselines"]:
            bpath = os.path.join(BASE, bname)
            def _norm(n):
                # 基准可能被人工改名（如「账单整理1.xlsx」），剥扩展名前尾部数字再比
                stem, ext = os.path.splitext(os.path.basename(n))
                return stem.rstrip("0123456789") + ext
            match = [f for f in files if _norm(f) == _norm(bname)]
            if not match:
                report.append(f"  [MISSING] 未导出基准同名文件: {bname}")
                continue
            got = read_book(match[0]); base = read_book(bpath)
            if list(got) != list(base):
                report.append(f"  [SHEETS] {os.path.basename(bname)}: got={list(got)} base={list(base)}")
            for sh in base:
                if sh in got:
                    cmp_sheet(sh, got[sh], base[sh], report)
                else:
                    report.append(f"  [SHEET-MISS] {sh}")
        if report:
            all_ok = False
            print("\n".join(report[:60]), flush=True)
        else:
            print("  [OK] 与基准逐sheet/列头/行数/数值合计一致", flush=True)
    print("ALL_OK" if all_ok else "HAS_DIFF", flush=True)


if __name__ == "__main__":
    main()
