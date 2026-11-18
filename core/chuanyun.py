"""氚云对接（承接 api_chuanyun.py，2026-11-18 启用）。

推送内容：把账单整理产物里的「氚云导入模板」sheet（店铺名称/业务类别/资金方向/金额，元）
经主数据映射（店铺/账单科目/管理科目/资金账户/巨益编号 五张氚云主数据表）组装成
「店铺资金账单」主表 + 子表，调 OpenAPI CreateBizObject 推送并提交流程。

相对原脚本的优化适配：
  · 凭据走环境变量（config.load_h3_headers），原 main.py / error_push_chuanyun.py
    明文 EngineCode/EngineSecret 废弃；SchemaCode 非凭据，集中在 config 常量
  · 原 `except Exception: print` 吞异常 → 逐文件捕获聚合失败清单，Job 如实报
    「成功 N / 失败 M / 跳过 K」，失败文件留守不移动
  · 原成功文件 move 到 氚云导入成功/ → 保留同口径（批次目录内子目录），
    并同步更新 manifest 的 excel/excel_list，管理页仍可下载
  · dry_run（缺省 True）：**零 HTTP 调用**（连主数据只读查询也不发），
    只做模板结构校验 + 金额合计 + payload 骨架预览落盘；
    主数据映射用本地快照（runtime/chuanyun_master.json，真实取数时自动留存），
    无快照时 ObjectId 留空并在 warnings 明示「待真实取数后补全」
  · 原 api_moneyValue 的 bug 一并修复：原代码在 moneyAcct_obj 还是空串时就查余额
    （恒 0），这里改为**账户匹配之后**再查该账户余额
  · 原每文件 sleep(5) 限流保留（真实推送模式）

2026-11-18 用户决策：当前只做代码功能，测试不向氚云传任何数据；
真实推送须显式 dry_run=0 且经用户确认。
"""
from __future__ import annotations

import calendar
import json
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (
    H3_API_URL,
    H3_SCHEMA_BILL,
    H3_SCHEMA_BILL_DETAIL,
    H3_SCHEMA_EXPENSE,
    H3_SCHEMA_MANAGE_EXP,
    H3_SCHEMA_MONEY_ACCT,
    H3_SCHEMA_MONEY_VALUE,
    H3_SCHEMA_OMS_DEPT,
    H3_SCHEMA_SHOP,
    load_h3_headers,
    runtime_dir,
)
from .errors import ChuanyunError, ConfigError

TEMPLATE_SHEET = "氚云导入模板"
TEMPLATE_COLUMNS = ("店铺名称", "业务类别", "资金方向", "金额")
SUCCESS_DIRNAME = "氚云导入成功"
MASTER_SNAPSHOT_NAME = "chuanyun_master.json"
PUSH_INTERVAL_SEC = 5  # 原脚本每文件 sleep(5) 限流


@dataclass
class ChuanyunResult:
    total_files: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = True
    status: str = "skipped"
    message: str = ""
    duration_sec: float = 0.0
    file_reports: list[dict] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_files": self.total_files, "ok": self.ok, "failed": self.failed,
            "skipped": self.skipped, "dry_run": self.dry_run, "status": self.status,
            "message": self.message, "duration_sec": round(self.duration_sec, 2),
            "file_reports": self.file_reports[:100], "previews": self.previews[:10],
        }


# ------------------------------------------------------------------ 工具

def get_month_end_date(period: str) -> str:
    """结算日期 = 账期月最后一天（原 api_chuanyun.get_month_end_date）。

    入参兼容 'YYYY-MM' 与 'YYYY-MM-DD'（原脚本只收后者）。
    """
    p = str(period or "").strip().replace("/", "-")
    if len(p) == 7:
        p += "-01"
    date_obj = datetime.strptime(p[:10], "%Y-%m-%d")
    last_day = calendar.monthrange(date_obj.year, date_obj.month)[1]
    return datetime(date_obj.year, date_obj.month, last_day).strftime("%Y-%m-%d")


