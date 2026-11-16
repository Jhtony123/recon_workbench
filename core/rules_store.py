"""平台解析规则的**文件化**存储（rules/{platform}.json）。

【2026-11-14 方案改版】规则从 SQLite 表 ext_recon_workbench_platform_rules 迁出。
用户要求数据库只存店铺映射，而规则属于「运行配置」而非对账结果，
放 JSON 文件的好处：可进版本管理、可 diff、可手工批量改，且不占库表迁移成本。

文件格式（version=1）：
{
  "platform": "taobao",
  "version": 1,
  "note": "...",
  "updated_at": "2026-11-14 15:20:00",
  "rules": {
    "column_map": [{"key": "order_no", "value": "业务基础订单号",
                    "note": "", "active": 1, "updated_at": "..."}],
    "biz_code":   [{"key": "0010001", "value": "货款", ...}]
  }
}

rule_kind 取值与旧表一致：column_map / filter / amount_sign / biz_code / read_option。

写入用「临时文件 + os.replace」原子替换：Job 与 MCP action 可能并发调用，
半截 JSON 会让整个平台解析失败（比读不到规则更糟）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .errors import ConfigError

#: 规则文件目录：插件源码目录下 rules/（随插件一起分发与版本管理）
RULES_DIR_NAME = "rules"

VALID_RULE_KINDS: tuple[str, ...] = (
    "column_map",
    "filter",
    "amount_sign",
    "biz_code",
    "read_option",
)

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def rules_dir() -> Path:
    from .config import plugin_dir

    d = plugin_dir() / RULES_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(platform: str) -> Path:
    """平台标识 -> 规则文件路径。平台标识只允许 [a-z0-9_]，防路径穿越。"""
    key = str(platform or "").strip().lower()
    if not key:
        raise ConfigError("未指定平台", detail={"hint": "platform 必填"})
    if not all(ch.isalnum() or ch == "_" for ch in key):
        raise ConfigError(
            f"非法平台标识：{platform!r}",
            detail={"hint": "只允许小写字母、数字与下划线"},
        )
    return rules_dir() / f"{key}.json"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_file(platform: str) -> dict[str, Any]:
    """读单个平台的规则文件；带 mtime 缓存（文件被手工改过会自动失效）。"""
    path = _path(platform)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _CACHE.pop(platform, None)
        return {"platform": platform, "version": 1, "rules": {}}

    hit = _CACHE.get(platform)
    if hit and hit[0] == mtime:
        return hit[1]

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 规则文件损坏必须显式报错：静默回落内置映射会让财务拿到口径错误的结果
        raise ConfigError(
            f"平台规则文件损坏：{path.name}",
            detail={"path": str(path), "error": f"{type(exc).__name__}: {exc}",
                    "hint": "请修复或删除该文件后重试；删除后平台回落内置兜底映射"},
        ) from exc

    if not isinstance(payload, dict):
        payload = {"platform": platform, "version": 1, "rules": {}}
    payload.setdefault("platform", platform)
    payload.setdefault("version", 1)
    rules = payload.get("rules")
    payload["rules"] = rules if isinstance(rules, dict) else {}
    _CACHE[platform] = (mtime, payload)
    return payload


def _write_file(platform: str, payload: dict[str, Any]) -> None:
    path = _path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["platform"] = platform
    payload["version"] = int(payload.get("version") or 1)
    payload["updated_at"] = _now()
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    fd, tmp = tempfile.mkstemp(prefix=f".{platform}-rules-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _CACHE.pop(platform, None)


def list_rule_platforms() -> list[str]:
    """已有规则文件的平台标识（升序）。"""
    try:
        d = rules_dir()
    except Exception:
        return []
    return sorted(p.stem for p in d.glob("*.json") if not p.stem.startswith("."))


def load_rules_map(platform: str) -> dict[str, dict[str, str]]:
    """规则文件 -> {rule_kind: {rule_key: rule_value}}，供平台适配器读取。

    只返回 active 的规则；无文件时返回空 dict（适配器用内置兜底映射）。
    与旧 repo.load_rules_map 的返回结构完全一致，适配器侧零改动。
    """
    if not str(platform or "").strip():
        return {}
    payload = _read_file(str(platform).strip().lower())
    out: dict[str, dict[str, str]] = {}
    for kind, entries in (payload.get("rules") or {}).items():
        if not isinstance(entries, list):
            continue
        bucket = out.setdefault(str(kind), {})
        for e in entries:
            if not isinstance(e, dict):
                continue
            if int(e.get("active", 1) or 0) != 1:
                continue
            bucket[str(e.get("key") or "")] = str(e.get("value") or "")
    return out


def query_rules(
    platform: str = "",
    *,
    active_only: bool = True,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """扁平行视图，字段与旧表一致，UI 与 AI 工具无需改渲染。

    返回列：platform / rule_kind / rule_key / rule_value / note / active / updated_at
    """
    plats = [str(platform).strip().lower()] if str(platform or "").strip() else list_rule_platforms()
    rows: list[dict[str, Any]] = []
    for p in plats:
        if not p:
            continue
        try:
            payload = _read_file(p)
        except ConfigError as exc:
            rows.append({
                "platform": p, "rule_kind": "_error", "rule_key": "",
                "rule_value": "", "note": exc.message, "active": 0,
                "updated_at": "",
            })
            continue
        for kind in sorted((payload.get("rules") or {}).keys()):
            entries = payload.get("rules", {}).get(kind) or []
            if not isinstance(entries, list):
                continue
            for e in entries:
                if not isinstance(e, dict):
                    continue
                active = int(e.get("active", 1) or 0)
                if active_only and active != 1:
                    continue
                rows.append({
                    "platform": p,
                    "rule_kind": str(kind),
                    "rule_key": str(e.get("key") or ""),
                    "rule_value": str(e.get("value") or ""),
                    "note": str(e.get("note") or ""),
                    "active": active,
                    "updated_at": str(e.get("updated_at") or ""),
                })
    rows.sort(key=lambda r: (r["platform"], r["rule_kind"], r["rule_key"]))
    n = int(limit or 500)
    return rows[:n] if n > 0 else rows


def upsert_rule(
    platform: str,
    rule_kind: str,
    rule_key: str,
    rule_value: str,
    *,
    note: str = "",
    active: bool = True,
) -> None:
    """新增/修改一条规则（按 platform+rule_kind+rule_key 唯一定位）。"""
    plat = str(platform or "").strip().lower()
    kind = str(rule_kind or "").strip()
    key = str(rule_key or "").strip()
    if not plat or not kind or not key:
        raise ConfigError("platform / rule_kind / rule_key 均必填")
    if kind not in VALID_RULE_KINDS:
        raise ConfigError(
            f"rule_kind 非法：{kind}",
            detail={"valid": list(VALID_RULE_KINDS)},
        )

    payload = _read_file(plat)
    rules = dict(payload.get("rules") or {})
    entries = [dict(e) for e in (rules.get(kind) or []) if isinstance(e, dict)]

    stamp = _now()
    hit = next((e for e in entries if str(e.get("key") or "") == key), None)
    if hit is None:
        entries.append({
            "key": key,
            "value": str(rule_value or ""),
            "note": str(note or ""),
            "active": 1 if active else 0,
            "updated_at": stamp,
        })
    else:
        hit["value"] = str(rule_value or "")
        hit["note"] = str(note or "")
        hit["active"] = 1 if active else 0
        hit["updated_at"] = stamp

    entries.sort(key=lambda e: str(e.get("key") or ""))
    rules[kind] = entries
    payload["rules"] = rules
    _write_file(plat, payload)


def delete_rule(platform: str, rule_kind: str, rule_key: str) -> None:
    """停用一条规则（置 active=0，保留条目以便追溯；与旧表行为一致）。"""
    upsert_rule(platform, rule_kind, rule_key, "", note="stopped", active=False)


def count_rules() -> int:
    """全部平台的启用规则条数（数据态用）。"""
    n = 0
    for p in list_rule_platforms():
        try:
            payload = _read_file(p)
        except ConfigError:
            continue
        for entries in (payload.get("rules") or {}).values():
            if isinstance(entries, list):
                n += sum(1 for e in entries if isinstance(e, dict) and int(e.get("active", 1) or 0) == 1)
    return n
