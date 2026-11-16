# -*- coding: utf-8 -*-
"""唯品会第三旬（2026.10.11-2026.10.20）单独重跑，带完整 traceback 定位 TypeError。"""
import sys, os, traceback
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")
from core import pipeline

RAW = r"C:\Users\贺杰\Desktop\2026.10月账单\2026年10月原始账单\唯品会-OK\2026.10.11-2026.10.20"
try:
    res = pipeline.run_bill(platform="vip", shop_code="VIP00101", period="2026-10",
        s_date="2026-10-11", e_date="2026-10-20", file_paths=[RAW], extra_files={})
    print("status:", res.status, "|", str(res.message)[:300], flush=True)
    print("exported:", [os.path.basename(f) for f in (res.data.get("exported_files") or [])], flush=True)
except Exception:
    traceback.print_exc()
    print("[EXC-FULL]", flush=True)
