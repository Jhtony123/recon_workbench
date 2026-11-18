"""本机持久化门面（2026-11-14 方案改版后收窄）。

【方案改版】数据库**只存店铺映射**（ext_recon_workbench_shop_map）。
原本由本模块统一落库的四类数据改由文件承担，本文件保留同名入口做**转发**，
这样 pipeline / actions 里的调用点不必到处改，也避免遗留代码静默写库：

  · 平台解析规则  -> core/rules_store.py   （{plugin}/rules/{platform}.json）
  · 作业留痕      -> core/joblog.py        （data/.../logs/jobs.jsonl）
  · 账单整理结果  -> core/artifacts.py     （data/.../results/{批次}/*.parquet + xlsx）
  · SKU对账/成本/回传结果 -> 占位暂缓，**不再有任何落库入口**

为什么保留转发而不是让调用方直接 import 新模块：
  转发函数在被调用时会经过 storage 的表名白名单，任何「想往旧表写」的路径都会显式报错，
  等于把方案约束变成了运行时闸门，而不只是注释里的约定。

金额口径：库内已无金额列（店铺映射只有编码与名称）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Sequence

import pandas as pd

from storage import upsert_row, write_connection

from . import artifacts, joblog, rules_store
from .config import PLUGIN_NAME
from .money import fen_to_float

P = "ext_recon_workbench_"

#: **唯一**保留的库表
T_SHOP = P + "shop_map"

# 店铺映射的自然键（与 schema.sql 的 UNIQUE 约束对齐）。
NATURAL_KEYS: dict[str, list[str]] = {
    T_SHOP: ["version", "shop_code"],
}


def new_batch_id(prefix: str = "batch") -> str:
    """批次号：{prefix}-{YYYYMMDDHHMMSS}-{短uuid}。"""
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- 通用写入


def _clean(value: Any) -> Any:
    """SQLite 可接受的标量：NaN/NaT → None，numpy 标量 → python。"""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return value
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            return str(value)
    return value


def insert_rows(
    table: str,
    rows: Iterable[dict[str, Any]],
    *,
    unique_keys: Sequence[str] | None = None,
    replace: bool = True,
) -> int:
    """批量写入（仅店铺映射表可用）。空行跳过；返回实际写入行数。

    table 不在 storage.ALLOWED_TABLES 里时，storage 会抛 ValueError 并点名
    「该表已在 2026-11-14 方案改版中退役」—— 这是有意设计的硬闸门。
    """
    keys = list(unique_keys) if unique_keys else (NATURAL_KEYS.get(table, []) if replace else [])
    n = 0
    for row in rows:
        if not isinstance(row, dict) or not row:
            continue
        cleaned = {k: _clean(v) for k, v in row.items()}
        usable = [k for k in keys if k in cleaned] if keys else []
        upsert_row(table, cleaned, plugin_name=PLUGIN_NAME, unique_keys=usable)
        n += 1
    return n


def _query(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    with write_connection(plugin_name=PLUGIN_NAME) as conn:
        conn.row_factory = None
        cur = conn.execute(sql, tuple(params))
        cols = [d[0] for d in (cur.description or [])]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------- 店铺映射


def query_shops(*, keyword: str = "", limit: int = 500, active_only: bool = True) -> list[dict]:
    """店铺清单（按编码或名称模糊筛选）。"""
    where: list[str] = []
    params: list[Any] = []
    if active_only:
        where.append("active = 1")
    kw = str(keyword or "").strip()
    if kw:
        # LIKE 参数化：店铺编码/名称来自导入的 Excel，直接拼串会把 % _ 当通配符
        where.append("(shop_code LIKE ? OR shop_name LIKE ? OR chuanyun_name LIKE ?)")
        params.extend([f"%{kw}%"] * 3)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit or 500))
    return _query(
        f"SELECT shop_code, shop_id, shop_name, platform, chuanyun_name,"
        f" active, source_file, version, imported_at"
        f" FROM {T_SHOP}{clause} ORDER BY shop_code LIMIT ?",
        params,
    )


def save_shops(rows: Iterable[dict[str, Any]]) -> int:
    """写店铺映射（按 version+shop_code 覆盖）。"""
    return insert_rows(T_SHOP, rows)


def shop_versions() -> list[dict]:
    """店铺表导入版本留痕。"""
    return _query(
        f"SELECT version, COUNT(*) AS n, MAX(imported_at) AS imported_at,"
        f" SUM(active) AS active_n, MAX(source_file) AS source_file"
        f" FROM {T_SHOP} GROUP BY version ORDER BY version DESC LIMIT 20"
    )


# ---------------------------------------------------------------- 规则（转发到文件）


def load_rules_map(platform: str) -> dict[str, dict[str, str]]:
    """{rule_kind: {rule_key: rule_value}} —— 数据源为 rules/{platform}.json。"""
    return rules_store.load_rules_map(platform)


def query_rules(platform: str = "", *, active_only: bool = True, limit: int = 500) -> list[dict]:
    return rules_store.query_rules(platform, active_only=active_only, limit=limit)


def upsert_rule(
    platform: str,
    rule_kind: str,
    rule_key: str,
    rule_value: str,
    *,
    note: str = "",
    active: bool = True,
) -> None:
    rules_store.upsert_rule(platform, rule_kind, rule_key, rule_value, note=note, active=active)


def delete_rule(platform: str, rule_kind: str, rule_key: str) -> None:
    rules_store.delete_rule(platform, rule_kind, rule_key)


# ---------------------------------------------------------------- 作业留痕（转发到 JSONL）


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
) -> None:
    """作业留痕 —— 写 data/extensions/recon_workbench/logs/jobs.jsonl（不入数据库）。"""
    joblog.save_job_log(
        batch_id, stage=stage, status=status, message=message, platform=platform,
        shop_code=shop_code, period=period, bill_rows=bill_rows,
        matched_rows=matched_rows, unmatched_rows=unmatched_rows,
        diff_fen=diff_fen, duration_sec=duration_sec,
    )


def query_job_log(limit: int = 60) -> list[dict]:
    return joblog.read_records(limit)


# ---------------------------------------------------------------- 账单批次（转发到 artifacts）


def query_bill_batches(
    *,
    period: str = "",
    platform: str = "",
    shop_code: str = "",
    limit: int = 50,
) -> list[dict]:
    """账单整理批次清单（扫 results/ 目录，新→旧）。"""
    return [
        artifacts.batch_rows(b)
        for b in artifacts.list_batches(
            period=period, platform=platform, shop_code=shop_code, limit=limit)
    ]


def query_bill_rows(
    *,
    batch_id: str = "",
    period: str = "",
    platform: str = "",
    shop_code: str = "",
    limit: int = 200,
) -> list[dict]:
    """账单标准行（读 parquet，金额转元视图）。

    **只返回单个批次**：未指定 batch_id 时取该范围内最新一次运行。
    每次整理都新建批次目录，不收敛会把历次结果叠在一起 —— 这是旧库版本踩过的坑
    （DY014 重跑十余次后管理页成本卡片显示 131,014.52 元、真值 1,800.26 元）。
    """
    df = artifacts.read_detail(
        batch_id=batch_id, period=period, platform=platform, shop_code=shop_code)
    if df.empty:
        return []
    n = int(limit or 200)
    out = df.head(n) if n > 0 else df
    rows = out.to_dict(orient="records")
    for r in rows:
        for c in ("receipt_fen", "refund_fen", "net_fen"):
            if c in r:
                r[c.replace("_fen", "_yuan")] = fen_to_float(r.get(c))
    return rows


def bill_batch_detail(batch_id: str = "", **kw: Any) -> dict[str, Any]:
    """单个批次的完整摘要（含产物路径与合计）。"""
    b = artifacts.resolve_batch(batch_id=batch_id, **kw)
    if not b:
        return {}
    return artifacts.batch_rows(b)


# ---------------------------------------------------------------- 总览与数据态


def overview(days: int = 90) -> dict[str, Any]:
    """管理页总览。

    总览不展示「匹配率 / 未匹配差异」聚合指标——跨批次聚合会把重跑结果叠在一起
    （批次收敛纪律），恒 0 或虚高都会被误读。这里如实给出**账单整理覆盖**与
    **能力开放状态**；SKU/成本明细按批次下钻（sku_batches + 底稿）。
    """
    from .pipeline import capability_status

    jobs = joblog.read_records(20)
    st = joblog.stats()
    cov = artifacts.coverage(limit=200)

    return {
        "latest_job": jobs[0] if jobs else None,
        "job_count": int(st.get("records") or 0),
        "totals": {
            "bill_rows": int(cov.get("detail_rows") or 0),
            "bill_batches": int(cov.get("batch_count") or 0),
            "goods_rows": int(cov.get("goods_rows") or 0),
            "shop_map": len(query_shops(limit=5000)),
            "rules": rules_store.count_rules(),
            "matched_rows": 0,
            "unmatched_rows": 0,
            "match_rate_pct": None,
        },
        "deferred": {
            "enabled": False,
            "reason": "数据库回传 / 氚云对接按方案改版暂缓开放（账单整理/SKU对账/成本匹配已启用）",
            "note": "总览不做跨批次匹配率聚合（防重跑叠加误读）；SKU/成本结果按批次下钻。",
        },
        "capabilities": capability_status(),
        "platforms": cov.get("platforms") or [],
        "periods": cov.get("periods") or [],
        "recent_batches": [artifacts.batch_rows(b) for b in
                           artifacts.list_batches(limit=10)],
        "results_dir": cov.get("results_dir") or "",
        "job_log_file": st.get("log_file") or "",
        "days": days,
    }


def data_status() -> dict[str, Any]:
    """插件数据态：库表只剩店铺映射，其余能力看文件与占位状态。"""
    shops = query_shops(limit=5000)
    cov = artifacts.coverage(limit=500)
    st = joblog.read_records(limit=0)

    bill_platforms = sorted({str(r.get("platform")) for r in cov.get("platforms") or []
                             if r.get("platform")})
    periods = [p.get("period") for p in cov.get("periods") or [] if p.get("period")]

    return {
        "counts": {
            # 数据库里只有这一张表
            "shop_map": len(shops),
            # 以下为文件态，不是库表行数
            "bill_batches": int(cov.get("batch_count") or 0),
            "bill_rows": int(cov.get("detail_rows") or 0),
            "goods_rows": int(cov.get("goods_rows") or 0),
            "rules": rules_store.count_rules(),
            "job_log": len(st),
            # 已退役：恒为 0，保留键名只为让老 UI/老提示不炸
            "oms_rows": 0, "match_detail": 0, "pivot_shop": 0,
            "pivot_product": 0, "writeback_log": 0, "cost_base": 0,
        },
        "db_tables": [T_SHOP],
        "retired_tables_note": (
            "账单整理结果、SKU 对账明细、成本透视、回传留痕、平台规则、成本表、作业留痕"
            "均已移出数据库；结果落 results/ 目录（xlsx+parquet），规则落 rules/*.json，"
            "作业留痕落 logs/jobs.jsonl。"
        ),
        "periods": periods,
        "platforms": bill_platforms,
        "shop_count": len(shops),
        "shop_versions": shop_versions(),
        "cost_versions": [],
        "rule_files": rules_store.list_rule_platforms(),
        "results_dir": cov.get("results_dir") or "",
        "job_log_file": joblog.stats().get("log_file") or "",
    }
