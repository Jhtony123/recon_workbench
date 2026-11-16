"""recon_workbench 单表存储辅助（scaffold template=multi_table → 收窄为单表）。

【2026-11-14 方案改版】**数据库只存店铺映射**（ext_recon_workbench_shop_map）。
其余职责改由文件承担，见 schema.sql 顶部说明与 core/artifacts.py / core/rules_store.py / core/joblog.py。

ALLOWED_TABLES 白名单是硬闸门：即使有代码试图往已废弃的账单/匹配/透视/回传表写数据，
也会在这里被 ValueError 拦下，而不是静默建表。这是「数据库只存店铺映射」的**执行点**，
不只是文档约定 —— 日后重新启用 SKU 对账时，必须先在这里显式加表名（等于一次有意的方案评审）。

upsert：提供 unique_keys 时按键 DELETE 再 INSERT（增量覆盖）。
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from extensions.paths import plugin_db_path

PLUGIN_NAME = "recon_workbench"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# 表名白名单（防 SQL 注入 + 方案闸门）；增表时同步改 schema.sql 与本集合。
ALLOWED_TABLES = frozenset(
    {
        "ext_recon_workbench_shop_map",
    }
)

# 2026-11-14 方案改版后**不再入库**的表名（仅供错误提示与迁移脚本引用）。
# 旧库里若还存在这些表，schema.sql 顶部的 DROP TABLE 会在下次建连时清掉。
RETIRED_TABLES = (
    "ext_recon_workbench_bill_rows",
    "ext_recon_workbench_bill_goods",
    "ext_recon_workbench_oms_rows",
    "ext_recon_workbench_match_detail",
    "ext_recon_workbench_pivot_shop",
    "ext_recon_workbench_pivot_product",
    "ext_recon_workbench_writeback_log",
    "ext_recon_workbench_platform_rules",
    "ext_recon_workbench_cost_base",
    "ext_recon_workbench_job_log",
)


def db_path(plugin_name: str | None = None) -> Path:
    return plugin_db_path(plugin_name or PLUGIN_NAME)


def _load_schema_sql() -> str:
    if SCHEMA_PATH.is_file():
        return SCHEMA_PATH.read_text(encoding="utf-8")
    return ""


@contextmanager
def write_connection(
    path: Path | None = None,
    *,
    plugin_name: str | None = None,
) -> Iterator[sqlite3.Connection]:
    target = path or db_path(plugin_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        sql = _load_schema_sql()
        if sql.strip():
            conn.executescript(sql)
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    sql = _load_schema_sql()
    if sql.strip():
        conn.executescript(sql)


def vacuum(path: Path | None = None, *, plugin_name: str | None = None) -> int:
    """清库后回收空间（DROP 十张表后 db 文件不会自己变小）。返回收缩后字节数。"""
    with write_connection(path, plugin_name=plugin_name) as conn:
        conn.execute("VACUUM")
    target = path or db_path(plugin_name)
    return target.stat().st_size if target.is_file() else 0


_COL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _assert_table(table: str) -> str:
    name = (table or "").strip()
    if name not in ALLOWED_TABLES:
        extra = (
            "（该表已在 2026-11-14 方案改版中退役：数据库只存店铺映射）"
            if name in RETIRED_TABLES
            else ""
        )
        raise ValueError(
            f"表名不在白名单: {name!r}{extra}；允许: {sorted(ALLOWED_TABLES)}"
        )
    return name


def _assert_cols(cols: list[str]) -> list[str]:
    out: list[str] = []
    for c in cols:
        name = str(c).strip()
        if not _COL_RE.match(name):
            raise ValueError(f"非法列名: {c!r}")
        out.append(name)
    return out


def upsert_row(
    table: str,
    values: dict[str, Any],
    *,
    plugin_name: str | None = None,
    unique_keys: list[str] | None = None,
) -> None:
    """写入一行；unique_keys 有值则按键删后插。"""
    if not values:
        return
    table_name = _assert_table(table)
    normalized: dict[str, Any] = {}
    for raw_k, raw_v in values.items():
        name = str(raw_k).strip()
        if not _COL_RE.match(name):
            raise ValueError(f"非法列名: {raw_k!r}")
        if name in normalized:
            raise ValueError(f"重复列名: {name!r}")
        normalized[name] = raw_v
    cols = list(normalized.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    row_vals = [normalized[c] for c in cols]
    ukeys_raw = [str(k).strip() for k in (unique_keys or []) if str(k).strip()]
    if ukeys_raw:
        missing = [k for k in ukeys_raw if k not in normalized]
        if missing:
            raise ValueError(
                f"unique_keys 在 values 中缺失: {missing}；禁止子集 DELETE"
            )
        keys = _assert_cols(ukeys_raw)
    else:
        keys = []

    with write_connection(plugin_name=plugin_name) as conn:
        if keys:
            clauses: list[str] = []
            params: list[Any] = []
            for k in keys:
                v = normalized[k]
                if v is None:
                    clauses.append(f"{k} IS NULL")
                else:
                    clauses.append(f"{k} = ?")
                    params.append(v)
            conn.execute(
                f"DELETE FROM {table_name} WHERE {' AND '.join(clauses)}",
                params,
            )
        conn.execute(
            f"INSERT INTO {table_name} ({col_sql}) VALUES ({placeholders})",
            row_vals,
        )


def upsert_rows(
    table: str,
    rows: list[dict[str, Any]],
    *,
    plugin_name: str | None = None,
    unique_keys: list[str] | None = None,
) -> int:
    n = 0
    for row in rows:
        if isinstance(row, dict) and row:
            upsert_row(
                table,
                row,
                plugin_name=plugin_name,
                unique_keys=unique_keys,
            )
            n += 1
    return n
