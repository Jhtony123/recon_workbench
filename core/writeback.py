"""结果回传数据库（承接 look_sku.update_sql，2026-11-18 启用）。

回传内容：把「已匹配的巨益单号」（SKU 对账产物 oms_matched 底稿）按 OMS 单据 id
写回 ADB mid_jedz_datas，标记 bill_state='已对账'、bill_period=中文账期标签。
SQL 口径与影刀原脚本完全一致：
    UPDATE mid_jedz_datas SET bill_state = %s, bill_period = %s WHERE id = %s

相对原脚本的优化适配：
  · 原 `except: pass` 吞掉全部异常 → 逐批捕获并聚合失败清单，
    Job summary 如实报「成功 N / 失败 M」，失败批次留守不迁移
  · 原每文件独立连接 + executemany(1000/批) + commit → 保留分批 executemany
    与显式 commit/rollback，连接改 contextmanager（db.adb_connection），
    异常路径也保证关闭；批失败 rollback 后继续其它批，不静默成功
  · 凭据走环境变量（core.config），代码内无明文口令（原脚本 5+ 处明文废弃）
  · dry_run 预检：只统计 + 生成 SQL 预览，**不连数据库、不写任何行**；
    2026-11-18 用户决策：当前版本只做代码功能，入口默认 dry_run，
    真实落库须显式 dry_run=0 且经用户确认
  · 原「成功后移动文件到 执行成功/」→ 由 pipeline.run_writeback 用
    artifacts.migrate_sku_batch 迁移 已匹配成本→已回传数据库（同盘原子 move）
"""
from __future__ import annotations

import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd

from .config import TABLE_JEDZ, period_label
from .db import WRITEBACK_BATCH_SIZE, adb_connection
from .errors import WritebackError

MARK_STATE = "已对账"
MAX_WORKERS = 4

#: 回传 SQL（与影刀 look_sku.update_sql 同口径；参数化防注入）
WRITEBACK_SQL = (
    f"UPDATE {TABLE_JEDZ} SET bill_state = %s, bill_period = %s WHERE id = %s"
)


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
    bill_period_label: str = ""
    sql_preview: list[str] = field(default_factory=list)

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
            "bill_period_label": self.bill_period_label,
            "sql_preview": self.sql_preview[:5],
        }


def prepare_rows(oms_matched: pd.DataFrame, *, period: str) -> list[dict]:
    """从「已匹配的巨益单号」表提取回传载荷（id / 账单周期 / 数量）。

    只取 id 与数量；账单周期优先沿用底稿里的中文标签（match 阶段
    build_oms_writeback 统一赋值 period_cn，VIP 带日），缺失才回落 period。
    id 去重保序（原脚本按 id 列表逐条 UPDATE，重复 id 幂等但浪费往返）。
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


def resolve_label(rows: list[dict], *, period: str,
                  s_date: str = "", e_date: str = "") -> str:
    """回传 bill_period 的中文账期标签。

    优先取底稿「账单周期」（影刀原脚本 df['账单周期'].iloc[0] 同口径；
    VIP 为带日标签 2026年1001-2026年1010），缺失回落 period_label。
    生产库实测存的就是中文格式（2026-11-13 查得）；写成 'YYYY-MM'
    会让同一账期在库里裂成两种写法，按 bill_period 统计会漏。
    """
    for row in rows or []:
        v = str(row.get("账单周期") or "").strip()
        if v:
            return v
    return period_label(period, s_date=s_date, e_date=e_date) or period


def sql_preview(rows: list[dict], label: str, *, limit: int = 5) -> list[str]:
    """dry_run 预检用：生成前 N 条等价 SQL 文本（只读展示，不执行）。"""
    out: list[str] = []
    for row in (rows or [])[:limit]:
        oms_id = str(row.get("id") or "").strip()
        if not oms_id:
            continue
        esc = lambda s: str(s).replace("'", "''")  # noqa: E731
        out.append(
            f"UPDATE {TABLE_JEDZ} SET bill_state = '{MARK_STATE}', "
            f"bill_period = '{esc(label)}' WHERE id = '{esc(oms_id)}';"
        )
    return out


def writeback(
    rows: list[dict],
    *,
    period: str,
    s_date: str = "",
    e_date: str = "",
    dry_run: bool = True,
    progress=None,
) -> WritebackResult:
    """执行回传。rows 为空时返回 skipped（正常场景，不算失败）。

    dry_run=True（缺省）：只统计 + SQL 预览，不连数据库。
    dry_run=False：分批 executemany + commit（影刀 1000/批口径），
    批失败 rollback 并计入失败清单，连接级失败整批计失败但不中断其它批。
    """
    t0 = _time.time()
    res = WritebackResult(total=len(rows or []), dry_run=dry_run)
    label = resolve_label(rows, period=period, s_date=s_date, e_date=e_date)
    res.bill_period_label = label

    if not rows:
        res.status = "skipped"
        res.message = "没有需要回传的已匹配单据"
        res.duration_sec = _time.time() - t0
        return res

    res.sql_preview = sql_preview(rows, label)

    if dry_run:
        res.ok = len(rows)
        res.status = "success"
        res.message = (
            f"预检通过：{len(rows)} 条待标记为「{MARK_STATE}」，"
            f"对账周期将写为「{label}」（dry-run，未连接/未写入数据库）"
        )
        res.duration_sec = _time.time() - t0
        return res

    lock = threading.Lock()
    done = 0
    total = len(rows)

    def _run_batch(batch: list[dict]) -> tuple[int, list[dict]]:
        """一批 = 一个连接 + executemany + commit（原脚本口径，失败 rollback）。"""
        params = [
            (MARK_STATE, label, str(row.get("id") or "").strip())
            for row in batch
            if str(row.get("id") or "").strip()
        ]
        if not params:
            return 0, []
        try:
            with adb_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.executemany(WRITEBACK_SQL, params)
                    conn.commit()
                    return len(params), []
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    return 0, [
                        {"id": p[2],
                         "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                        for p in params
                    ]
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
        except Exception as exc:
            # 连接级失败：整批计为失败，但不中断其它批
            return 0, [
                {"id": str(row.get("id") or ""),
                 "error": f"connection: {type(exc).__name__}: {str(exc)[:120]}"}
                for row in batch
            ]

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
        res.message = f"回传完成：{res.ok} 条已标记为「{MARK_STATE}」（账期「{label}」）"
    elif res.ok == 0:
        res.status = "failed"
        res.message = f"回传全部失败（{res.failed} 条），请检查数据库连接与权限"
    else:
        res.status = "partial"
        res.message = f"回传部分成功：成功 {res.ok} 条 / 失败 {res.failed} 条"
    return res