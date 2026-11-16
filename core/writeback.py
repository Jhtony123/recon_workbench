"""结果回传数据库（承接 look_sku.update_sql）。

回传内容：把「已匹配的巨益单号」写回 ADB mid_jedz_datas，
标记 bill_state='已对账'、bill_period=账期。

相对原脚本的修复：
  · 原 `except: pass` 吞掉全部异常 → 这里逐行/逐批捕获并聚合失败清单，
    Job summary 如实报「成功 N / 失败 M」，管理页回传中心可见失败明细
  · 原多线程 8 并发 + 每文件独立连接 → 保留并发但统一连接池管理，
    连接失败整体判定不可用（WritebackError），不再静默成功
  · 支持 dry_run（只统计不落库），管理页可先预检
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd

from .config import TABLE_JEDZ, period_label
from .db import WRITEBACK_BATCH_SIZE, adb_cursor
from .errors import WritebackError

MARK_STATE = "已对账"
MAX_WORKERS = 4


@dataclass
class WritebackResult:
    total: int = 0
    ok: int = 0
    failed: int = 0
    failed_detail: list[dict] = field(default_factory=list)
    dry_run: bool = False
    status: str = "skipped"
    message: str = ""
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "failed_detail": self.failed_detail[:200],
            "dry_run": self.dry_run,
            "status": self.status,
            "message": self.message,
            "duration_sec": round(self.duration_sec, 2),
        }


def prepare_rows(oms_matched: pd.DataFrame, *, period: str) -> list[dict]:
    """从「已匹配的巨益单号」表提取回传载荷。

    只取 id 与数量；账单周期由 writeback() 统一生成中文标签，
    不采信明细里的 'YYYY-MM'（与库内既有 bill_period 格式不一致）。
    """
    if oms_matched is None or oms_matched.empty or "id" not in oms_matched.columns:
        return []
    df = oms_matched.copy()
    df["id"] = df["id"].astype("string").fillna("").str.strip()
    df = df[df["id"] != ""]
    if df.empty:
        return []
    if "账单周期" not in df.columns or df["账单周期"].astype("string").str.strip().eq("").all():
        df["账单周期"] = period
    if "数量" not in df.columns:
        df["数量"] = 0
    df["数量"] = pd.to_numeric(df["数量"], errors="coerce").fillna(0).astype("int64")
    df = df.drop_duplicates(subset=["id"])
    return df.loc[:, ["id", "账单周期", "数量"]].to_dict(orient="records")


def writeback(
    rows: list[dict],
    *,
    period: str,
    s_date: str = "",
    e_date: str = "",
    dry_run: bool = False,
    progress=None,
) -> WritebackResult:
    """执行回传。rows 为空时返回 skipped（正常场景，不算失败）。

    bill_period 写的是**中文账期标签**（如 '2026年10月-2026年10月'），
    与库内既有值同格式 —— 生产库实测存的就是这种格式（2026-11-13 查得）。
    若写成 'YYYY-MM' 会让同一账期在库里裂成两种写法，按 bill_period 统计会漏。
    """
    import time as _time

    t0 = _time.time()
    res = WritebackResult(total=len(rows or []), dry_run=dry_run)
    label = period_label(period, s_date=s_date, e_date=e_date) or period

    if not rows:
        res.status = "skipped"
        res.message = "没有需要回传的已匹配单据"
        res.duration_sec = _time.time() - t0
        return res

    if dry_run:
        res.ok = len(rows)
        res.status = "success"
        res.message = (
            f"预检通过：{len(rows)} 条待标记为「{MARK_STATE}」，"
            f"对账周期将写为「{label}」（未写入数据库）"
        )
        res.duration_sec = _time.time() - t0
        return res

    sql = (
        f"UPDATE {TABLE_JEDZ} SET bill_state = %s, bill_period = %s "
        "WHERE id = %s"
    )

    lock = threading.Lock()
    done = 0
    total = len(rows)

    def _run_batch(batch: list[dict]) -> tuple[int, list[dict]]:
        ok = 0
        fails: list[dict] = []
        try:
            with adb_cursor() as cursor:
                for row in batch:
                    oms_id = str(row.get("id") or "").strip()
                    if not oms_id:
                        continue
                    try:
                        cursor.execute(
                            sql,
                            (MARK_STATE, label or str(row.get("账单周期") or ""), oms_id),
                        )
                        ok += 1
                    except Exception as exc:
                        fails.append(
                            {
                                "id": oms_id,
                                "order_no": str(row.get("订单编号") or ""),
                                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                            }
                        )
        except Exception as exc:
            # 连接级失败：整批计为失败，但不中断其它批
            for row in batch:
                fails.append(
                    {
                        "id": str(row.get("id") or ""),
                        "error": f"connection: {type(exc).__name__}: {str(exc)[:120]}",
                    }
                )
            return 0, fails
        return ok, fails

    batches = [
        rows[i : i + WRITEBACK_BATCH_SIZE]
        for i in range(0, len(rows), WRITEBACK_BATCH_SIZE)
    ]

    try:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(batches)))) as pool:
            futures = [pool.submit(_run_batch, b) for b in batches]
            for fut in as_completed(futures):
                try:
                    ok, fails = fut.result()
                except Exception as exc:
                    ok, fails = 0, [{"error": f"{type(exc).__name__}: {str(exc)[:160]}"}]
                with lock:
                    res.ok += ok
                    res.failed += len(fails)
                    res.failed_detail.extend(fails)
                    done += ok + len(fails)
                    if progress and total:
                        progress(int(done * 100 / total), f"回传进度 {done}/{total}")
    except Exception as exc:
        raise WritebackError(
            f"回传数据库失败：{type(exc).__name__}",
            detail={"ok": res.ok, "failed": res.failed, "raw": str(exc)[:300]},
        ) from exc

    res.duration_sec = _time.time() - t0
    if res.failed == 0:
        res.status = "success"
        res.message = f"回传完成：{res.ok} 条已标记为「{MARK_STATE}」"
    elif res.ok == 0:
        res.status = "failed"
        res.message = f"回传全部失败（{res.failed} 条），请检查数据库连接与权限"
    else:
        res.status = "partial"
        res.message = f"回传部分成功：成功 {res.ok} 条 / 失败 {res.failed} 条"
    return res
