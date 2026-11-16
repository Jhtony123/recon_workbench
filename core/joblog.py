"""作业留痕的 **JSONL 文件**存储（data/extensions/recon_workbench/logs/jobs.jsonl）。

【2026-11-14 方案改版】job_log 从 SQLite 表迁出（用户要求数据库只存店铺映射）。
作业留痕是「过程记录」，不是对账结果：JSONL 追加写天然适合，且能直接 tail/grep，
重启桌面智能体后历史仍可读（这是相对「只放内存」方案的关键优势）。

一行一条记录，字段与旧 ext_recon_workbench_job_log 表**完全一致**，
UI 与 AI 工具的渲染逻辑不必改：
  batch_id, stage, platform, shop_code, period, status, message,
  bill_rows, matched_rows, unmatched_rows, diff_fen, duration_sec, created_at

batch_id 唯一：重复写同批次时**整文件重写**覆盖（旧表是 upsert 语义，这里保持一致）。
文件行数上限 MAX_RECORDS（默认 5000）：超限丢最旧，避免长期运行后文件无限增长。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

LOG_FILE_NAME = "jobs.jsonl"
MAX_RECORDS = 5000

_FIELDS = (
    "batch_id", "stage", "platform", "shop_code", "period", "status", "message",
    "bill_rows", "matched_rows", "unmatched_rows", "diff_fen", "duration_sec",
    "created_at",
)

_LOCK = threading.Lock()


def logs_dir() -> Path:
    from .config import runtime_dir

    d = runtime_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return logs_dir() / LOG_FILE_NAME


def _blank(batch_id: str = "") -> dict[str, Any]:
    rec: dict[str, Any] = {f: "" for f in _FIELDS}
    rec.update({
        "batch_id": batch_id,
        "bill_rows": 0, "matched_rows": 0, "unmatched_rows": 0, "diff_fen": 0,
        "duration_sec": None,
    })
    return rec


def read_records(limit: int = 0) -> list[dict[str, Any]]:
    """读全部留痕（**新→旧**排序，与旧表 ORDER BY id DESC 一致）。

    损坏行跳过而不是抛错：留痕是辅助信息，一行坏数据不该让整个管理页打不开。
    """
    path = log_path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    out.reverse()  # 文件是追加写（旧→新），返回时翻成新→旧
    n = int(limit or 0)
    return out[:n] if n > 0 else out


def save_job_log(
    batch_id: str,
    *,
    stage: str = "",
    status: str = "",
    message: str = "",
    platform: str = "",
    shop_code: str = "",
    period: str = "",
    bill_rows: int = 0,
    matched_rows: int = 0,
    unmatched_rows: int = 0,
    diff_fen: int = 0,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    """写一条作业留痕。batch_id 已存在则覆盖该条（整文件原子重写）。"""
    rec = _blank(str(batch_id or ""))
    rec.update({
        "stage": str(stage or ""),
        "platform": str(platform or ""),
        "shop_code": str(shop_code or ""),
        "period": str(period or ""),
        "status": str(status or ""),
        "message": str(message or "")[:2000],
        "bill_rows": int(bill_rows or 0),
        "matched_rows": int(matched_rows or 0),
        "unmatched_rows": int(unmatched_rows or 0),
        "diff_fen": int(diff_fen or 0),
        "duration_sec": float(duration_sec) if duration_sec is not None else None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    with _LOCK:
        _append_or_replace(rec)
    return rec


def _append_or_replace(rec: dict[str, Any]) -> None:
    """同 batch_id 覆盖，否则追加；超上限截断最旧记录。"""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    bid = str(rec.get("batch_id") or "")

    existing: list[dict[str, Any]] = []
    replaced = False
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(old, dict):
                continue
            if bid and str(old.get("batch_id") or "") == bid:
                existing.append(rec)  # 原位覆盖，保持时间顺序
                replaced = True
            else:
                existing.append(old)

    if not replaced:
        existing.append(rec)

    if len(existing) > MAX_RECORDS:
        existing = existing[-MAX_RECORDS:]

    body = "".join(
        json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in existing
    )
    fd, tmp = tempfile.mkstemp(prefix=".jobs-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        # Windows 并发竞态（2026-11-16 十月双进程实测 WinError 5）：
        # 另一进程/杀毒/索引器瞬时持有目标句柄时 os.replace 会拒绝访问。
        # 留痕是辅助信息：短重试后仍失败则跳过本条，绝不让整理作业因留痕失败。
        last_exc: BaseException | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.15 * (attempt + 1))
        if last_exc is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_records() -> int:
    """清空作业留痕（用户「清空历史」动作）。返回清掉的条数；文件保留为空。"""
    path = log_path()
    with _LOCK:
        n = len(read_records())
        if path.is_file():
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
    return n


def stats() -> dict[str, Any]:
    """留痕统计（管理页「作业与日志」与数据态用）。"""
    recs = read_records()
    total = len(recs)
    bill = sum(int(r.get("bill_rows") or 0) for r in recs)
    matched = sum(int(r.get("matched_rows") or 0) for r in recs)
    unmatched = sum(int(r.get("unmatched_rows") or 0) for r in recs)
    denom = matched + unmatched
    return {
        "log_file": str(log_path()),
        "records": total,
        "bill_rows": bill,
        "matched_rows": matched,
        "unmatched_rows": unmatched,
        "match_rate_pct": round(matched * 100.0 / denom, 2) if denom else None,
        "latest": recs[0] if recs else None,
    }