def _convert_types(data):
    """numpy 标量 → Python 原生（原 convert_to_python_types，json 序列化前置）。"""
    if isinstance(data, dict):
        return {k: _convert_types(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_convert_types(v) for v in data]
    if isinstance(data, (np.integer, np.floating)):
        return data.item()
    if isinstance(data, float) and (np.isnan(data) or np.isinf(data)):
        return None
    return data


def load_template(excel_path: Path | str) -> pd.DataFrame:
    """读账单整理交付 Excel 的「氚云导入模板」sheet；无该 sheet 返回空表。"""
    p = Path(excel_path)
    if not p.is_file():
        return pd.DataFrame(columns=list(TEMPLATE_COLUMNS))
    try:
        xl = pd.ExcelFile(p)
        if TEMPLATE_SHEET not in xl.sheet_names:
            return pd.DataFrame(columns=list(TEMPLATE_COLUMNS))
        df = xl.parse(TEMPLATE_SHEET)
    except Exception:
        return pd.DataFrame(columns=list(TEMPLATE_COLUMNS))
    for c in TEMPLATE_COLUMNS:
        if c not in df.columns:
            df[c] = "" if c != "金额" else 0.0
    df["金额"] = pd.to_numeric(df["金额"], errors="coerce").fillna(0.0)
    return df.loc[:, list(TEMPLATE_COLUMNS)]


# ------------------------------------------------------------------ 主数据（五张表）

_MASTER_SPECS: tuple[tuple[str, str, dict, str], ...] = (
    # (key, SchemaCode, F字段→中文列, 过滤列)
    ("expense", H3_SCHEMA_EXPENSE,
     {"F0000001": "账单科目名称", "F0000002": "关联科目", "F0000014": "金蝶科目编号",
      "F0000013": "核算类型", "F0000012": "税率"}, ""),
    ("manage_exp", H3_SCHEMA_MANAGE_EXP,
     {"F0000010": "分类科目名称", "F0000025": "预算科目名称", "F0000008": "科目名称",
      "F0000011": "科目性质", "F0000014": "科目预控", "F0000028": "是否入决算",
      "F0000005": "状态"}, "状态"),
    ("shop", H3_SCHEMA_SHOP,
     {"F0000010": "平台", "F0000008": "店铺名称", "F0000011": "注册公司",
      "F0000015": "决算部门", "F0000016": "品牌", "F0000005": "状态"}, "状态"),
    ("money_acct", H3_SCHEMA_MONEY_ACCT,
     {"F0000008": "账户性质", "F0000019": "注册公司", "F0000006": "开户机构全称",
      "F0000007": "账号", "F0000005": "状态", "F0000023": "提现账户obj"}, "状态"),
    ("oms_dept", H3_SCHEMA_OMS_DEPT,
     {"F0000001": "决算部门", "F0000006": "部门预算管控", "F0000014": "巨益编号",
      "F0000016": "品牌", "F0000005": "状态"}, "状态"),
)


def _load_filter() -> str:
    return json.dumps({
        "FromRowNum": 0, "RequireCount": False, "ReturnItems": [],
        "SortByCollection": [], "ToRowNum": 500,
        "Matcher": {"Type": "And", "Matchers": []},
    })


def fetch_master_data(*, url: str = H3_API_URL, headers: dict | None = None) -> dict[str, pd.DataFrame]:
    """真实取数：五张主数据表 LoadBizObjects（只读查询，不写氚云）。

    仅 dry_run=False 路径调用。字段映射与原 api_expenseName / api_manage_exp /
    api_shopID / api_moneyAcct / api_OMS_id 逐位核对一致；
    状态过滤（启用 / 第三方资金平台）同原脚本。
    """
    import requests

    headers = headers or load_h3_headers()
    out: dict[str, pd.DataFrame] = {}
    for key, schema, rename, filter_col in _MASTER_SPECS:
        params = {"ActionName": "LoadBizObjects", "SchemaCode": schema, "Filter": _load_filter()}
        resp = requests.post(url, headers=headers, json=params, timeout=60)
        if resp.status_code != 200:
            raise ChuanyunError(
                f"氚云主数据取数失败（{key}）：HTTP {resp.status_code}",
                detail={"schema": schema, "body": resp.text[:300]},
            )
        arr = (resp.json().get("ReturnData") or {}).get("BizObjectArray") or []
        df = pd.json_normalize(arr)
        cols = ["ObjectId", *rename.keys()]
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        df = df.loc[:, cols].rename(columns=rename)
        if filter_col:
            df = df[df[filter_col] == "启用"]
        if key == "money_acct":
            df = df[df["账户性质"] == "第三方资金平台"]
        out[key] = df.reset_index(drop=True)
    return out


def master_snapshot_path() -> Path:
    return runtime_dir() / MASTER_SNAPSHOT_NAME


def save_master_snapshot(master: dict[str, pd.DataFrame]) -> Path:
    p = master_snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: json.loads(df.to_json(orient="records", force_ascii=False))
               for k, df in master.items()}
    payload["__saved_at__"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def load_master_snapshot() -> dict[str, pd.DataFrame] | None:
    """dry-run 用本地快照（真实取数时自动留存）；无快照返回 None。"""
    p = master_snapshot_path()
    if not p.is_file():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    out = {}
    for key, *_ in _MASTER_SPECS:
        rows = payload.get(key)
        if isinstance(rows, list):
            out[key] = pd.DataFrame(rows)
    return out or None


def fetch_account_balance(money_acct_obj: str, *, url: str = H3_API_URL,
                          headers: dict | None = None) -> float:
    """资金余额台账：该账户剩余可用金额合计（原 api_moneyValue）。

    修复原脚本时序 bug：原代码在 moneyAcct_obj 尚为空串时调用（恒 0），
    这里由调用方在账户匹配完成后传入真实 obj。
    """
    import requests

    if not money_acct_obj:
        return 0.0
    headers = headers or load_h3_headers()
    params = {"ActionName": "LoadBizObjects", "SchemaCode": H3_SCHEMA_MONEY_VALUE,
              "Filter": _load_filter()}
    resp = requests.post(url, headers=headers, json=params, timeout=60)
    if resp.status_code != 200:
        raise ChuanyunError(f"氚云余额台账取数失败：HTTP {resp.status_code}",
                            detail={"body": resp.text[:300]})
    arr = (resp.json().get("ReturnData") or {}).get("BizObjectArray") or []
    df = pd.json_normalize(arr)
    if df.empty or "F0000004" not in df.columns:
        return 0.0
    sub = df[df["F0000004"] == money_acct_obj]
    return float(pd.to_numeric(sub.get("F0000013"), errors="coerce").fillna(0).sum())


# ------------------------------------------------------------------ payload 组装

def _match_money_account(money_acct: pd.DataFrame, shop_dept: str,
                         file_name: str) -> dict | None:
    """资金账户匹配（原 file_arrange 的爱库存/微信分支，逐条同口径）。"""
    if money_acct is None or money_acct.empty:
        return None
    if "爱库存" in shop_dept:
        sub = money_acct[money_acct["开户机构全称"].astype(str).str.contains("爱库存")]
    else:
        sub = money_acct[money_acct["开户机构全称"].astype(str).str.contains(shop_dept, regex=False)]
        if sub.empty:
            return None
        if "微信" in file_name:
            sub = sub[sub["开户机构全称"].astype(str).str.contains("微信")]
        else:
            sub = sub[~sub["开户机构全称"].astype(str).str.contains("微信")]
    if sub.empty:
        return None
    row = sub.iloc[0]
    return {
        "obj": str(row.get("ObjectId") or ""),
        "withdraw_obj": str(row.get("提现账户obj") or ""),
        "name": str(row.get("开户机构全称") or ""),
        "no": str(row.get("账号") or ""),
    }


def build_payload(
    template: pd.DataFrame,
    master: dict[str, pd.DataFrame] | None,
    *,
    period: str,
    file_name: str,
    account_balance: float = 0.0,
) -> tuple[dict | None, list[str], dict]:
    """模板 sheet + 主数据 → CreateBizObject params（原 file_arrange 全映射）。

    返回 (params|None, warnings, stats)。校验门与原脚本一致：
    店铺/平台/公司/业务类别/预算科目 任一未匹配 → 返回 None + 原因（跳过该文件）。
    master=None（dry-run 无快照）：ObjectId 类映射留空，warnings 明示待补全，
    金额/税额/预算标签等本地可算字段照常生成。
    """
    warnings: list[str] = []
    stats: dict = {}
    df = template.copy()
    if df.empty:
        return None, ["模板为空（无收款/付款行）"], stats
    df["店铺名称"] = df["店铺名称"].astype(str).str.strip()
    df["业务类别"] = df["业务类别"].astype(str).str.strip()
    df["资金方向"] = df["资金方向"].astype(str).str.strip()

    m = master or {}
    shop = m.get("shop", pd.DataFrame())
    expense = m.get("expense", pd.DataFrame())
    manage = m.get("manage_exp", pd.DataFrame())
    oms = m.get("oms_dept", pd.DataFrame())
    money_acct = m.get("money_acct", pd.DataFrame())

    def _dict(frame: pd.DataFrame, key: str, val: str) -> dict:
        if frame is None or frame.empty or key not in frame.columns or val not in frame.columns:
            return {}
        return dict(zip(frame[key].astype(str), frame[val]))

    shop_maps = {
        "店铺ObjectId": _dict(shop, "决算部门", "ObjectId"),
        "平台": _dict(shop, "决算部门", "平台"),
        "公司": _dict(shop, "决算部门", "注册公司"),
        "不含渠道名称": _dict(shop, "决算部门", "店铺名称"),
        "品牌": _dict(shop, "决算部门", "品牌"),
    }
    for col, mp in shop_maps.items():
        df[col] = df["店铺名称"].map(mp)
    df["决算部门"] = df["店铺名称"]
    df["巨益编号"] = df["决算部门"].map(_dict(oms, "决算部门", "巨益编号"))
    df["部门预控"] = df["决算部门"].map(_dict(oms, "决算部门", "部门预算管控"))
    df["业务类别ObjectId"] = df["业务类别"].map(_dict(expense, "账单科目名称", "ObjectId"))
    df["管理科目OBJ"] = df["业务类别"].map(_dict(expense, "账单科目名称", "关联科目"))
    df["税率"] = df["业务类别"].map(_dict(expense, "账单科目名称", "税率"))
    df["核算类型"] = df["业务类别"].map(_dict(expense, "账单科目名称", "核算类型"))
    df["账单科目名称"] = df["业务类别"].map(_dict(expense, "账单科目名称", "账单科目名称"))
    for col, src in (("科目分类", "分类科目名称"), ("预算科目", "预算科目名称"),
                     ("科目名称", "科目名称"), ("科目性质", "科目性质"),
                     ("预算管控", "科目预控"), ("是否入决算", "是否入决算")):
        df[col] = df["管理科目OBJ"].map(_dict(manage, "ObjectId", src))

    if master is None:
        warnings.append("无主数据快照：ObjectId/平台/公司/科目映射留空，待真实取数后补全（dry-run 骨架预览）")
    else:
        for col, label in (("店铺ObjectId", "店铺名称"), ("平台", "平台"), ("公司", "公司"),
                           ("业务类别ObjectId", "业务类别")):
            if df[col].isnull().any():
                missing = sorted(set(df.loc[df[col].isnull(), "店铺名称" if col != "业务类别ObjectId" else "业务类别"]))
                return None, [f"存在未匹配的{label}：{('、'.join(missing))[:200]}"], stats

    # 表头参数（原 file_arrange 399-460 行）
    def _first(col: str) -> str:
        if col not in df.columns:
            return ""
        v = df[col].iloc[0]
        return "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)

    platform = _first("平台")
    company = _first("公司")
    shop_dept = str(df["决算部门"].iloc[0])
    payday = get_month_end_date(period)
    time_period = datetime.strptime((period[:7] + "-01") if len(period) == 7 else period[:10], "%Y-%m-%d")

    acct = _match_money_account(money_acct, shop_dept, file_name) if master is not None else None
    if master is not None and acct is None:
        return None, [f"没有匹配的资金账户（决算部门={shop_dept}）"], stats
    acct = acct or {"obj": "", "withdraw_obj": "", "name": "", "no": ""}
    if "微信" in file_name:
        acct["no"] = ""  # 原脚本微信分支不取账号

    tot_received = round(float(df.loc[df["资金方向"] == "收款", "金额"].sum()), 2)
    tot_pay = round(float(df.loc[df["资金方向"] == "付款", "金额"].sum()), 2)
    net_cash = round(tot_received - tot_pay, 2)
    rest_amount = round(float(account_balance) + net_cash, 2)
    note = (f"{time_period.strftime('%Y年%m月')}，{acct['name']}账单整理。"
            f"收款：{tot_received}元，退款：{tot_pay}元，净现金流：{net_cash}元。")
    double_input_text = time_period.strftime("%Y-%m") + acct["obj"]
    stats.update({"tot_received": tot_received, "tot_pay": tot_pay, "net_cash": net_cash,
                  "account_balance": float(account_balance), "rest_amount": rest_amount,
                  "rows": int(len(df))})

    # 子表（原 462-483 行）
    data_df = df.copy()
    data_df["含税金额"] = data_df["金额"].round(2)
    tax = pd.to_numeric(data_df["税率"], errors="coerce").fillna(0).astype(float)
    data_df["不含税金额"] = (data_df["含税金额"] / (1 + tax)).round(2)
    data_df["税费"] = (data_df["含税金额"] - data_df["不含税金额"]).round(2)
    data_df["预算标签"] = (str(time_period.year) + "_" + str(time_period.month) + "_"
                          + data_df["预算科目"].fillna("").astype(str) + "_"
                          + data_df["决算部门"].astype(str))
    data_df["预算变动"] = np.where(data_df["资金方向"] == "收款", "释放", "占用")
    data_df["预算占用金额"] = np.where(data_df["预算变动"] == "释放",
                                      -data_df["含税金额"], data_df["含税金额"])
    data_df["预算占用不含税金额"] = np.where(data_df["预算变动"] == "释放",
                                            -data_df["不含税金额"], data_df["不含税金额"])
    if master is not None and data_df["预算科目"].isnull().any():
        return None, ["存在预算科目为空的行（管理科目映射缺失）"], stats

    def _s(v) -> str:
        return "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)

    detail_rows = [{
        "F0000068": _s(r["店铺ObjectId"]), "F0000072": _s(r["平台"]),
        "F0000077": _s(r["不含渠道名称"]), "F0000069": _s(r["决算部门"]),
        "F0000070": _s(r["品牌"]), "F0000071": _s(r["巨益编号"]),
        "F0000058": _s(r["业务类别ObjectId"]), "F0000010": _s(r["管理科目OBJ"]),
        "F0000011": _s(r["科目分类"]), "F0000012": _s(r["科目名称"]),
        "F0000048": _s(r["预算科目"]), "F0000013": _s(r["科目性质"]),
        "F0000061": _s(r["是否入决算"]), "F0000059": _s(r["核算类型"]),
        "F0000014": _s(r["资金方向"]),
        "F0000015": float(r["含税金额"]),
        "F0000052": None if pd.isna(r["税率"]) else float(r["税率"]),
        "F0000053": float(r["税费"]), "F0000051": float(r["不含税金额"]),
        "F0000036": _s(r["预算管控"]), "F0000037": _s(r["部门预控"]),
        "F0000039": _s(r["预算标签"]), "F0000049": _s(r["预算变动"]),
        "F0000045": float(r["预算占用金额"]), "F0000067": float(r["预算占用不含税金额"]),
        "F0000084": _s(r["账单科目名称"]),
    } for _, r in data_df.iterrows()]

    biz = {
        "CreatedBy": "", "OwnerId": "",
        "F0000001": "BIL-店铺资金账单",
        "F0000030": note,
        "F0000078": platform, "F0000002": company,
        "F0000003": period, "F0000076": payday,
        "F0000021": acct["obj"], "F0000024": acct["name"], "F0000025": acct["no"],
        "F0000066": acct["withdraw_obj"],
        "F0000022": float(account_balance), "F0000020": net_cash, "F0000023": rest_amount,
        "F0000016": tot_received, "F0000017": tot_pay, "F0000057": net_cash,
        "F0000026": double_input_text,
        "F0000034": "预算管控模式", "F0000035": "年度_月度_科目_部门",
        "F0000062": "未生成",
        H3_SCHEMA_BILL_DETAIL: detail_rows,
    }
    params = {
        "ActionName": "CreateBizObject",
        "SchemaCode": H3_SCHEMA_BILL,
        "BizObject": json.dumps(_convert_types(biz), ensure_ascii=False),
        "IsSubmit": "true",
    }
    return params, warnings, stats


def push_biz_object(params: dict, *, url: str = H3_API_URL,
                    headers: dict | None = None) -> dict:
    """真实推送 CreateBizObject（仅 dry_run=False 路径）。返回响应 JSON。"""
    import requests

    headers = headers or load_h3_headers()
    resp = requests.post(url, headers=headers, data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
                         timeout=60)
    if resp.status_code != 200:
        raise ChuanyunError(f"氚云推送失败：HTTP {resp.status_code}",
                            detail={"body": resp.text[:300]})
    try:
        return resp.json()
    except ValueError as exc:
        raise ChuanyunError("氚云响应不是合法 JSON", detail={"body": resp.text[:300]}) from exc


def save_preview(batch_dir: Path | str, file_name: str, params: dict | None,
                 warnings: list[str], stats: dict) -> Path:
    """dry-run payload 预览落盘：{批次}/底稿/chuanyun_preview/{文件名}.json。"""
    d = Path(batch_dir) / "底稿" / "chuanyun_preview"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (Path(file_name).stem + ".json")
    payload = {"file": file_name, "warnings": warnings, "stats": stats,
               "params": params, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p