# -*- coding: utf-8 -*-
"""回传数据库模块验证（2026-11-18）。

纪律：本脚本**绝不连接真实数据库**——
  · dry-run 路径：monkeypatch adb_connection 为「一旦被调用即失败」，证明不连库；
  · 真实路径：用 FakeConn 捕获 executemany/commit/rollback，逐参数核对 SQL 口径
    与影刀 look_sku.update_sql 一致（UPDATE mid_jedz_datas SET bill_state,bill_period WHERE id）。
  · run_writeback 端到端：临时 results 根 + 伪造 sku 批次底稿，验证
    dry-run 不迁移 / 真实回传成功迁移 已回传数据库/ / 失败留守。

用法：python scripts/verify_writeback.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, r"D:\DesktopAgent")  # 宿主 extensions.paths（storage 依赖）

import pandas as pd  # noqa: E402

from core import artifacts, joblog, pipeline, writeback as wb  # noqa: E402
from core import db as core_db  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[OK] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


# ---------------------------------------------------------------- 单元：prepare/label/preview

df = pd.DataFrame({
    "id": ["1001", "1002", "1001", " ", None],
    "账单周期": ["2026年10月-2026年10月"] * 5,
    "数量": [1, 2, 1, 0, 0],
})
rows = wb.prepare_rows(df, period="2026-10")
check("prepare_rows 去重+去空id", len(rows) == 2 and rows[0]["id"] == "1001")
check("prepare_rows 空表返回[]", wb.prepare_rows(pd.DataFrame(), period="2026-10") == [])

label = wb.resolve_label(rows, period="2026-10")
check("resolve_label 沿用底稿中文标签", label == "2026年10月-2026年10月", label)
vip_rows = [{"id": "9", "账单周期": "2026年1001-2026年1010", "数量": 1}]
check("resolve_label VIP带日标签", wb.resolve_label(vip_rows, period="2026-10") == "2026年1001-2026年1010")
check("resolve_label 缺失回落period_label",
      wb.resolve_label([{"id": "9", "账单周期": "", "数量": 1}], period="2026-10") == "2026年10月-2026年10月")

prev = wb.sql_preview(rows, label)
check("sql_preview 文本口径", prev and prev[0] ==
      "UPDATE mid_jedz_datas SET bill_state = '已对账', bill_period = '2026年10月-2026年10月' WHERE id = '1001';",
      str(prev))
check("WRITEBACK_SQL 与影刀同口径",
      wb.WRITEBACK_SQL == "UPDATE mid_jedz_datas SET bill_state = %s, bill_period = %s WHERE id = %s")

# ---------------------------------------------------------------- dry-run 不连库

def _boom(*a, **k):
    raise AssertionError("dry-run 不应连接数据库！")

_orig_conn = core_db.adb_connection
core_db.adb_connection = _boom
wb.adb_connection = _boom
try:
    res = wb.writeback(rows, period="2026-10", dry_run=True)
    check("dry_run 成功且不连库", res.status == "success" and res.ok == 2 and res.dry_run)
    check("dry_run 消息含未写入声明", "未连接/未写入数据库" in res.message, res.message)
    res_empty = wb.writeback([], period="2026-10", dry_run=True)
    check("空rows=skipped", res_empty.status == "skipped")
finally:
    core_db.adb_connection = _orig_conn
    wb.adb_connection = _orig_conn

# ---------------------------------------------------------------- 真实路径：FakeConn 捕获

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def executemany(self, sql, params):
        self.conn.calls.append((sql, list(params)))
        if self.conn.fail_batch:
            raise RuntimeError("injected batch failure")

    def close(self):
        pass


class FakeConn:
    def __init__(self, fail_batch=False):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_batch = fail_batch

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


conns = []


def fake_connect_factory(fail_batch=False):
    from contextlib import contextmanager

    @contextmanager
    def _factory():
        c = FakeConn(fail_batch=fail_batch)
        conns.append(c)
        try:
            yield c
        finally:
            c.close()
    return _factory


# 2500 行 → 3 批（1000/1000/500），4 并发
big = [{"id": str(i), "账单周期": "2026年10月-2026年10月", "数量": 1} for i in range(2500)]
conns.clear()
wb.adb_connection = fake_connect_factory()
try:
    res = wb.writeback(big, period="2026-10", dry_run=False)
finally:
    wb.adb_connection = _orig_conn
check("真实路径 2500 行全成功", res.status == "success" and res.ok == 2500 and res.failed == 0,
      res.message)
check("分批 1000/批 → 3 连接 3 commit", len(conns) == 3 and all(c.commits == 1 for c in conns),
      f"conns={len(conns)}")
sizes = sorted(len(c.calls[0][1]) for c in conns)
check("批大小 500/1000/1000", sizes == [500, 1000, 1000], str(sizes))
sql0, params0 = conns[0].calls[0]
check("executemany SQL 模板一致", sql0 == wb.WRITEBACK_SQL)
check("参数三元组 (已对账,中文账期,id)",
      all(p[0] == "已对账" and p[1] == "2026年10月-2026年10月" and p[2].isdigit() for p in params0))
ids_all = sorted(int(p[2]) for c in conns for p in c.calls[0][1])
check("id 全覆盖无重复", ids_all == list(range(2500)))

# 批失败 → rollback + 失败清单，不静默成功
conns.clear()
wb.adb_connection = fake_connect_factory(fail_batch=True)
try:
    res = wb.writeback(big[:1500], period="2026-10", dry_run=False)
finally:
    wb.adb_connection = _orig_conn
check("批失败 rollback", all(c.rollbacks == 1 and c.commits == 0 for c in conns))
check("批失败终态 failed + 明细", res.status == "failed" and res.failed == 1500
      and len(res.failed_detail) == 1500, res.message)

# 连接级失败 → 整批计失败不抛
def _conn_boom():
    raise RuntimeError("injected connect failure")

wb.adb_connection = _conn_boom
try:
    res = wb.writeback(big[:10], period="2026-10", dry_run=False)
finally:
    wb.adb_connection = _orig_conn
check("连接失败聚合为failed", res.status == "failed" and res.failed == 10
      and all("connection" in f["error"] for f in res.failed_detail))

# ---------------------------------------------------------------- run_writeback 端到端（临时 results 根）

tmp = Path(tempfile.mkdtemp(prefix="wb_verify_"))
_orig_output_dir = artifacts.output_dir
artifacts.output_dir = lambda: tmp
_orig_joblog = joblog.save_job_log
logs = []
joblog.save_job_log = lambda *a, **k: logs.append((a, k)) or {}
pipeline.joblog.save_job_log = joblog.save_job_log


def make_batch(name, state, oms_rows, platform="douyin", shop="DY001"):
    root = artifacts.sku_root("2026-10", state)
    d = root / name
    (d / "底稿").mkdir(parents=True, exist_ok=True)
    (d / f"{name}_账单匹配结果.xlsx").write_bytes(b"fake-xlsx")
    man = {"batch_id": f"match-{name}", "platform": platform, "shop_code": shop,
           "period": "2026-10", "status": "success", "files": {}}
    (d / "底稿" / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    pd.DataFrame({"id": ["1"], "账单周期": ["x"], "数量": [1]}).to_csv(
        d / "底稿" / "match_detail.csv", index=False, encoding="utf-8-sig")
    oms = pd.DataFrame(oms_rows, columns=["id", "账单周期", "数量"])
    oms.to_csv(d / "底稿" / "oms_matched.csv", index=False, encoding="utf-8-sig")
    return d


try:
    d1 = make_batch("2026-10-抖音-测试店-DY001", artifacts.SKU_STATE_COSTED,
                    [["2001", "2026年10月-2026年10月", 1], ["2002", "2026年10月-2026年10月", 2]])
    d2 = make_batch("2026-10-抖音-空店-DY002", artifacts.SKU_STATE_PENDING_COST, [])

    # dry-run：不连库（adb_connection 仍为 boom 检测器）、不迁移
    wb.adb_connection = _boom
    core_db.adb_connection = _boom
    try:
        r = pipeline.run_writeback(period="2026-10", dry_run=True)
    finally:
        wb.adb_connection = _orig_conn
        core_db.adb_connection = _orig_conn
    check("run_writeback dry-run success", r.status == "success", r.message)
    check("dry-run 统计 2 条", r.data["total"] == 2 and r.data["ok"] == 2, str(r.data["total"]))
    check("dry-run 不迁移", d1.is_dir() and d2.is_dir() and not r.data["migrated"])
    check("dry-run stored_in_db=False", r.data["stored_in_db"] is False)
    check("dry-run SQL预览非空", len(r.data["sql_preview"]) >= 1)
    check("空回传表批次跳过", any(u["status"] == "skipped" for u in r.data["unit_reports"]))

    # 真实路径（FakeConn）：成功 → 迁移 已回传数据库/
    conns.clear()
    wb.adb_connection = fake_connect_factory()
    try:
        r = pipeline.run_writeback(period="2026-10", dry_run=False)
    finally:
        wb.adb_connection = _orig_conn
    check("真实回传 success", r.status == "success" and r.data["ok"] == 2, r.message)
    dst = artifacts.sku_root("2026-10", artifacts.SKU_STATE_WRITTEN_BACK) / d1.name
    check("成功批次迁移 已回传数据库/", dst.is_dir() and not d1.exists(), str(dst))
    check("空批次留守不迁移", d2.is_dir())
    check("真实回传 stored_in_db=True", r.data["stored_in_db"] is True)
    check("joblog 留痕 writeback", any(k.get("stage") == "writeback" for _, k in logs))

    # 指定批次 + 失败注入 → 留守
    d3 = make_batch("2026-10-抖音-失败店-DY003", artifacts.SKU_STATE_COSTED,
                    [["3001", "2026年10月-2026年10月", 1]])
    wb.adb_connection = fake_connect_factory(fail_batch=True)
    try:
        r = pipeline.run_writeback(period="2026-10", bill_batch_id=d3.name, dry_run=False)
    finally:
        wb.adb_connection = _orig_conn
    check("失败批次终态 failed", r.status == "failed", r.message)
    check("失败批次留守", d3.is_dir() and not r.data["migrated"])

    # period 必填 / 无批次可读错误
    try:
        pipeline.run_writeback(period="")
        check("period必填抛错", False)
    except Exception as exc:
        check("period必填抛错", "period" in str(exc))
finally:
    artifacts.output_dir = _orig_output_dir
    joblog.save_job_log = _orig_joblog
    pipeline.joblog.save_job_log = _orig_joblog
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 入口接线静态检查

check("ACTIVE_STAGES 含 writeback", "writeback" in pipeline.ACTIVE_STAGES)
caps = {c["key"]: c["status"] for c in pipeline.capability_status()}
check("capability_status writeback=active", caps.get("writeback") == "active", str(caps))
check("chuanyun 已 active（2026-11-18 启用）", caps.get("chuanyun") == "active")

import actions  # noqa: E402
import inspect  # noqa: E402
sig = inspect.signature(actions.writeback)
check("actions.writeback 参数齐全",
      all(p in sig.parameters for p in ("period", "bill_batch_id", "dry_run")))
src = inspect.getsource(actions.start_recon)
check("start_recon writeback 缺省 dry_run=1", '"dry_run": "0" if' in src)

print(f"\n==== verify_writeback: PASS={PASS} FAIL={FAIL} ====")
sys.exit(1 if FAIL else 0)