"""账单整理产物的文件化存储（results/ 下表格 + manifest.json）。

【2026-11-14 方案改版】账单整理结果**不再入库**（用户要求数据库只存店铺映射），
改落文件（方案 3-B：Excel + 机器可读表格）。为什么两者都要：
  · Excel —— 财务交付物，人看、可对账、可归档；
  · 表格  —— 机器读的规范结果，将来 SKU 对账直接读它，不必重新解析原始账单
             （原始账单可能是几十 MB 的 gb18030 csv/xlsx，重复解析既慢又易踩编码坑）。

表格格式由**环境决定**，不要假设扩展名：
  · 有 pyarrow / fastparquet -> bill_detail.parquet / bill_goods.parquet
  · 都没有（2026-11-14 本机实测就是这种）-> bill_detail.csv / bill_goods.csv
    （utf-8-sig 带 BOM，Excel 双击不乱码）
两种格式列序与金额口径完全一致。一律通过 BillBatch.detail_path / goods_path
或 read_detail() / read_goods() 访问，**不要自己拼 .parquet 后缀**。

目录布局（每次运行一个带时间戳的批次目录，永不覆盖历史）：
  results/
    {period}_{platform}_账单整理_{shop_code}_{YYYYmmdd_HHMMSS}/
      manifest.json                      批次元信息（口径、行数、货款合计、产物路径）
      {platform}_{shop}_{period}_账单整理.xlsx   财务交付 Excel
      bill_detail.parquet | .csv         账单标准行（金额单位=分，int64）
      bill_goods.parquet | .csv          匹配层输入货款行（多源平台与 detail 不等价）

金额口径：表格内**一律存「分」的整数**（与旧库 *_fen 列同口径），
导出 Excel 时才转元。读回时由 from_fen/fen_to_float 还原，避免 float 累加分位漂移。

为什么用 manifest 而不是 SQLite 索引：批次目录本身就是索引（目录名含账期/平台/店铺/时间戳），
manifest 只补「口径与合计」这类无法从目录名得到的信息；扫目录即可列批次，无需库表。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from .config import output_dir
from .money import fen_to_float

MANIFEST_NAME = "manifest.json"

#: 过程底稿子目录（2026-11-16 用户需求：批次目录只留财务交付 Excel，
#: manifest / bill_detail / bill_goods / 统一明细 / E卡等过程产物一律进这里）
DRAFTS_DIRNAME = "底稿"


def manifest_path_for(batch_dir: Path) -> Path:
    """批次 manifest 位置：新批次在 底稿/，旧批次在根目录（兼容读取）。

    唯品会多旬共用一个批次目录，底稿按旬分子目录（底稿/{旬标签}/），
    此时取最新一份 manifest 代表该目录。
    """
    d = Path(batch_dir)
    p = d / DRAFTS_DIRNAME / MANIFEST_NAME
    if p.is_file():
        return p
    nested = sorted((d / DRAFTS_DIRNAME).glob(f"*/{MANIFEST_NAME}"))
    if nested:
        return nested[-1]
    return d / MANIFEST_NAME

#: 表格产物的主名（扩展名由可用引擎决定：parquet 优先，缺引擎回落 csv）
DETAIL_STEM = "bill_detail"
GOODS_STEM = "bill_goods"
#: 读取时按此顺序探测；写入时取第一个可用引擎对应的扩展名
TABLE_EXTS: tuple[str, ...] = (".parquet", ".csv")

# 向后兼容旧常量名（SKILL.md 与外部脚本可能引用）
DETAIL_PARQUET = DETAIL_STEM + ".parquet"
GOODS_PARQUET = GOODS_STEM + ".parquet"

MANIFEST_VERSION = 1

# 旧目录名格式：{period}_{platform}_账单整理_{shop_code}_{stamp}（历史批次兼容读取）
_DIR_RE = re.compile(
    r"^(?P<period>[^_]+)_(?P<platform>[^_]+)_账单整理_(?P<shop>[^_]+)_(?P<stamp>\d{8}_\d{6})$"
)
# 新目录名格式（2026-11-16 用户需求）：YYYY-MM-中文平台名-店铺名称-店铺编号，重跑覆盖。
# 特例：爱库存 YYYY-MM-爱库存（一渠道多店，不写店铺）；
#       唯品会 YYYY-MM-唯品会-供应商（默认唯品中国），旬文件在目录内按结算期命名。
_DIR_RE_NEW = re.compile(
    r"^(?P<period>\d{4}-\d{2})-(?P<display>[^-]+)(?:-(?P<shop_name>.+)-(?P<shop>[A-Za-z0-9]+))?$"
)
# 唯品会两层目录第一层：YYYY-MM-唯品会-供应商（无店铺编号段，单独识别）
_DIR_RE_VIP = re.compile(r"^(?P<period>\d{4}-\d{2})-唯品会-(?P<supplier>.+)$")

# 账单标准行的规范列（parquet schema 固定列序，避免各平台列漂移）
DETAIL_COLUMNS: tuple[str, ...] = (
    "batch_id", "platform", "shop_code", "shop_name", "period",
    "s_date", "e_date", "order_no", "fund_date", "biz_category",
    "receipt_fen", "refund_fen", "net_fen", "source_file", "source_row",
)
GOODS_COLUMNS: tuple[str, ...] = (
    "batch_id", "platform", "shop_code", "shop_name", "period",
    "s_date", "e_date", "order_no", "fund_date",
    "receipt_fen", "refund_fen", "net_fen",
    "match_s_date", "match_e_date", "source_file", "source_row",
)


@dataclass
class BillBatch:
    """一次账单整理批次的落盘位置与元信息。"""

    batch_id: str
    dir: Path
    platform: str = ""
    shop_code: str = ""
    shop_name: str = ""
    period: str = ""
    s_date: str = ""
    e_date: str = ""
    status: str = ""
    created_at: str = ""
    detail_rows: int = 0
    goods_rows: int = 0
    totals: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: 底稿子目录名（唯品会多旬共用批次目录时按旬隔离：底稿/{旬标签}）
    drafts_subdir: str = ""

    @property
    def excel_path(self) -> Path | None:
        p = self.files.get("excel")
        return Path(p) if p else None

    @property
    def drafts_dir(self) -> Path:
        """过程底稿目录（manifest / 明细表格 / 统一明细簿等）。"""
        return self.dir / (self.drafts_subdir or DRAFTS_DIRNAME)

    @property
    def detail_path(self) -> Path:
        """账单标准行表格：旧批次在根目录（兼容），新批次写/读 底稿/。"""
        legacy = resolve_table_path(self.dir, DETAIL_STEM)
        if legacy.is_file():
            return legacy
        return resolve_table_path(self.drafts_dir, DETAIL_STEM)

    @property
    def goods_path(self) -> Path:
        """匹配层输入货款行表格（同上，底稿/ 优先）。"""
        legacy = resolve_table_path(self.dir, GOODS_STEM)
        if legacy.is_file():
            return legacy
        return resolve_table_path(self.drafts_dir, GOODS_STEM)

    @property
    def table_format(self) -> str:
        return self.detail_path.suffix.lstrip(".") or "csv"


# ------------------------------------------------------------------ 表格引擎


def parquet_available() -> bool:
    """是否有可用的 parquet 引擎。

    2026-11-14 实测：本机运行时 Python **既没有 pyarrow 也没有 fastparquet**
    （pandas 3.0.4 / numpy 2.5.0 / 仅 openpyxl），`df.to_parquet` 直接 ImportError。
    财务机器装什么不该由插件假设，所以表格产物按可用性回落 CSV：
    CSV 用 utf-8-sig（Excel 直接双击不乱码），金额列仍是整数分，口径不变。
    """
    import importlib.util

    return any(importlib.util.find_spec(m) for m in ("pyarrow", "fastparquet"))


def write_table_ext() -> str:
    return ".parquet" if parquet_available() else ".csv"


def resolve_table_path(directory: Path, stem: str) -> Path:
    """按 TABLE_EXTS 顺序找已存在的表格文件；都没有则返回默认写路径。"""
    d = Path(directory)
    for ext in TABLE_EXTS:
        p = d / f"{stem}{ext}"
        if p.is_file():
            return p
    return d / f"{stem}{write_table_ext()}"


def _safe(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch not in '\\/:*?"<>|')


def build_batch_dir(period: str, platform: str, shop_code: str,
                    shop_name: str = "", display_name: str = "") -> Path:
    """批次目录（2026-11-16 用户需求改命名）：YYYY-MM-中文平台名-店铺名称-店铺编号。

    · 重复运行**直接覆盖**：普通平台重跑前清空旧批次目录（原脚本 get_save_dir 同口径），
      不再加时间戳后缀。
    · 特例 爱库存：YYYY-MM-爱库存（一个渠道含多店 AKC003/AKC004，不写店铺）。
    · 特例 唯品会：YYYY-MM-唯品会-供应商（默认唯品中国）；一个账期多旬共用该目录，
      旬文件按结算期命名在目录内累积，**不清目录**（否则跑第二旬会删第一旬交付件）。
    """
    import shutil

    display = _safe(display_name or platform)
    if platform == "akc":
        name = f"{_safe(period)}-{display}"
    elif platform == "vip":
        name = f"{_safe(period)}-{display}-唯品中国"
    else:
        name = f"{_safe(period)}-{display}-{_safe(shop_name or shop_code)}-{_safe(shop_code)}"
    d = output_dir() / name
    if d.is_dir() and platform != "vip":
        shutil.rmtree(d, ignore_errors=True)
    # 唯品会不清目录：一个账期多旬交付件在目录内累积，重跑同旬由导出同名覆盖；
    # 底稿按旬子目录隔离（BillBatch.drafts_subdir），互不覆盖。
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ 写


def _normalize_df(
    df: pd.DataFrame | None,
    columns: Sequence[str],
    *,
    int_cols: Iterable[str] = (),
) -> pd.DataFrame:
    """补齐列序、清洗标量，金额/行号列强制 int64（parquet 不接受混合类型）。"""
    if df is None or df.empty:
        return pd.DataFrame({c: pd.Series(dtype="int64" if c in tuple(int_cols) else "object")
                             for c in columns})
    out = df.copy()
    for c in columns:
        if c not in out.columns:
            out[c] = 0 if c in tuple(int_cols) else ""
    out = out.loc[:, list(columns)]
    for c in int_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype("int64")
    for c in columns:
        if c in tuple(int_cols):
            continue
        out[c] = out[c].where(out[c].notna(), "").astype("object")
        out[c] = out[c].map(lambda v: "" if v is None or (isinstance(v, float) and v != v) else str(v))
    return out


def write_table(
    dirpath: Path | str,
    stem: str,
    df: pd.DataFrame | None,
    columns: Sequence[str],
    *,
    int_cols: Iterable[str] = (),
) -> tuple[Path, int]:
    """落一份表格产物；返回 (实际路径, 行数)。目录自动创建。

    格式由环境决定：有 pyarrow/fastparquet 就写 parquet，否则写 CSV（utf-8-sig，
    Excel 双击不乱码）。两种格式列序与金额口径完全一致，读回时由 _read_table 归一。
    调用方不要假设扩展名 —— 一律用返回的路径，或 BillBatch.detail_path/goods_path。
    """
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    norm = _normalize_df(df, columns, int_cols=int_cols)
    ext = write_table_ext()
    path = d / f"{stem}{ext}"
    if ext == ".parquet":
        try:
            norm.to_parquet(path, index=False)
            return path, int(len(norm))
        except Exception:
            # 引擎探测通过但写失败（版本不兼容等）：回落 CSV，别让整个整理作业失败
            path = d / f"{stem}.csv"
    # CSV：utf-8-sig 带 BOM，Excel 直接打开不乱码；int64 列原样写出保持分口径
    norm.to_csv(path, index=False, encoding="utf-8-sig")
    # 另一种格式的旧残留要删掉，避免 resolve_table_path 命中过期文件
    for other in TABLE_EXTS:
        stale = d / f"{stem}{other}"
        if other != path.suffix and stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass
    return path, int(len(norm))


# 向后兼容旧函数名（SKILL.md / pipeline_dormant 可能引用）
def write_parquet(
    path: Path | str,
    df: pd.DataFrame | None,
    columns: Sequence[str],
    *,
    int_cols: Iterable[str] = (),
) -> tuple[Path, int]:
    """兼容旧签名：传完整路径也可，实际按环境决定扩展名。"""
    p = Path(path)
    return write_table(p.parent, p.stem, df, columns, int_cols=int_cols)


def write_manifest(batch: BillBatch) -> Path:
    """写 manifest.json（原子替换）。新批次落 底稿/，批次根目录只留交付 Excel。"""
    batch.drafts_dir.mkdir(parents=True, exist_ok=True)
    path = batch.drafts_dir / MANIFEST_NAME
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "batch_id": batch.batch_id,
        "platform": batch.platform,
        "shop_code": batch.shop_code,
        "shop_name": batch.shop_name,
        "period": batch.period,
        "s_date": batch.s_date,
        "e_date": batch.e_date,
        "status": batch.status,
        "created_at": batch.created_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        "detail_rows": int(batch.detail_rows or 0),
        "goods_rows": int(batch.goods_rows or 0),
        "totals": dict(batch.totals or {}),
        "files": {k: str(v) for k, v in (batch.files or {}).items()},
        "drafts_subdir": batch.drafts_subdir or "",
        "notes": [str(n) for n in (batch.notes or [])],
        "money_unit": "fen",
        "note": "账单整理产物元信息。数据库不存账单结果（2026-11-14 方案改版），"
                "批次以本目录为单位；金额列一律为「分」的整数。",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ 读


def read_manifest(path: Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_dir_name(name: str) -> dict[str, str]:
    """目录名 → {period, platform, shop_code, shop_name, stamp}；解析不出给空串。

    兼容两种命名：旧 {period}_{platform}_账单整理_{shop}_{stamp} 与
    新 YYYY-MM-中文平台名[-店铺名称-店铺编号]（2026-11-16 改命名）。
    """
    n = str(name or "")
    m = _DIR_RE.match(n)
    if m:
        return {
            "period": m.group("period"),
            "platform": m.group("platform"),
            "shop_code": m.group("shop"),
            "shop_name": "",
            "stamp": m.group("stamp"),
        }
    m = _DIR_RE_VIP.match(n)
    if m:
        return {"period": m.group("period"), "platform": "vip",
                "shop_code": "", "shop_name": m.group("supplier"), "stamp": ""}
    m = _DIR_RE_NEW.match(n)
    if m:
        from .platforms.registry import DISPLAY_TO_PLATFORM

        display = m.group("display")
        return {
            "period": m.group("period"),
            "platform": DISPLAY_TO_PLATFORM.get(display, display),
            "shop_code": m.group("shop") or "",
            "shop_name": m.group("shop_name") or "",
            "stamp": "",
        }
    return {"period": "", "platform": "", "shop_code": "", "shop_name": "", "stamp": ""}


def clear_history(*, period: str = "", platform: str = "", shop_code: str = "") -> dict[str, Any]:
    """清空账单整理历史（2026-11-16 用户需求：结果下载完即可清理，全在本地）。

    清理范围（只动插件自己的产物，绝不碰用户其它文件）：
      · 默认 results/ 与当前自定义 results 根目录下的**批次目录**
        （目录名匹配 _DIR_RE/_DIR_RE_NEW，或含 manifest.json —— 双重判据防误删手工目录）
      · downloads/ 下的下载副本（该目录为插件专用）
      · logs/jobs.jsonl 作业留痕（joblog.clear_records）
    保留：店铺映射（数据库唯一表）、rules/ 规则文件、settings.json。
    切换过自定义路径时两个根目录都清，防旧历史残留。

    细粒度（2026-11-16 用户需求，AI 可按条件清理）：给 period/platform/shop_code
    任一时只删**命中条件的批次目录**（按 manifest 与目录名双解析匹配），
    downloads 副本与作业留痕**保留**（无法可靠归属到单批次）；全空则全清。
    """
    import shutil

    from . import joblog
    from .config import default_results_dir, results_root, runtime_dir
    from .platforms.registry import parse_platform

    scoped = bool(period or platform or shop_code)
    want_platform = parse_platform(platform) if platform else ""

    roots: list[Path] = []
    for r in (default_results_dir(), results_root()):
        r = Path(r)
        if r not in roots:
            roots.append(r)

    deleted: list[str] = []
    kept: list[str] = []
    errors: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            # 新格式目录名可能撞用户手工目录：仅当平台名可识别或含 manifest 才认作批次
            m_new = _DIR_RE_NEW.match(d.name)
            known_display = False
            if m_new:
                from .platforms.registry import DISPLAY_TO_PLATFORM

                known_display = m_new.group("display") in DISPLAY_TO_PLATFORM
            is_batch = (bool(_DIR_RE.match(d.name))
                        or bool(_DIR_RE_VIP.match(d.name))
                        or (bool(m_new) and (known_display or manifest_path_for(d).is_file()))
                        or manifest_path_for(d).is_file())
            if not is_batch:
                kept.append(str(d))
                continue
            if scoped:
                meta = parse_dir_name(d.name)
                man = read_manifest(manifest_path_for(d))
                eff_period = str(man.get("period") or meta.get("period") or "")
                eff_platform = str(man.get("platform") or meta.get("platform") or "")
                eff_shop = str(man.get("shop_code") or meta.get("shop_code") or "")
                if period and eff_period != period:
                    continue
                if want_platform and eff_platform != want_platform:
                    continue
                if shop_code and eff_shop != shop_code:
                    continue
            try:
                shutil.rmtree(d)
                deleted.append(d.name)
            except OSError as exc:
                errors.append(f"{d.name}: {exc}")

    dl_removed = 0
    log_removed = 0
    if not scoped:
        dl_dir = runtime_dir() / "downloads"
        if dl_dir.is_dir():
            for f in dl_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        dl_removed += 1
                    except OSError as exc:
                        errors.append(f"downloads/{f.name}: {exc}")
        log_removed = joblog.clear_records()
    return {
        "batches_deleted": len(deleted),
        "batches": deleted,
        "downloads_removed": dl_removed,
        "joblog_removed": log_removed,
        "kept_dirs": kept,
        "errors": errors,
        "roots": [str(r) for r in roots],
        "scope": {"period": period, "platform": want_platform or platform, "shop_code": shop_code},
        "scoped": scoped,
    }


def list_batches(
    *,
    period: str = "",
    platform: str = "",
    shop_code: str = "",
    limit: int = 50,
) -> list[BillBatch]:
    """扫 results/ 列出账单整理批次（**新→旧**：按目录名内时间戳倒序）。

    目录名解析不出的（手工建的目录、旧版命名）仍会返回，只是筛选条件不生效 ——
    宁可多列一条，也不要因为一个目录命名不规范就整页空白。
    """
    root = output_dir()
    if not root.is_dir():
        return []
    out: list[BillBatch] = []
    for d in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir():
            continue
        meta = parse_dir_name(d.name)
        man = read_manifest(manifest_path_for(d))
        # 新目录名可能不含店铺段（vip/akc），筛选用 manifest 回落
        eff_period = str(man.get("period") or meta.get("period") or "")
        eff_platform = str(man.get("platform") or meta.get("platform") or "")
        eff_shop = str(man.get("shop_code") or meta.get("shop_code") or "")
        if period and eff_period != period:
            continue
        if platform and eff_platform != platform:
            continue
        if shop_code and eff_shop != shop_code:
            continue
        out.append(BillBatch(
            batch_id=str(man.get("batch_id") or meta.get("stamp") or d.name),
            dir=d,
            platform=eff_platform,
            shop_code=eff_shop,
            shop_name=str(man.get("shop_name") or meta.get("shop_name") or ""),
            drafts_subdir=str(man.get("drafts_subdir") or ""),
            period=str(man.get("period") or meta.get("period") or ""),
            s_date=str(man.get("s_date") or ""),
            e_date=str(man.get("e_date") or ""),
            status=str(man.get("status") or ""),
            created_at=str(man.get("created_at") or meta.get("stamp") or ""),
            detail_rows=int(man.get("detail_rows") or 0),
            goods_rows=int(man.get("goods_rows") or 0),
            totals=dict(man.get("totals") or {}),
            files=dict(man.get("files") or {}),
            notes=[str(n) for n in (man.get("notes") or [])],
        ))
        if limit and len(out) >= int(limit):
            break
    return out


def resolve_batch(
    *,
    batch_id: str = "",
    period: str = "",
    platform: str = "",
    shop_code: str = "",
) -> BillBatch | None:
    """定位一个批次：显式 batch_id 优先，否则取该范围内**最新**一次。

    为什么必须收敛到单批次：每次运行都新建目录，同一店铺同一账期跑两次就有两份结果。
    查询若不收敛会把历次结果叠在一起 —— 这正是旧库版本踩过的坑（DY014 重跑十余次后
    管理页成本卡片显示 131,014.52 元、真值 1,800.26 元），改成文件后同样要防。
    """
    if str(batch_id or "").strip():
        target = str(batch_id).strip()
        root = output_dir()
        if root.is_dir():
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                man = read_manifest(manifest_path_for(d))
                if target in (str(man.get("batch_id") or ""), d.name):
                    meta = parse_dir_name(d.name)
                    return BillBatch(
                        batch_id=str(man.get("batch_id") or d.name),
                        dir=d,
                        platform=str(man.get("platform") or meta.get("platform") or ""),
                        shop_code=str(man.get("shop_code") or meta.get("shop_code") or ""),
                        shop_name=str(man.get("shop_name") or ""),
                        period=str(man.get("period") or meta.get("period") or ""),
                        s_date=str(man.get("s_date") or ""),
                        e_date=str(man.get("e_date") or ""),
                        status=str(man.get("status") or ""),
                        created_at=str(man.get("created_at") or ""),
                        detail_rows=int(man.get("detail_rows") or 0),
                        goods_rows=int(man.get("goods_rows") or 0),
                        totals=dict(man.get("totals") or {}),
                        files=dict(man.get("files") or {}),
                        notes=[str(n) for n in (man.get("notes") or [])],
                        drafts_subdir=str(man.get("drafts_subdir") or ""),
                    )
        return None

    batches = list_batches(period=period, platform=platform, shop_code=shop_code, limit=1)
    return batches[0] if batches else None


def _read_table(path: Path, *, int_cols: Sequence[str] = ()) -> pd.DataFrame:
    """读表格产物（parquet 或 csv 自动识别）。

    读失败（引擎缺失、文件损坏）返回空表，由调用方给可读空态 ——
    不让一个坏文件把整个管理页打不开。
    """
    p = Path(path)
    if not p.is_file():
        return pd.DataFrame()
    try:
        if p.suffix.lower() == ".parquet":
            df = pd.read_parquet(p)
        else:
            # dtype=str 防止 pandas 把订单号前导零/长数字推断成 float 丢精度，
            # 随后只对金额与行号列显式转 int64
            df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
            for c in int_cols:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
            for c in df.columns:
                if c in tuple(int_cols):
                    continue
                df[c] = df[c].fillna("")
        return df
    except Exception:
        return pd.DataFrame()


# 向后兼容旧函数名
def _read_parquet(path: Path, *, int_cols: Sequence[str] = ()) -> pd.DataFrame:
    return _read_table(path, int_cols=int_cols)


#: 各表格的整数列（CSV 读回时必须显式转 int64，否则金额口径会漂成字符串）
DETAIL_INT_COLS = ("receipt_fen", "refund_fen", "net_fen", "source_row")
GOODS_INT_COLS = ("receipt_fen", "refund_fen", "net_fen", "source_row")


def read_detail(
    batch: BillBatch | None = None,
    *,
    batch_id: str = "",
    period: str = "",
    platform: str = "",
    shop_code: str = "",
) -> pd.DataFrame:
    """读账单标准行（金额为分）。未指定批次则取范围内最新一次。"""
    b = batch or resolve_batch(batch_id=batch_id, period=period,
                               platform=platform, shop_code=shop_code)
    if not b:
        return pd.DataFrame(columns=list(DETAIL_COLUMNS))
    df = _read_table(b.detail_path, int_cols=DETAIL_INT_COLS)
    if df.empty:
        return pd.DataFrame(columns=list(DETAIL_COLUMNS))
    return df


def read_goods(
    batch: BillBatch | None = None,
    *,
    batch_id: str = "",
    period: str = "",
    platform: str = "",
    shop_code: str = "",
) -> pd.DataFrame:
    """读匹配层输入货款行（金额为分）。SKU 对账启用后从这里取输入，不重解析原始账单。"""
    b = batch or resolve_batch(batch_id=batch_id, period=period,
                               platform=platform, shop_code=shop_code)
    if not b:
        return pd.DataFrame(columns=list(GOODS_COLUMNS))
    df = _read_table(b.goods_path, int_cols=GOODS_INT_COLS)
    if df.empty:
        return pd.DataFrame(columns=list(GOODS_COLUMNS))
    return df


def match_window(
    *,
    batch_id: str = "",
    period: str = "",
    platform: str = "",
    shop_code: str = "",
) -> tuple[str, str]:
    """匹配窗口 (match_s_date, match_e_date)：SKU 对账补录段筛「存货异动时间」用的账期。

    与账单账期**刻意分开**：多源平台两者不等价。京东 JD001 实测 —— OMS 取数用用户账期
    (08-01~08-31)，补录段却用钱包「备注资金日期」min~max(07-31~08-30)；两端各差一天
    就让补录行在收款/退款侧翻转。无记录时返回 ("", "")，调用方回落用户账期。
    """
    df = read_goods(batch_id=batch_id, period=period, platform=platform, shop_code=shop_code)
    if df.empty or "match_s_date" not in df.columns or "match_e_date" not in df.columns:
        return "", ""
    sub = df[(df["match_s_date"].astype(str) != "") & (df["match_e_date"].astype(str) != "")]
    if sub.empty:
        return "", ""
    return str(sub.iloc[0]["match_s_date"]), str(sub.iloc[0]["match_e_date"])


# ------------------------------------------------------------------ 汇总


def _json_list(value: Any) -> list:
    """manifest files 里的 JSON 字符串列表（excel_list / downloads）→ list。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def batch_rows(batch: BillBatch, *, yuan: bool = True) -> dict[str, Any]:
    """批次 → UI/工具用的一行摘要。"""
    totals = dict(batch.totals or {})
    row: dict[str, Any] = {
        "batch_id": batch.batch_id,
        "platform": batch.platform,
        "shop_code": batch.shop_code,
        "shop_name": batch.shop_name,
        "period": batch.period,
        "s_date": batch.s_date,
        "e_date": batch.e_date,
        "status": batch.status,
        "created_at": batch.created_at,
        "detail_rows": int(batch.detail_rows or 0),
        "goods_rows": int(batch.goods_rows or 0),
        "dir": str(batch.dir),
        "excel": batch.files.get("excel", ""),
        "excel_list": _json_list(batch.files.get("excel_list")),
        "downloads": _json_list(batch.files.get("downloads")),
        # 键名保留 *_parquet 以兼容既有调用方；实际扩展名可能是 .csv（环境无 parquet 引擎时）
        "detail_parquet": str(batch.detail_path) if batch.detail_path.is_file() else "",
        "goods_parquet": str(batch.goods_path) if batch.goods_path.is_file() else "",
        "table_format": batch.table_format,
        "notes": list(batch.notes or []),
    }
    for k, v in totals.items():
        if yuan and k.endswith("_fen"):
            row[k.replace("_fen", "_yuan")] = fen_to_float(v)
        else:
            row[k] = v
    return row


