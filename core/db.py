"""巨益 ADB 连接与查询（承接 look_sku.oms_sql_select / 商品信息查询 / 订单日期回填）。

真实表结构（2026-11-13 连生产库 xianxia 实测，见 scripts/probe_adb_schema.py）：
  mid_jedz_datas   22 列英文：id, bill_type, bill_date, bill_code, sku, qty, shop_code,
                   shop_name, detail_id, order_code, delivery_status, refund_status,
                   create_time, bill_amount, bill_time, bill_state, settlement_amount,
                   category_name, order_time, refund_type, mall_type, bill_period
  AI_product_info   9 列：sku, product_code, sku_name, category_name, year, season,
                   brand_name, marked_price, standard_cost
  je_saleorders     5 列：shop_name, shop_code, trade_no, order_date, sales_order_id

原脚本用 `SELECT *` + `pd.DataFrame(results, columns=中文col)` **按位置**重命名，
列名映射隐含在位置里、极易错。这里改为**显式英文列名 SELECT + rename**，
列顺序变化不会再静默错位；映射关系与原脚本逐位核对一致。

改造要点（相对原脚本）：
  · 凭据来自环境变量（core.config），代码内无明文口令
  · 连接用 contextmanager 管理，异常路径也保证 cursor/conn 关闭（原脚本 exit() 前会泄漏连接）
  · SQL 参数化（原脚本已用 %s，保持）
  · 「无数据」是正常业务场景 → 返回空 DataFrame，不抛异常、不 exit()
  · 查询失败 → ReconError，由上层转成可读状态
  · 不依赖 swifter（原脚本仅用于 apply 加速，此处改列向量化）
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Sequence

import pandas as pd

from .config import (
    DEFAULT_LOOKBACK_MONTHS,
    DOC_DELIVERY,
    DOC_REFUND,
    DOC_RETURN,
    TABLE_JEDZ,
    TABLE_PRODUCT,
    TABLE_SALEORDER,
    load_adb_config,
)
from .errors import ReconError

# mid_jedz_datas：真实英文列 → 原脚本中文名（按位置逐列核对一致）
OMS_COLUMN_MAP: dict[str, str] = {
    "id": "id",
    "bill_type": "单据类型",
    "bill_date": "存货异动时间",
    "bill_code": "单号",
    "sku": "sku",                 # 原脚本先叫「规格编码」，随后 oms_df_['sku'] = 规格编码
    "qty": "数量",
    "shop_code": "店铺编号",
    "shop_name": "店铺名称",
    "detail_id": "明细id",
    "order_code": "订单编号",
    "delivery_status": "发货状态",
    "refund_status": "退款状态",
    "create_time": "创建时间",
    "bill_amount": "账单金额",
    "bill_time": "账单日期",
    "bill_state": "对账状态",
    "settlement_amount": "结算金额",
    "category_name": "品类",
    "order_time": "订单日期",
    "refund_type": "退款类型",
    "mall_type": "平台",
    "bill_period": "对账周期",
}

# 原脚本 oms_sql_select 输出的 15 列（下游 matching 依赖这套列名）
OMS_OUTPUT_COLUMNS: tuple[str, ...] = (
    "id", "单据类型", "存货异动时间", "单号", "sku", "品类", "数量",
    "店铺名称", "订单编号", "订单日期", "结算金额", "退款类型",
    "退款状态", "历史对账记录", "对账周期",
)
# 下游透视按「店铺编码」分组，而 OMS 侧列名是「店铺编号」→ 补一列别名
OMS_OUTPUT_EXTRA: tuple[str, ...] = ("店铺编码",)

# AI_product_info：真实英文列 → 原脚本中文名（col_prodcut 按位置核对一致）
PRODUCT_COLUMN_MAP: dict[str, str] = {
    "sku": "sku",
    "product_code": "款号",
    "sku_name": "商品名称",
    "category_name": "品类",
    "year": "年份",
    "season": "季节",
    "brand_name": "品牌",
    "marked_price": "吊牌价",
    "standard_cost": "标准成本单价",
}

PRODUCT_COLUMNS: tuple[str, ...] = tuple(PRODUCT_COLUMN_MAP.values())

# je_saleorders：真实英文列 → 原脚本中文名（col_tradeno 按位置核对一致）
SALEORDER_COLUMN_MAP: dict[str, str] = {
    "shop_name": "店铺名称",
    "shop_code": "店铺编号",
    "trade_no": "订单编号",
    "order_date": "订单日期",
    "sales_order_id": "记录id",
}


def _select_clause(column_map: dict[str, str]) -> str:
    """显式列名 SELECT，不依赖 `SELECT *` 的位置顺序。"""
    return ", ".join(column_map.keys())


# 兼容旧引用
OMS_COLUMNS: tuple[str, ...] = tuple(OMS_COLUMN_MAP.values())

# 商品信息分批查询大小（原脚本 4000）
PRODUCT_BATCH_SIZE = 4000

# 回传分批大小（原脚本 1000）
WRITEBACK_BATCH_SIZE = 1000


def _connect():
    """建立 ADB 连接。凭据缺失 → ConfigError（由 config 抛）。"""
    try:
        import mysql.connector  # 延迟导入：本地功能不依赖驱动是否安装
    except ImportError as exc:
        raise ReconError(
            "缺少数据库驱动 mysql-connector-python",
            detail={"hint": "请在桌面智能体运行环境安装该依赖后重试"},
        ) from exc

    cfg = load_adb_config()
    try:
        return mysql.connector.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            connection_timeout=30,
        )
    except Exception as exc:
        raise ReconError(
            f"连接巨益数据库失败：{type(exc).__name__}",
            detail={"host": cfg.host, "database": cfg.database, "raw": str(exc)[:300]},
        ) from exc


@contextmanager
def adb_connection() -> Iterator[Any]:
    """连接上下文：异常路径也保证关闭（修复原脚本连接泄漏）。

    回传等写路径需要 conn.commit()/rollback()（原脚本 executemany+commit 口径），
    只读查询用 adb_cursor() 即可。
    """
    conn = _connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def adb_cursor() -> Iterator[Any]:
    """游标上下文：异常路径也保证关闭（修复原脚本连接泄漏）。"""
    conn = _connect()
    cursor = None
    try:
        cursor = conn.cursor()
        yield cursor
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def minus_months(date_str: str, months: int) -> str:
    """账期起始日往前推 N 个月（原 minus_x_month_and_assemble）。"""
    from dateutil.relativedelta import relativedelta

    d = datetime.strptime(date_str, "%Y-%m-%d") - relativedelta(months=months)
    return d.strftime("%Y-%m-%d")


def plus_days(date_str: str, days: int = 1) -> str:
    """账期结束日往后推 N 天（原 add_one_day_and_assemble）。"""
    from datetime import timedelta

    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _empty_oms() -> pd.DataFrame:
    return pd.DataFrame(columns=list(OMS_OUTPUT_COLUMNS) + list(OMS_OUTPUT_EXTRA))


def _normalize_oms(rows: Sequence[tuple]) -> pd.DataFrame:
    """真实英文列 → 原脚本中文列，并派生 sku / 历史对账记录 / 店铺编码。

    对应原 look_sku.oms_sql_select 的：
      oms_df_['sku'] = oms_df_['规格编码']
      oms_df_['历史对账记录'] = oms_df_['对账状态']
      输出裁剪到 15 列
    """
    df = pd.DataFrame(list(rows), columns=list(OMS_COLUMN_MAP.keys()))
    df = df.rename(columns=OMS_COLUMN_MAP)
    # 原脚本派生列
    df["历史对账记录"] = df["对账状态"] if "对账状态" in df.columns else ""
    df["id"] = df["id"].astype("string").fillna("")
    # 下游透视按「店铺编码」分组，OMS 侧真实列名是「店铺编号」→ 补别名
    df["店铺编码"] = df["店铺编号"].astype("string").fillna("")
    out_cols = list(OMS_OUTPUT_COLUMNS) + list(OMS_OUTPUT_EXTRA)
    for c in out_cols:
        if c not in df.columns:
            df[c] = ""
    return df.loc[:, out_cols]


def fetch_oms_rows(
    shop_code: str,
    s_date: str,
    e_date: str,
    *,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
) -> dict[str, pd.DataFrame]:
    """取 OMS 出入库单，按单据类型切成三份。

    返回 {'出库': df, '入库': df, '退款': df}；无数据时各为空表（不抛异常）。
    对应原 look_sku.oms_sql_select。

    与原脚本的差异：显式列出英文列名（不再 SELECT * 按位置重命名），
    列顺序变化不会静默错位；退款单去重键「单号+sku+结算金额」保持原逻辑。
    """
    start_time = minus_months(s_date, lookback_months)
    end_time = plus_days(e_date, 1)

    # 显式 ORDER BY id ASC：ADB（MySQL 协议）在无 ORDER BY 时**不保证**返回行序，
    # 且行序会随执行计划/数据分布变化。2026-08 DY023 回归实测：同一订单的多条出库行，
    # 影刀 8 月那次跑的行序与今天查的行序不同（订单 6955322015214016196 今天是
    # id 925→924→927，既非升序也非降序）。
    # 行序会影响「一对多 join 后组内首行保留货款金额、其余置 0」落在哪一行，
    # 进而连锁影响 finalize_advice 的匹配状态回落与处理意见标签。
    # 金额合计与分摊合计恒等（分摊差异 0 分），但逐行比对不可复现。
    # 加 ORDER BY 让插件自身结果稳定可复现，这是可审计的前提。
    sql = (
        f"SELECT {_select_clause(OMS_COLUMN_MAP)} FROM {TABLE_JEDZ} "
        "WHERE bill_date >= %s AND bill_date < %s AND shop_code = %s "
        "ORDER BY id ASC"
    )

    try:
        with adb_cursor() as cursor:
            cursor.execute(sql, (start_time, end_time, shop_code))
            rows = cursor.fetchall()
    except ReconError:
        raise
    except Exception as exc:
        raise ReconError(
            f"查询出入库单失败：{type(exc).__name__}",
            detail={"shop_code": shop_code, "raw": str(exc)[:300]},
        ) from exc

    if not rows:
        # 正常业务场景（新店首月 / 本期无发货），返回空表由上层标 skipped
        return {"出库": _empty_oms(), "入库": _empty_oms(), "退款": _empty_oms()}

    df = _normalize_oms(rows)

    df_out = df[df["单据类型"] == DOC_DELIVERY].copy()
    df_in = df[df["单据类型"] == DOC_RETURN].copy()
    df_refund = df[df["单据类型"] == DOC_REFUND].copy()

    # 退款单去重（原脚本：单号 + sku + 结算金额 拼 temp 列去重）
    if not df_refund.empty:
        tmp = (
            df_refund["单号"].astype("string").fillna("")
            + df_refund["sku"].astype("string").fillna("")
            + df_refund["结算金额"].astype("string").fillna("")
        )
        df_refund = df_refund[~tmp.duplicated()].copy()

    return {"出库": df_out, "入库": df_in, "退款": df_refund}



def fetch_product_info(skus: Sequence[str]) -> pd.DataFrame:
    """按 sku 批量查商品主数据（分批，避免 IN 过长）。

    对应原 look_sku.result 中的 AI_product_info 查询段。
    """
    sku_list = [str(s).strip() for s in skus if str(s).strip()]
    if not sku_list:
        return pd.DataFrame(columns=list(PRODUCT_COLUMNS))

    # 去重保序
    seen: set[str] = set()
    merged: list[str] = []
    for s in sku_list:
        if s not in seen:
            merged.append(s)
            seen.add(s)

    all_rows: list[tuple] = []
    try:
        with adb_cursor() as cursor:
            for i in range(0, len(merged), PRODUCT_BATCH_SIZE):
                batch = merged[i : i + PRODUCT_BATCH_SIZE]
                placeholders = ",".join(["%s"] * len(batch))
                sql = (
                    f"SELECT {_select_clause(PRODUCT_COLUMN_MAP)} "
                    f"FROM {TABLE_PRODUCT} WHERE sku IN ({placeholders})"
                )
                cursor.execute(sql, tuple(batch))
                all_rows.extend(cursor.fetchall())
    except ReconError:
        raise
    except Exception as exc:
        raise ReconError(
            f"查询商品信息失败：{type(exc).__name__}",
            detail={"sku_count": len(merged), "raw": str(exc)[:300]},
        ) from exc

    if not all_rows:
        return pd.DataFrame(columns=list(PRODUCT_COLUMNS))

    df = pd.DataFrame(all_rows, columns=list(PRODUCT_COLUMN_MAP.keys()))
    return df.rename(columns=PRODUCT_COLUMN_MAP)


def fetch_order_dates(order_nos: Sequence[str]) -> dict[str, str]:
    """按订单号查订单日期（原 trade_no_fill / look_sku 的订单日期回填段）。

    返回 {订单编号: 订单日期}。
    """
    nos = [str(o).strip() for o in order_nos if str(o).strip()]
    if not nos:
        return {}

    seen: set[str] = set()
    merged = [n for n in nos if not (n in seen or seen.add(n))]

    out: dict[str, str] = {}
    dup_orders: list[str] = []
    try:
        with adb_cursor() as cursor:
            for i in range(0, len(merged), PRODUCT_BATCH_SIZE):
                batch = merged[i : i + PRODUCT_BATCH_SIZE]
                placeholders = ",".join(["%s"] * len(batch))
                # 显式 ORDER BY：同一 trade_no 在生产库里确实存在多行
                # （实测 261031-403995441472663 有 2 行：10-31 与 11-05），
                # 原脚本 dict(zip(...)) 取「结果集最后一行」，依赖 ADB 未指定的返回顺序，
                # 不可复现。这里改为确定性规则：同 trade_no 取**最新** order_date。
                sql = (
                    f"SELECT trade_no, order_date FROM {TABLE_SALEORDER} "
                    f"WHERE trade_no IN ({placeholders}) "
                    f"ORDER BY trade_no ASC, order_date ASC"
                )
                cursor.execute(sql, tuple(batch))
                for trade_no, order_date in cursor.fetchall():
                    if trade_no is None:
                        continue
                    key = str(trade_no).strip()
                    if not key:
                        continue
                    val = _fmt_date(order_date)
                    if key in out:
                        dup_orders.append(key)
                        # ORDER BY order_date ASC → 后到的更大，直接覆盖即「取最新」
                        if val and val >= out[key]:
                            out[key] = val
                        continue
                    out[key] = val
    except ReconError:
        raise
    except Exception as exc:
        raise ReconError(
            f"查询订单日期失败：{type(exc).__name__}",
            detail={"order_count": len(merged), "raw": str(exc)[:300]},
        ) from exc

    if dup_orders:
        # 不抛异常：这是数据现状，但要让调用方能感知口径差异
        LAST_ORDER_DATE_DUPLICATES.clear()
        LAST_ORDER_DATE_DUPLICATES.extend(sorted(set(dup_orders)))

    return out


def _fmt_date(value: Any) -> str:
    """日期值 → 'YYYY-MM-DD'；None/非法 → ''。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return "" if not s or s.lower() in ("none", "nat") else s[:10]


#: 最近一次 fetch_order_dates 中「同一订单号多行」的订单号（供排查/口径说明）
LAST_ORDER_DATE_DUPLICATES: list[str] = []


def test_connection() -> dict:
    """连通性探测（管理页「数据源」页用）。返回可读状态，不抛异常。"""
    from .config import adb_config_status

    status = adb_config_status()
    if not status["ready"]:
        return {"ok": False, "stage": "config", **status}

    try:
        with adb_cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"ok": True, "stage": "connected", "message": "数据库连接正常", **status}
    except ReconError as exc:
        return {
            "ok": False,
            "stage": "connect",
            "message": exc.message,
            "detail": exc.detail,
            **status,
        }
    except Exception as exc:
        return {
            "ok": False,
            "stage": "connect",
            "message": f"{type(exc).__name__}",
            **status,
        }
