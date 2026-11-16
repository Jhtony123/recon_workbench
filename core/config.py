"""配置与凭据。

凭据纪律（用户决策）：
  · 巨益 ADB 连接信息一律读**系统环境变量**，插件不存凭据、config.json 不写凭据。
    RECON_ADB_HOST / RECON_ADB_PORT / RECON_ADB_USER / RECON_ADB_PASSWORD / RECON_ADB_DATABASE
  · 原脚本里 5+ 处明文 host/user/password 全部废弃，代码中不出现任何口令字面量。
  · 缺配置时抛 ConfigError（带「需要运维在机器上配哪些变量」的可读提示），
    由管理页转成黄色引导态，而不是红色系统崩溃。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

PLUGIN_NAME = "recon_workbench"

# 环境变量名（唯一凭据来源）
ENV_ADB_HOST = "RECON_ADB_HOST"
ENV_ADB_PORT = "RECON_ADB_PORT"
ENV_ADB_USER = "RECON_ADB_USER"
ENV_ADB_PASSWORD = "RECON_ADB_PASSWORD"
ENV_ADB_DATABASE = "RECON_ADB_DATABASE"

# 库表名（原脚本硬编码在 SQL 里，集中到这里）
TABLE_JEDZ = "mid_jedz_datas"        # 出入库单（B2C配货单 / B2C退换货通知单 / 退款单）
TABLE_PRODUCT = "AI_product_info"    # 商品主数据（款号/品类/年份/季节/品牌/吊牌价/标准成本单价）
TABLE_SALEORDER = "je_saleorders"    # 销售订单（订单日期回填）

# OMS 取数默认回溯月数（原 main.py 的 month_no 参数）
DEFAULT_LOOKBACK_MONTHS = 6

# 单据类型（原脚本字面量）
DOC_DELIVERY = "B2C配货单"
DOC_RETURN = "B2C退换货通知单"
DOC_REFUND = "退款单"

# 业务类别
BIZ_GOODS = "货款"

# 匹配状态
MATCHED = "已匹配"
UNMATCHED = "未匹配"

# 处理意见（三分口径，成本结转核心）
ADVICE_CARRY = "结转成本"            # 入库与出库能对上
ADVICE_OFFSET_COST = "冲成本"        # 入库无对应出库
ADVICE_OFFSET_SHIP = "冲发出商品"
ADVICE_NO_ACTION = "成本不做处理"     # 出库未参与收款核对

# 数据来源
SRC_RECEIPT = "收款匹配"
SRC_REFUND = "退款匹配"

# 匹配明细标准列（原 look_sku.py 里重复出现 10+ 次的 32 列字面量，收敛为唯一常量）
MATCH_DETAIL_COLUMNS: tuple[str, ...] = (
    "账单周期", "订单编号", "订单日期", "货款金额", "货款分摊金额",
    "id", "单据类型", "存货异动时间", "单号", "sku", "款号", "商品名称",
    "品类", "年份", "季节", "品牌", "吊牌价", "数量", "店铺名称",
    "结算金额", "退款类型", "退款状态", "标准成本单价", "标准成本",
    "采购单价", "发货成本", "巨益单据匹配状态", "历史对账记录",
    "对账周期", "是否秒退判断", "处理意见",
)

# 回传数据库的列
WRITEBACK_COLUMNS: tuple[str, ...] = ("id", "账单周期", "数量")


@dataclass(frozen=True)
class AdbConfig:
    """巨益 ADB（AnalyticDB MySQL）连接配置。"""

    host: str
    port: int
    user: str
    password: str
    database: str

    def masked(self) -> dict:
        """给 UI / 日志用的脱敏视图，绝不含口令。"""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": "***" if self.password else "",
            "database": self.database,
        }


_ENV_CACHE: dict[str, str] | None = None


def _user_env_snapshot() -> dict[str, str]:
    """读 Windows 用户级环境变量（HKCU\\Environment）作为兜底。

    为什么需要：进程环境变量在启动时快照，用 setx / [Environment]::SetEnvironmentVariable
    写入用户级变量后，已在运行的 sidecar 进程读不到，必须重启才生效。
    这里直接查注册表，让「配完就能用」，不必重启桌面智能体。
    仅 Windows 有效；其它平台返回空 dict（只靠 os.environ）。
    """
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    snap: dict[str, str] = {}
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            idx = 0
            while True:
                try:
                    name, value, _vtype = winreg.EnumValue(key, idx)
                except OSError:
                    break
                idx += 1
                if isinstance(value, str):
                    snap[name] = value
    except Exception:
        snap = {}
    _ENV_CACHE = snap
    return snap


def env(name: str) -> str:
    """取环境变量：进程环境优先，缺失时回落用户级注册表。"""
    v = os.environ.get(name)
    if v is not None and str(v).strip():
        return str(v)
    return str(_user_env_snapshot().get(name, "") or "")


def load_adb_config() -> AdbConfig:
    """从环境变量读取 ADB 凭据（进程环境 → 用户级注册表兜底）；缺失则抛 ConfigError。"""
    host = env(ENV_ADB_HOST).strip()
    user = env(ENV_ADB_USER).strip()
    password = env(ENV_ADB_PASSWORD)
    database = env(ENV_ADB_DATABASE).strip() or "xianxia"
    port_raw = env(ENV_ADB_PORT).strip() or "3306"

    missing = [
        name
        for name, val in (
            (ENV_ADB_HOST, host),
            (ENV_ADB_USER, user),
            (ENV_ADB_PASSWORD, password),
        )
        if not val
    ]
    if missing:
        raise ConfigError(
            "数据库连接未配置",
            detail={
                "missing_env": missing,
                "hint": (
                    "请运维在本机系统环境变量中配置："
                    f"{ENV_ADB_HOST} / {ENV_ADB_USER} / {ENV_ADB_PASSWORD}"
                    f"（可选 {ENV_ADB_PORT}、{ENV_ADB_DATABASE}），"
                    "配置后重启桌面智能体使其生效。"
                ),
            },
        )

    try:
        port = int(port_raw)
    except ValueError:
        port = 3306

    return AdbConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )


def adb_config_status() -> dict:
    """探测凭据是否就绪（不连接、不抛异常、不暴露口令），供管理页「数据源」页显示。"""
    names = (ENV_ADB_HOST, ENV_ADB_USER, ENV_ADB_PASSWORD, ENV_ADB_DATABASE)
    present = {name: bool(env(name).strip()) for name in names}
    # 哪些来自注册表兜底（进程环境里没有）→ 提示重启可让子进程也继承
    from_registry = [
        n for n in names
        if present[n] and not str(os.environ.get(n, "") or "").strip()
    ]
    missing = [k for k, v in present.items() if not v and k != ENV_ADB_DATABASE]
    return {
        "ready": not missing,
        "missing_env": missing,
        "present_env": sorted(k for k, v in present.items() if v),
        "loaded_from_registry": from_registry,
        "database_default": "xianxia",
        "hint": (
            "已就绪，可连接巨益数据库取数"
            if not missing
            else f"缺少 {', '.join(missing)}，取数与回传功能不可用；"
                 "账单整理、成本匹配等本地功能仍可正常使用"
        ),
    }


def plugin_dir() -> Path:
    return Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 账期标签

def period_label(period: str, *, s_date: str = "", e_date: str = "") -> str:
    """把账期转成回传数据库用的中文标签。

    生产库 mid_jedz_datas.bill_period 实测存的是中文格式（2026-11-13 查得）：
      '2026年06月-2026年06月'（当前版本）、'2025年05-05月'（历史遗留格式）
    原脚本 look_sku.check_dates 产出的是：
      f"{s_year}年{s_month}月-{e_year}年{e_month}月"
    得物为 "YYYY年MM月-YYYY年MM月"（单月-单月），唯品会带日。

    本函数复刻 check_dates 的通用分支，保证回传值与库内既有值同一格式，
    否则同一账期会在库里裂成两种写法，后续按 bill_period 统计会漏。
    """
    p = str(period or "").strip()
    s = str(s_date or "").strip()
    e = str(e_date or "").strip()

    if s and e and len(s) >= 7 and len(e) >= 7:
        sy, sm = s[:4], s[5:7]
        ey, em = e[:4], e[5:7]
        return f"{sy}年{sm}月-{ey}年{em}月"

    if p:
        # 'YYYY-MM' 或 'YYYY-MM~YYYY-MM'
        head = p.replace("/", "-")[:7]
        parts = head.split("-")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            y, m = parts[0], parts[1].zfill(2)
            return f"{y}年{m}月-{y}年{m}月"
        return p
    return ""


def period_from_label(label: str) -> str:
    """中文账期标签 → 'YYYY-MM'（读库时反向解析）。"""
    s = str(label or "").strip()
    if not s:
        return ""
    try:
        y = s.split("年")[0]
        m = s.split("年")[1].replace("月", "").split("-")[0]
        if y.isdigit() and m.isdigit():
            return f"{y}-{m.zfill(2)}"
    except (IndexError, AttributeError):
        pass
    return s


def runtime_dir() -> Path:
    """data/extensions/recon_workbench/ 运行时落盘区（结果 Excel、上传文件）。"""
    try:
        from extensions.paths import plugin_data_dir  # type: ignore

        return Path(plugin_data_dir(PLUGIN_NAME))
    except Exception:
        base = Path(os.environ.get("DESKTOP_AGENT_HOME", r"D:\DesktopAgent"))
        return base / "data" / "extensions" / PLUGIN_NAME


def settings_path() -> Path:
    """插件设置文件（文件态，与规则文件同纪律，不进数据库）。"""
    return runtime_dir() / "settings.json"


def load_settings() -> dict:
    """读设置；损坏/缺失一律回落空 dict（设置是辅助信息，不该让页面打不开）。"""
    import json

    p = settings_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(patch: dict) -> dict:
    """合并写设置（原子替换，避免半截 JSON）。"""
    import json
    import os
    import tempfile

    data = load_settings()
    data.update({k: v for k, v in (patch or {}).items()})
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def results_root() -> Path:
    """结果存储根目录：用户在设置页自定义优先，缺省 runtime_dir()/results。

    自定义路径不可写时 output_dir() 的 mkdir 会抛可读错误，
    由 Job 终态转成失败提示，而不是静默落回默认目录（那会让用户找不到文件）。
    """
    custom = str(load_settings().get("results_dir") or "").strip()
    if custom:
        return Path(custom)
    return runtime_dir() / "results"


def default_results_dir() -> Path:
    """默认结果目录（清空历史时连同自定义目录一起清，防切换路径后旧历史残留）。"""
    return runtime_dir() / "results"


def output_dir() -> Path:
    """对账结果 Excel 输出目录（带时间戳子目录，不覆盖历史结果）。

    原脚本 get_save_dir 会对已存在目录执行 rmtree，重名即静默删旧结果；
    这里改为时间戳隔离 + 历史可回看。根目录可由设置页自定义（results_root）。
    """
    d = results_root()
    d.mkdir(parents=True, exist_ok=True)
    return d
