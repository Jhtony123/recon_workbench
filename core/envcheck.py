"""插件运行环境自检（2026-11-18，随插件分发）。

纯本地只读探测：不连数据库、不发 HTTP、不安装任何东西。
输出 = 逐包安装状态/版本 + 五能力就绪矩阵 + 缺失包安装指引（venv_install）。

DEPENDENCIES 是唯一事实源（SSOT）：
  · skills/recon_assist/environment_checks.yaml（宿主 env_check 启动体检清单）
  · requirements.txt（人工安装参考）
  三者由 scripts/verify_envcheck.py 做一致性断言，改依赖先改这里再同步。

分层口径：
  required    核心三包（pandas/numpy/openpyxl）——缺任何一个插件整体不可用
  conditional 按功能条件（mysql-connector-python=ADB 取数/回传；requests=氚云真实推送）
  optional    纯加速/格式优选（xlsxwriter/pyarrow/python-calamine），全部有自动回落
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DepSpec:
    pypi: str                 # PyPI 包名（venv_install 用）
    module: str               # import 名（find_spec 探测用）
    tier: str                 # required / conditional / optional
    purpose: str              # 干什么用
    features: tuple[str, ...] # 影响哪些能力（required/optional 影响面见 readiness）
    fallback: str = ""        # 缺失时的回落行为（optional 必填）
    min_version: str = ""     # 建议下限（本机实测通过版本族）


DEPENDENCIES: tuple[DepSpec, ...] = (
    DepSpec("pandas", "pandas", "required",
            "对账核心数据处理（全部能力）", ("bill", "match", "cost", "writeback", "chuanyun", "summaries"),
            min_version="2.0"),
    DepSpec("numpy", "numpy", "required",
            "数值计算（pandas 依赖，通常随 pandas 安装）", ("bill", "match", "cost", "writeback", "chuanyun", "summaries"),
            min_version="1.24"),
    DepSpec("openpyxl", "openpyxl", "required",
            "Excel 读写兜底引擎（导出/汇总读簿）", ("bill", "match", "cost", "writeback", "chuanyun", "summaries"),
            min_version="3.1"),
    DepSpec("mysql-connector-python", "mysql.connector", "conditional",
            "巨益 ADB 驱动（SKU对账取数 / 数据库回传真实落库）", ("match", "writeback"),
            fallback="缺失时 SKU对账取数与真实回传报可读错误；账单整理/成本匹配/汇总/dry-run 预检不受影响",
            min_version="8.0"),
    DepSpec("requests", "requests", "conditional",
            "氚云 OpenAPI（真实推送）", ("chuanyun",),
            fallback="缺失时氚云真实推送不可用；dry-run 预检零 HTTP 不受影响",
            min_version="2.28"),
    DepSpec("xlsxwriter", "xlsxwriter", "optional",
            "Excel 写引擎首选", ("bill", "match", "cost"),
            fallback="自动回落 openpyxl，功能不受影响", min_version="3.1"),
    DepSpec("pyarrow", "pyarrow", "optional",
            "底稿 parquet 格式", ("bill", "match", "cost"),
            fallback="自动落 CSV（utf-8-sig），列序与金额口径完全一致（fastparquet 亦可替代）",
            min_version="14.0"),
    DepSpec("python-calamine", "python_calamine", "optional",
            "大簿秒级读（SKU 透视汇总 VIP 67MB 级）", ("summaries",),
            fallback="自动回落 openpyxl 流式读（分钟级），结果一致", min_version="0.2"),
)

#: 能力 → 必需包集合（conditional 按功能计入；optional 永不阻塞就绪）
FEATURE_LABELS = {
    "bill": "账单整理",
    "match": "SKU对账",
    "cost": "成本匹配",
    "writeback": "数据库回传",
    "chuanyun": "氚云对接",
    "summaries": "透视汇总",
}


def _probe(spec: DepSpec) -> dict[str, Any]:
    installed = importlib.util.find_spec(spec.module.split(".")[0]) is not None
    if installed and "." in spec.module:
        # 子模块包（mysql.connector）：顶层在但子模块缺也算缺
        try:
            installed = importlib.util.find_spec(spec.module) is not None
        except (ImportError, ValueError):
            installed = False
    version = None
    if installed:
        try:
            from importlib.metadata import version as _v
            version = _v(spec.pypi)
        except Exception:
            version = None
    return {
        "pypi": spec.pypi, "module": spec.module, "tier": spec.tier,
        "installed": installed, "version": version,
        "purpose": spec.purpose, "fallback": spec.fallback,
        "min_version": spec.min_version,
    }


def feature_readiness(installed: dict[str, bool]) -> dict[str, dict]:
    """能力就绪矩阵（纯函数，便于测试）。

    · required 三包缺任一 → 全部能力 not_ready；
    · conditional 缺 → 仅对应能力降级：match/writeback 标 degraded（dry-run 仍可用），
      chuanyun 标 degraded（dry-run 仍可用）；
    · optional 缺失不影响就绪，只进 suggestions。
    """
    core_ok = all(installed.get(s.pypi, False) for s in DEPENDENCIES if s.tier == "required")
    out: dict[str, dict] = {}
    for key, label in FEATURE_LABELS.items():
        missing_required = [s.pypi for s in DEPENDENCIES
                            if s.tier == "required" and not installed.get(s.pypi, False)]
        missing_cond = [s.pypi for s in DEPENDENCIES
                        if s.tier == "conditional" and key in s.features
                        and not installed.get(s.pypi, False)]
        if not core_ok:
            status, note = "not_ready", f"缺核心依赖：{', '.join(missing_required)}"
        elif missing_cond:
            status = "degraded"
            note = f"缺 {', '.join(missing_cond)}：真实取数/落库/推送不可用，dry-run 预检与本地功能仍可用"
        else:
            status, note = "ready", ""
        out[key] = {"label": label, "status": status, "note": note,
                    "missing_required": missing_required, "missing_conditional": missing_cond}
    return out


def env_status() -> dict:
    """环境自检主入口（actions.env_status 调用）。只读，不抛异常。"""
    from .config import adb_config_status, h3_config_status

    packages = [_probe(s) for s in DEPENDENCIES]
    installed = {p["pypi"]: p["installed"] for p in packages}
    readiness = feature_readiness(installed)

    missing_required = [p["pypi"] for p in packages if p["tier"] == "required" and not p["installed"]]
    missing_conditional = [p["pypi"] for p in packages if p["tier"] == "conditional" and not p["installed"]]
    missing_optional = [p["pypi"] for p in packages if p["tier"] == "optional" and not p["installed"]]

    # 安装指引：venv_install 一次装齐缺失包（required 必装；conditional 按需；optional 建议）
    install_now = missing_required + missing_conditional
    guidance = {
        "tool": "venv_install",
        "packages_required": missing_required,
        "packages_conditional": missing_conditional,
        "packages_optional_recommended": missing_optional,
        "hint": (
            "让 AI 调 venv_install(packages=[...]) 安装到 Desktop Agent 运行时"
            "（不要 Bash pip install 到系统 Python）。"
            + (f"必装：{', '.join(missing_required)}。" if missing_required else "核心依赖已齐。")
            + (f"按功能选装：{', '.join(missing_conditional)}。" if missing_conditional else "")
            + (f"可选加速：{', '.join(missing_optional)}（不装不影响功能正确性）。" if missing_optional else "")
        ),
        "requirements_file": "requirements.txt（插件根目录，人工安装参考）",
        "docs": "docs/dependencies.md",
    }

    overall = "ok"
    if missing_required:
        overall = "error"
    elif missing_conditional or missing_optional:
        overall = "warning"

    return {
        "overall": overall,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "packages": packages,
        "features": readiness,
        "missing": {"required": missing_required, "conditional": missing_conditional,
                    "optional": missing_optional},
        "install_guidance": guidance,
        "credentials": {
            "adb": adb_config_status(),
            "h3yun": h3_config_status(),
        },
        "note": "凭据缺失只影响真实取数/回传/推送（环境变量配置，见 docs/dependencies.md）；"
                "dry-run 预检与本地功能不需要凭据。",
    }