def coverage(*, limit: int = 200) -> dict[str, Any]:
    """按平台 / 账期汇总批次覆盖（总览页与数据态用）。"""
    batches = list_batches(limit=limit)
    by_platform: dict[str, dict[str, Any]] = {}
    by_period: dict[str, dict[str, Any]] = {}
    for b in batches:
        p = by_platform.setdefault(b.platform or "-", {"platform": b.platform or "-", "batches": 0, "detail_rows": 0})
        p["batches"] += 1
        p["detail_rows"] += int(b.detail_rows or 0)

        key = b.period or "-"
        q = by_period.setdefault(key, {"period": key, "batches": 0, "detail_rows": 0,
                                       "goods_net_fen": 0, "goods_receipt_fen": 0,
                                       "goods_refund_fen": 0})
        q["batches"] += 1
        q["detail_rows"] += int(b.detail_rows or 0)
        t = b.totals or {}
        q["goods_net_fen"] += int(t.get("goods_net_fen") or 0)
        q["goods_receipt_fen"] += int(t.get("goods_receipt_fen") or 0)
        q["goods_refund_fen"] += int(t.get("goods_refund_fen") or 0)

    periods = sorted(by_period.values(), key=lambda r: r["period"], reverse=True)
    for q in periods:
        q["goods_net_yuan"] = fen_to_float(q.pop("goods_net_fen"))
        q["goods_receipt_yuan"] = fen_to_float(q.pop("goods_receipt_fen"))
        q["goods_refund_yuan"] = fen_to_float(q.pop("goods_refund_fen"))

    return {
        "batch_count": len(batches),
        "detail_rows": sum(int(b.detail_rows or 0) for b in batches),
        "goods_rows": sum(int(b.goods_rows or 0) for b in batches),
        "platforms": sorted(by_platform.values(), key=lambda r: -r["detail_rows"]),
        "periods": periods,
        "results_dir": str(output_dir()),
    }
