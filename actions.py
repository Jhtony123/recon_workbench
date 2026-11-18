"""recon_workbench — Agent 工具实现（permission=action）。

【2026-11-14 方案改版】只做影刀现有能力：
  · 账单整理     —— 可用（结果落文件，不入数据库）
  · SKU 对账     —— 可用（2026-11-17 启用，2026-11-18 入口接线补齐）
  · 成本匹配     —— 可用（2026-11-18 启用，SKU 对账下游，成功后迁移 已匹配成本/）
  · 数据库回传   —— 可用（2026-11-18 启用，缺省 dry-run 预检不写库；
                   真实落库须显式 dry_run=0 且经用户确认）
  · 氚云对接     —— 可用（2026-11-18 启用，缺省 dry-run 预检零 HTTP 不推送；
                   真实推送须显式 dry_run=0 且经用户确认）

因此本文件**不再有** query_match_detail / unmatched_summary / query_pivot /
cost_summary / query_writeback / load_cost_file 这些工具（对应表已退役，
查询走文件态批次工具 bill_batches/sku_batches/query_bill_rows + 底稿）。
改版前库表版编排封存于 core/pipeline_dormant.py（仅历史口径参考，不在运行路径上）。

所有 action 返回归一结构：{ok, status, summary, data}；大结果只回摘要 + 分页读取入口。
财务口径纪律写在 skills/recon_assist/SKILL.md，主脑按那里执行。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
for _p in (str(_PLUGIN_DIR), r"D:\DesktopAgent"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import artifacts, db, joblog, pipeline, repo, rules_store, summaries  # noqa: E402
from core.errors import ReconError  # noqa: E402
from core.pipeline import _as_file_map, _as_list  # noqa: E402
from core.platforms.registry import list_platforms, parse_platform  # noqa: E402

#: 真实可跑的阶段；其余为占位暂缓
ACTIVE_STAGES = pipeline.ACTIVE_STAGES
DEFERRED_STAGES = pipeline.DEFERRED_STAGES


def _ok(summary: str, data: Any = None, **extra) -> dict:
    """归一出口。

    注意：Runtime Kit 的 normalize_action_result 要求 data 必须是 dict，
    传 list 会被静默丢成 {}。所以列表一律包成 {"rows": [...]}。
    """
    if data is None:
        payload: dict = {}
    elif isinstance(data, dict):
        payload = data
    else:
        payload = {"rows": data}
    out = {"ok": True, "status": "completed", "summary": summary, "data": payload}
    out.update(extra)
    return out


def _fail(message: str, **extra) -> dict:
    out = {"ok": False, "status": "failed", "summary": message,
           "error": {"code": "recon_error", "message": message}}
    out.update(extra)
    return out


def _deferred(stage: str) -> dict:
    """占位能力的归一终态：ok=True + status=deferred，绝不假装成功也不报系统错。"""
    res = pipeline.deferred_result(stage)
    return {
        "ok": True,
        "status": "deferred",
        "summary": res.message,
        "data": dict(res.data or {}),
    }


def _int(value: Any, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 状态与概览


def dist_check(include_git: Any = "1", **_) -> dict:
    """分发前安全自检（2026-11-18，凭据防泄露机制；只读，不改文件不连库不发HTTP）。

    扫描插件目录工作区 + git 全历史：本机真实凭据值是否出现在文件里、
    硬编码口令样赋值、PEM 私钥、混入的运行时文件（settings/plugin.db/结果/日志）。
    返回 verdict（safe/caution/blocked）+ findings（文件+行号，秘密打码）+
    打包排除清单 + 凭据存储架构说明。分享插件给其他用户前先调它。
    """
    from core import distcheck

    try:
        data = distcheck.dist_check(
            include_git=str(include_git or "1") not in ("0", "false", "False"))
    except Exception as exc:
        return _fail(f"分发安全自检失败：{type(exc).__name__}: {exc}")
    v = data["verdict"]
    n_ws = len(data["workspace"]["findings"])
    n_git = len(data["git_history"].get("findings") or [])
    n_rt = len(data["workspace"]["runtime_files_in_plugin_dir"])
    summary = {
        "safe": f"分发安全自检通过：工作区与 git 历史均无凭据命中，插件目录无运行时文件，可分享",
        "caution": f"可分享但须按排除清单打包：插件目录混入 {n_rt} 个运行时文件/残留（非凭据）",
        "blocked": f"禁止直接分享：发现凭据/私钥命中（工作区 {n_ws} / git历史 {n_git}），先清理 findings 所列文件",
    }[v]
    return _ok(summary, data)


def env_status(**_) -> dict:
    """插件运行环境自检（2026-11-18，分发用户入口；只读，不连库不发HTTP不安装）。

    返回逐包安装状态/版本（required/conditional/optional 三层）、六能力就绪矩阵、
    缺失包 venv_install 安装指引、ADB/氚云凭据就绪状态（不暴露口令）。
    缺依赖时让 AI 按 install_guidance 调宿主 venv_install 安装到 sidecar 运行时。
    """
    from core import envcheck

    try:
        data = envcheck.env_status()
    except Exception as exc:
        return _fail(f"环境自检失败：{type(exc).__name__}: {exc}")
    miss = data.get("missing") or {}
    n_miss = sum(len(v) for v in miss.values())
    if data["overall"] == "ok":
        summary = "运行环境就绪：全部依赖已安装"
    elif data["overall"] == "error":
        summary = (f"核心依赖缺失：{', '.join(miss.get('required') or [])}，"
                   "插件不可用，请按 install_guidance 调 venv_install 安装")
    else:
        parts = []
        if miss.get("conditional"):
            parts.append(f"条件依赖缺失 {', '.join(miss['conditional'])}（对应功能真实路径不可用，dry-run 不受影响）")
        if miss.get("optional"):
            parts.append(f"可选加速缺失 {', '.join(miss['optional'])}（自动回落，功能不受影响）")
        summary = "核心依赖已齐；" + "；".join(parts)
    return _ok(summary, data)


def capability_status(**_) -> dict:
    """五个能力的开放状态（五能力全可用；回传/氚云缺省 dry-run 预检）。"""
    rows = pipeline.capability_status()
    active = [r["name"] for r in rows if r["status"] == "active"]
    deferred = [r["name"] for r in rows if r["status"] != "active"]
    summary = f"可用：{'、'.join(active)}"
    if deferred:
        summary += f"；暂缓：{'、'.join(deferred)}"
    summary += "（回传与氚云缺省 dry-run 预检，不写外部系统）"
    return _ok(
        summary,
        {"rows": rows, "active": active, "deferred": deferred,
         "note": pipeline.DEFERRED_NOTE},
    )


def overview(days: Any = 90, **_) -> dict:
    """对账总览：账单整理覆盖、最近批次、店铺映射与规则条数。

    总览不做跨批次「匹配率/未匹配差异」聚合（防重跑叠加误读）；
    SKU/成本结果按批次下钻（sku_batches + 底稿）。
    """
    try:
        data = repo.overview(_int(days, 90))
    except Exception as exc:
        return _fail(f"读取总览失败：{type(exc).__name__}: {exc}")

    totals = data.get("totals") or {}
    latest = data.get("latest_job") or {}
    parts = [
        f"账单批次 {totals.get('bill_batches', 0)}",
        f"账单行 {totals.get('bill_rows', 0)}",
        f"店铺映射 {totals.get('shop_map', 0)} 家",
        f"规则 {totals.get('rules', 0)} 条",
    ]
    if latest:
        parts.append(
            f"最近批次 {latest.get('batch_id', '')}"
            f"（{latest.get('platform', '')}/{latest.get('shop_code', '')}/{latest.get('period', '')}："
            f"{latest.get('status', '')}）"
        )
    parts.append("SKU对账/成本匹配/数据库回传/氚云对接（回传与氚云缺省 dry-run 预检）可用")
    return _ok("；".join(parts), data)


def data_status(**_) -> dict:
    """插件数据态：数据库只剩店铺映射表；账单批次/规则/作业留痕看文件。"""
    try:
        return _ok("数据态已读取", repo.data_status())
    except Exception as exc:
        return _fail(f"读取数据态失败：{type(exc).__name__}: {exc}")


def db_status(**_) -> dict:
    """巨益数据库凭据状态（不暴露口令）。

    账单整理**不连数据库**；ADB 仅 SKU 对账取数与回传真实落库需要（均已启用）。
    凭据未配置 / 连不上都属于「需要用户处理的引导态」，不是系统失败：
    action 层一律返回 ok=True，连通性放在 data.ok 里，由管理页渲染黄色引导。
    """
    try:
        res = db.test_connection()
    except Exception as exc:
        return _ok(f"数据库探测异常：{type(exc).__name__}: {exc}",
                   {"ok": False, "stage": "probe_error", "message": str(exc)[:300],
                    "needed_by": "SKU对账取数 / 数据库回传真实落库（回传缺省 dry-run 不连库）"})
    ready = bool(res.get("ok"))
    msg = res.get("message") or res.get("hint") or ("已连接" if ready else "未就绪")
    res = dict(res)
    res["needed_by"] = "SKU对账取数 / 数据库回传真实落库（账单整理不需要数据库；回传缺省 dry-run 不连库）"
    return _ok(str(msg), res)


# ------------------------------------------------------------------ 平台与规则


def platforms(**_) -> dict:
    """已接入平台清单与各自需要的附加数据源。"""
    rows = list_platforms()
    return _ok(f"已接入 {len(rows)} 个平台", rows)


def query_rules(platform: Any = "", active_only: Any = "1", limit: Any = 500, **_) -> dict:
    """查询平台解析规则（数据源为 rules/{platform}.json，不在数据库）。"""
    plat = parse_platform(str(platform or ""))
    rows = rules_store.query_rules(
        plat, active_only=str(active_only) not in ("0", "false", "False"),
        limit=_int(limit, 500))
    return _ok(f"{plat or '全部平台'}：{len(rows)} 条规则（文件态）",
               {"rows": rows, "rules_dir": str(rules_store.rules_dir())})


def upsert_rule(platform: Any = "", rule_kind: Any = "", rule_key: Any = "",
                rule_value: Any = "", note: Any = "", active: Any = "1", **_) -> dict:
    """新增/修改一条平台解析规则（如 biz_code: 0010001 → 货款），写入规则文件。"""
    plat = parse_platform(str(platform or ""))
    kind = str(rule_kind or "").strip()
    key = str(rule_key or "").strip()
    if not plat or not kind or not key:
        return _fail("platform / rule_kind / rule_key 均必填")
    if kind not in rules_store.VALID_RULE_KINDS:
        return _fail(
            f"rule_kind 非法：{kind}"
            f"（可选 {'/'.join(rules_store.VALID_RULE_KINDS)}）")
    try:
        rules_store.upsert_rule(plat, kind, key, str(rule_value or ""), note=str(note or ""),
                                active=str(active) not in ("0", "false", "False"))
    except ReconError as exc:
        return _fail(exc.message, data=exc.detail)
    except Exception as exc:
        return _fail(f"写入规则失败：{type(exc).__name__}: {exc}")
    return _ok(f"规则已保存到文件：{plat}/{kind}/{key} = {rule_value}",
               {"rule_file": str(rules_store._path(plat))})  # noqa: SLF001


def delete_rule(platform: Any = "", rule_kind: Any = "", rule_key: Any = "", **_) -> dict:
    """停用一条规则（置 active=0，保留条目便于追溯）。"""
    plat = parse_platform(str(platform or ""))
    kind = str(rule_kind or "").strip()
    key = str(rule_key or "").strip()
    if not plat or not kind or not key:
        return _fail("platform / rule_kind / rule_key 均必填")
    try:
        rules_store.delete_rule(plat, kind, key)
    except ReconError as exc:
        return _fail(exc.message, data=exc.detail)
    except Exception as exc:
        return _fail(f"停用规则失败：{type(exc).__name__}: {exc}")
    return _ok(f"规则已停用：{plat}/{kind}/{key}")


# ------------------------------------------------------------------ 执行


def start_recon(stage: Any = "bill", platform: Any = "", shop_code: Any = "",
                period: Any = "", s_date: Any = "", e_date: Any = "",
                file_paths: Any = "", extra_files: Any = "", export: Any = "1",
                agent_session_id: Any = "", bill_batch_id: Any = "",
                cost_file: Any = "", include_tax: Any = "0",
                lookback_months: Any = "", dry_run: Any = "1", **_) -> dict:
    """触发对账作业（异步 Job）。2026-11-18 cost-match spec C5：按 stage 分发。

    stage=bill  账单整理（platform/period/file_paths 必填）；
    stage=match SKU 对账（bill_batch_id 或 period+platform+shop_code 定位账单批次）；
    stage=cost  成本匹配（period 必填；cost_file 缺省查 结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx）；
    stage=writeback 数据库回传（period 必填；dry_run 缺省 1=预检不写库，
                    真实落库须显式 dry_run=0 且经用户确认）；
    stage=chuanyun 氚云对接（period 必填；dry_run 缺省 1=预检零 HTTP，
                    真实推送须显式 dry_run=0 且经用户确认）；
    full 仍占位暂缓（status=deferred）。
    """
    st = str(stage or "bill").strip().lower()
    if st in DEFERRED_STAGES and st not in ACTIVE_STAGES:
        return _deferred(st)
    if st not in ACTIVE_STAGES:
        return _fail(
            f"stage 非法：{st}（当前仅支持 {'/'.join(ACTIVE_STAGES)}；"
            f"{'/'.join(DEFERRED_STAGES)} 暂缓开放）")

    plat = parse_platform(str(platform or "")) if str(platform or "").strip() else ""
    params: dict[str, Any] = {"stage": st}

    if st == "bill":
        if not plat:
            return _fail("platform 必填（如 taobao / douyin / jd / pdd）")
        if not (str(period or "").strip() or str(s_date or "").strip()):
            return _fail("period 或 s_date/e_date 至少提供一组")
        files = _as_list(file_paths)
        if not files:
            # 无账单文件就不该启动作业：旧版会起一个必失败的 Job，
            # 在「作业与日志」里留一条红色失败记录，看起来像插件坏了。
            return _fail("file_paths 必填（账单目录或文件，分号分隔）；"
                         "只想验证映射请用 preview_bill（不落盘）")
        params.update({
            "platform": plat,
            "shop_code": str(shop_code or ""),
            # 店铺名称留空 → pipeline 查店铺映射取氚云名称（基准 Excel 同口径）
            "shop_name": "",
            "period": str(period or ""),
            "s_date": str(s_date or ""),
            "e_date": str(e_date or ""),
            "file_paths": files,
            "extra_files": _as_file_map(extra_files),
            "export": str(export if export != "" else "1"),
        })
    elif st == "match":
        if not (str(bill_batch_id or "").strip()
                or (str(period or "").strip() and plat)):
            return _fail("match 需要 bill_batch_id，或 period+platform（+shop_code）定位账单整理批次")
        params.update({
            "platform": plat, "shop_code": str(shop_code or ""),
            "period": str(period or ""), "s_date": str(s_date or ""),
            "e_date": str(e_date or ""),
            "bill_batch_id": str(bill_batch_id or ""),
            "export": str(export if export != "" else "1"),
        })
        if str(lookback_months or "").strip():
            params["lookback_months"] = str(lookback_months).strip()
    elif st == "cost":
        if not str(period or "").strip():
            return _fail("cost 需要 period（YYYY-MM）")
        params.update({
            "period": str(period).strip(),
            "cost_file": str(cost_file or ""),
            "bill_batch_id": str(bill_batch_id or ""),
            "platform": plat, "shop_code": str(shop_code or ""),
            "include_tax": str(include_tax or "0"),
        })
    elif st == "writeback":
        if not str(period or "").strip():
            return _fail("writeback 需要 period（YYYY-MM）")
        params.update({
            "period": str(period).strip(),
            "bill_batch_id": str(bill_batch_id or ""),
            "platform": plat, "shop_code": str(shop_code or ""),
            "s_date": str(s_date or ""), "e_date": str(e_date or ""),
            # 缺省 dry-run 预检：只统计+SQL预览，不连库不写库（2026-11-18 用户决策）
            "dry_run": "0" if str(dry_run or "1").strip() in ("0", "false", "False") else "1",
        })
    elif st == "chuanyun":
        if not str(period or "").strip():
            return _fail("chuanyun 需要 period（YYYY-MM）")
        params.update({
            "period": str(period).strip(),
            "bill_batch_id": str(bill_batch_id or ""),
            "platform": plat, "shop_code": str(shop_code or ""),
            # 缺省 dry-run 预检：零 HTTP，不向氚云发送任何请求（2026-11-18 用户决策）
            "dry_run": "0" if str(dry_run or "1").strip() in ("0", "false", "False") else "1",
        })

    if str(agent_session_id or "").strip():
        params["agent_session_id"] = str(agent_session_id).strip()

    try:
        from extensions.jobs import get_extension_job_manager  # type: ignore

        job = get_extension_job_manager().start_job("recon_workbench", params=params)
    except Exception as exc:
        return _fail(f"作业启动失败：{type(exc).__name__}: {exc}")

    job_id = str((job or {}).get("job_id") or "")
    label = {"bill": "账单整理", "match": "SKU对账", "cost": "成本匹配",
             "writeback": "数据库回传", "chuanyun": "氚云对接"}[st]
    note = {
        "bill": "结果落 results/YYYY-MM/账单整理/{批次}/（重跑覆盖；爱库存无店铺段；"
                "唯品会为 YYYY-MM-唯品会-唯品中国，旬文件在目录内累积）",
        "match": "结果落 results/YYYY-MM/sku匹配/未匹配成本/{批次}/；不写生产库",
        "cost": "读 未匹配成本/ 批次底稿回填成本；全部成功后整批迁移 已匹配成本/（VIP 全旬成功才迁移）",
        "writeback": ("按 OMS 单据 id 回写 mid_jedz_datas（bill_state=已对账 / bill_period=中文账期）。"
                      + ("当前为 dry-run 预检：不连库不写库，只出统计与 SQL 预览"
                         if params.get("dry_run") == "1"
                         else "真实回传：成功后整批迁移 已回传数据库/，失败批次留守")),
        "chuanyun": ("「氚云导入模板」sheet → 店铺资金账单主表+子表 → CreateBizObject 推送。"
                     + ("当前为 dry-run 预检：零 HTTP 调用，payload 预览落 底稿/chuanyun_preview/"
                        if params.get("dry_run") == "1"
                        else "真实推送：成功文件移入批次内 氚云导入成功/，失败留守")),
    }[st]
    return _ok(
        f"{label}作业已启动（{plat or '全平台'} · {params.get('period') or params.get('s_date', '')}）",
        {"job_id": job_id,
         "params": {k: v for k, v in params.items() if k != "agent_session_id"},
         "stored_in_db": False,
         "output_note": note},
    )


def job_status(job_id: Any = "", **_) -> dict:
    """查询作业进度/终态；不传 job_id 返回最近一次。"""
    try:
        from extensions.jobs import get_extension_job_manager  # type: ignore

        mgr = get_extension_job_manager()
        jid = str(job_id or "").strip()
        if jid:
            job = mgr.get_job(jid)
            if not job:
                return _fail(f"未找到作业 {jid}")
            return _ok(
                f"作业 {jid}：{job.get('status')} {job.get('message') or ''}", job
            )
        jobs = mgr.list_jobs(plugin_name="recon_workbench", limit=10) or []
        if not jobs:
            return _ok("还没有对账作业记录", {})
        job = jobs[0]
    except Exception as exc:
        return _fail(f"查询作业失败：{type(exc).__name__}: {exc}")

    return _ok(
        f"作业 {job.get('job_id') or job.get('id')}：{job.get('status')} {job.get('message') or ''}",
        job,
    )


# ------------------------------------------------------------------ 账单整理读数


def preview_bill(platform: Any = "", file_paths: Any = "", limit: Any = 20, **_) -> dict:
    """账单解析试跑（不落盘、不入库）：读文件 → 清洗 → 归一，返回前 N 行与列名。

    新增平台或改规则后用它验证映射是否正确，对应原脚本「跑一次才知道对不对」。
    """
    plat = parse_platform(str(platform or ""))
    if not plat:
        return _fail("platform 必填")
    files = _as_list(file_paths)
    if not files:
        return _fail("file_paths 必填（目录或文件，分号分隔）")

    try:
        rules = rules_store.load_rules_map(plat)
        from core.platforms import ParseContext, get_adapter

        adapter = get_adapter(plat, rules)
        ctx = ParseContext(platform=plat, file_paths=files)
        frames = adapter.read(ctx)
        cleaned = [adapter.clean(f) for f in frames]
        import pandas as pd

        merged = pd.concat(cleaned, ignore_index=True) if cleaned else pd.DataFrame()
        if merged.empty:
            return _ok("文件已读取但没有数据行", {"columns": [], "rows": []})
        norm = adapter.normalize(merged)
        n = _int(limit, 20)
        rows = norm.head(n).to_dict(orient="records")
        rows = [{k: (None if v != v else v) for k, v in r.items()} for r in rows]  # NaN → None
        return _ok(
            f"{adapter.display_name} 解析成功：{len(merged)} 行原始 → {len(norm)} 行归一",
            {
                "platform": plat,
                "display_name": adapter.display_name,
                "raw_rows": int(len(merged)),
                "normalized_rows": int(len(norm)),
                "raw_columns": [str(c) for c in merged.columns][:40],
                "normalized_columns": [str(c) for c in norm.columns],
                "sample": rows,
                "rules_loaded": {k: len(v) for k, v in rules.items()},
                "rules_source": str(rules_store.rules_dir()),
            },
        )
    except ReconError as exc:
        return _fail(exc.message, data=exc.detail)
    except Exception as exc:
        return _fail(f"试跑失败：{type(exc).__name__}: {exc}")


def bill_batches(period: Any = "", platform: Any = "", shop_code: Any = "",
                 limit: Any = 50, **_) -> dict:
    """账单整理批次清单（扫 results/ 目录，新→旧）。数据库里没有账单结果。"""
    rows = repo.query_bill_batches(
        period=str(period or ""), platform=parse_platform(str(platform or "")),
        shop_code=str(shop_code or ""), limit=_int(limit, 50))
    cov = artifacts.coverage(limit=500)
    return _ok(f"账单批次 {len(rows)} 个（合计 {cov.get('detail_rows', 0)} 行）",
               {"rows": rows, "coverage": cov})


def query_bill_rows(batch_id: Any = "", period: Any = "", platform: Any = "",
                    shop_code: Any = "", limit: Any = 200, **_) -> dict:
    """账单标准行查询（读 parquet，金额已转元）。

    **只返回单个批次**：未指定 batch_id 时取该范围内最新一次整理。
    每次整理都新建批次目录，不收敛会把历次结果叠在一起。
    """
    try:
        rows = repo.query_bill_rows(
            batch_id=str(batch_id or ""), period=str(period or ""),
            platform=parse_platform(str(platform or "")),
            shop_code=str(shop_code or ""), limit=_int(limit, 200))
        meta = repo.bill_batch_detail(
            batch_id=str(batch_id or ""), period=str(period or ""),
            platform=parse_platform(str(platform or "")), shop_code=str(shop_code or ""))
    except Exception as exc:
        return _fail(f"读取账单结果失败：{type(exc).__name__}: {exc}")
    if not rows:
        return _ok("还没有账单整理结果（结果只落文件，不入库）",
                   {"rows": [], "batch": meta})
    return _ok(f"账单行 {len(rows)} 条（批次 {meta.get('batch_id', '-')}）",
               {"rows": rows, "batch": meta})


def query_job_log(limit: Any = 60, **_) -> dict:
    """作业留痕查询（读 logs/jobs.jsonl，新→旧）。"""
    rows = joblog.read_records(_int(limit, 60))
    return _ok(f"作业留痕 {len(rows)} 条（JSONL 文件态）",
               {"rows": rows, "log_file": joblog.stats().get("log_file", "")})


# ------------------------------------------------------------------ SKU对账 / 成本匹配（2026-11-18 启用）


def sku_match(platform: Any = "", shop_code: Any = "", period: Any = "",
              s_date: Any = "", e_date: Any = "", bill_batch_id: Any = "",
              lookback_months: Any = "", agent_session_id: Any = "", **_) -> dict:
    """SKU 对账（不带成本表，异步 Job）。2026-11-17 启用、2026-11-18 接线补齐。

    输入 = 账单整理批次（bill_batch_id 或 period+platform+shop_code 定位）；
    产物落 sku匹配/未匹配成本/，不写生产库。
    """
    return start_recon(stage="match", platform=platform, shop_code=shop_code,
                       period=period, s_date=s_date, e_date=e_date,
                       bill_batch_id=bill_batch_id, lookback_months=lookback_months,
                       agent_session_id=agent_session_id)


def cost_match(period: Any = "", cost_file: Any = "", bill_batch_id: Any = "",
               platform: Any = "", shop_code: Any = "", include_tax: Any = "0",
               agent_session_id: Any = "", **_) -> dict:
    """实际成本匹配（异步 Job）。2026-11-18 cost-match-stage spec 启用。

    period 必填；cost_file 缺省查 结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx；
    bill_batch_id 缺省 = 该账期全部 SKU 对账批次（含 已匹配成本/ 重跑）。
    成功后整批迁移 未匹配成本→已匹配成本（VIP 全旬成功才迁移）。
    """
    return start_recon(stage="cost", period=period, cost_file=cost_file,
                       bill_batch_id=bill_batch_id, platform=platform,
                       shop_code=shop_code, include_tax=include_tax,
                       agent_session_id=agent_session_id)


def sku_batches(period: Any = "", platform: Any = "", shop_code: Any = "",
                state: Any = "", **_) -> dict:
    """SKU 对账批次清单（扫 sku匹配/{状态}/，供 cost 表单选批次）。

    state 缺省列 未匹配成本+已匹配成本 两态；可指定单态。
    """
    st = str(state or "").strip()
    states = (st,) if st in artifacts.SKU_STATES else (
        artifacts.SKU_STATE_PENDING_COST, artifacts.SKU_STATE_COSTED)
    if not str(period or "").strip():
        return _fail("period 必填（YYYY-MM）")
    try:
        batches = artifacts.list_sku_batches(
            str(period).strip(), states=states,
            platform=parse_platform(str(platform or "")) if str(platform or "").strip() else "",
            shop_code=str(shop_code or ""))
    except Exception as exc:
        return _fail(f"扫描 SKU 批次失败：{type(exc).__name__}: {exc}")
    rows = []
    for b in batches:
        units = artifacts.sku_batch_units(b.dir)
        rows.append({
            "batch_id": b.batch_id, "dir": str(b.dir), "name": b.dir.name,
            "state": b.dir.parent.name, "platform": b.platform,
            "shop_code": b.shop_code, "shop_name": b.shop_name,
            "period": b.period, "status": b.status,
            "units": [u.get("label") or "(整批)" for u in units],
            "excel": b.files.get("excel", ""),
        })
    return _ok(f"SKU 批次 {len(rows)} 个（{'/'.join(states)}）", {"rows": rows})


def writeback(period: Any = "", bill_batch_id: Any = "", platform: Any = "",
              shop_code: Any = "", s_date: Any = "", e_date: Any = "",
              dry_run: Any = "1", agent_session_id: Any = "", **_) -> dict:
    """数据库回传（异步 Job，2026-11-18 启用，承接影刀 look_sku.update_sql）。

    按 SKU 对账产物「已匹配的巨益单号」的 OMS 单据 id 回写 ADB：
    UPDATE mid_jedz_datas SET bill_state='已对账', bill_period=中文账期 WHERE id=%s。
    **dry_run 缺省 1 = 预检**：只统计 + SQL 预览，不连库不写库（当前版本纪律）。
    真实落库须显式 dry_run=0 且先经用户确认；成功后批次迁移 已回传数据库/。
    """
    return start_recon(stage="writeback", period=period, bill_batch_id=bill_batch_id,
                       platform=platform, shop_code=shop_code,
                       s_date=s_date, e_date=e_date, dry_run=dry_run,
                       agent_session_id=agent_session_id)


def chuanyun_sync(period: Any = "", bill_batch_id: Any = "", platform: Any = "",
                  shop_code: Any = "", dry_run: Any = "1",
                  agent_session_id: Any = "", **_) -> dict:
    """氚云对接（异步 Job，2026-11-18 启用，承接影刀 api_chuanyun.file_arrange）。

    把账单整理交付 Excel 的「氚云导入模板」sheet 经五张氚云主数据表映射，
    组装「店铺资金账单」主表+子表 → OpenAPI CreateBizObject 推送。
    **dry_run 缺省 1 = 预检**：零 HTTP 调用（主数据只读查询也不发），
    只做模板校验 + 金额合计 + payload 预览落盘（底稿/chuanyun_preview/）。
    真实推送须显式 dry_run=0 且先经用户确认（凭据走 RECON_H3_ENGINE_CODE/SECRET）；
    成功文件移入批次内 氚云导入成功/。用 job_status 查终态。
    """
    return start_recon(stage="chuanyun", period=period, bill_batch_id=bill_batch_id,
                       platform=platform, shop_code=shop_code, dry_run=dry_run,
                       agent_session_id=agent_session_id)


# ------------------------------------------------------------------ 独立透视汇总工具（2026-11-18）


def bill_pivot_summary(period: Any = "", platform: Any = "", shop_code: Any = "",
                       bill_batch_id: Any = "", **_) -> dict:
    """店铺账单整理透视汇总（同步，承接影刀 cost_match.final_bill_pivot）。

    扫该账期账单整理批次交付 Excel 的 8 列透视 sheet（按列结构识别，
    不依赖 sheet 位置），concat 输出「账单整理透视汇总表_{时间戳}.xlsx」
    到 results/{YYYY-MM}/汇总/，附下载链接与收/退款/现金流合计。
    纯本地聚合：不连数据库、不推氚云、不修改批次产物。period 必填。
    """
    try:
        res = summaries.bill_pivot_summary(
            period=str(period or "").strip(),
            platform=parse_platform(str(platform or "")) if str(platform or "").strip() else "",
            shop_code=str(shop_code or ""), bill_batch_id=str(bill_batch_id or ""))
    except ReconError as exc:
        return _fail(exc.message, error_detail=exc.detail)
    except Exception as exc:
        return _fail(f"账单透视汇总失败：{type(exc).__name__}: {exc}")
    return _ok(res["message"], res)


def sku_pivot_summary(period: Any = "", platform: Any = "", shop_code: Any = "",
                      bill_batch_id: Any = "", state: Any = "", **_) -> dict:
    """店铺sku匹配透视汇总（同步，承接影刀 cost_match.final_pivot）。

    扫该账期 SKU 批次交付 Excel 的 5 sheet：店铺/款号/匹配三张透视 concat；
    收退款明细按 店铺名称×订单年月 汇总货款分摊金额后 outer merge。
    输出「sku匹配数据透视汇总表_{时间戳}.xlsx」4 sheet 到 results/{YYYY-MM}/汇总/。
    state 缺省三态全扫（同一批次只在一个状态目录，不重复）；可指定单态。
    纯本地聚合：不连数据库、不推氚云、不修改批次产物。period 必填。
    """
    try:
        res = summaries.sku_pivot_summary(
            period=str(period or "").strip(),
            platform=parse_platform(str(platform or "")) if str(platform or "").strip() else "",
            shop_code=str(shop_code or ""), bill_batch_id=str(bill_batch_id or ""),
            state=str(state or ""))
    except ReconError as exc:
        return _fail(exc.message, error_detail=exc.detail)
    except Exception as exc:
        return _fail(f"SKU透视汇总失败：{type(exc).__name__}: {exc}")
    return _ok(res["message"], res)


# ------------------------------------------------------------------ 设置与历史清理（2026-11-16）


def get_settings(**_) -> dict:
    """读插件设置：结果存储路径（自定义/默认）与当前生效目录。"""
    from core import config as cfg

    settings = cfg.load_settings()
    custom = str(settings.get("results_dir") or "").strip()
    effective = cfg.results_root()
    return _ok(
        f"结果存储路径：{effective}" + ("（自定义）" if custom else "（默认）"),
        {
            "results_dir": custom,
            "effective_dir": str(effective),
            "default_dir": str(cfg.default_results_dir()),
            "is_custom": bool(custom),
            "settings_file": str(cfg.settings_path()),
        },
    )


def save_settings(results_dir: Any = None, **_) -> dict:
    """保存设置。results_dir 传空串=恢复默认；非空则校验可创建可写后落 settings.json。"""
    from core import config as cfg

    patch: dict = {}
    if results_dir is not None:
        raw = str(results_dir or "").strip().strip('"')
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                return _fail(f"结果存储路径必须是绝对路径：{raw}")
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".recon_write_probe.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                return _fail(f"结果存储路径不可写：{raw}（{exc}）")
            patch["results_dir"] = str(p)
        else:
            patch["results_dir"] = ""
    try:
        cfg.save_settings(patch)
    except OSError as exc:
        return _fail(f"保存设置失败：{exc}")
    return get_settings()


def clear_history(confirm: Any = "", period: Any = "", platform: Any = "",
                  shop_code: Any = "", **_) -> dict:
    """清空历史记录（2026-11-18 改版：只清插件内信息，**不删输出目录任何文件**）。

    清理范围：作业留痕 logs/jobs.jsonl（支持条件过滤）+ downloads 下载副本
    （插件运行时缓存，仅全清时删）。输出目录（results/ 或自定义 results_dir）
    下的批次目录、交付 Excel、底稿、汇总簿一律原样保留——批次清单是扫目录
    得来的文件态，清记录后批次仍可见，这是预期行为。
    保留：店铺映射、rules/ 规则、settings.json。
    必须显式传 confirm="yes" 才执行（UI 二次确认 / AI 先向用户确认），防误触。
    细粒度：给 period/platform/shop_code 任一时只删命中条件的作业留痕，
    downloads 副本保留；全空则留痕全清 + downloads 清空。
    """
    if str(confirm or "").strip().lower() != "yes":
        return _fail("清空历史需要二次确认（confirm=yes）。只清插件内记录（作业留痕/下载副本），输出目录文件不受影响。")
    try:
        res = artifacts.clear_history(period=str(period or "").strip(),
                                      platform=str(platform or "").strip(),
                                      shop_code=str(shop_code or "").strip())
    except Exception as exc:
        return _fail(f"清空历史失败：{type(exc).__name__}: {exc}")
    if res.get("scoped"):
        sc = res.get("scope") or {}
        cond = "/".join(v for v in (sc.get("period"), sc.get("platform"), sc.get("shop_code")) if v)
        msg = f"已按条件（{cond}）清理作业留痕 {res['joblog_removed']} 条（输出目录文件与下载副本保留）"
    else:
        msg = (f"已清空插件内记录：作业留痕 {res['joblog_removed']} 条、下载副本 {res['downloads_removed']} 个"
               "（输出目录的批次/Excel/底稿全部保留）")
    if res["errors"]:
        msg += f"；{len(res['errors'])} 项删除失败（可能被占用）"
    return _ok(msg, res)


# ------------------------------------------------------------------ 本地数据源


def load_shop_file(file_path: Any = "", version: Any = "", **_) -> dict:
    """导入店铺信息表（原 resources/OMS店铺.xlsx）到 plugin.db —— 数据库唯一保留的数据。"""
    path = str(file_path or "").strip()
    if not path or not Path(path).exists():
        return _fail(f"店铺表路径不存在：{path}")
    try:
        import pandas as pd

        df = pd.read_excel(path, index_col=None, dtype={"shopId": "str", "shopCode": "str"})
    except Exception as exc:
        return _fail(f"读取店铺表失败：{type(exc).__name__}: {exc}")

    if df.empty or "shopCode" not in df.columns:
        return _fail("店铺表缺少 shopCode 列", data={"columns": [str(c) for c in df.columns][:20]})

    import time as _time

    ver = str(version or "").strip() or _time.strftime("%Y%m%d%H%M%S")
    rows = []
    for rec in df.to_dict(orient="records"):
        code = str(rec.get("shopCode") or "").strip()
        if not code:
            continue
        name = str(rec.get("shopName") or "").strip()
        rows.append({
            "shop_code": code,
            "shop_id": str(rec.get("shopId") or "").strip(),
            "shop_name": f"{code}_{name}" if name else code,
            "platform": str(rec.get("platform") or rec.get("平台") or "").strip(),
            "chuanyun_name": str(rec.get("氚云名称") or "").strip(),
            "active": 1,
            "source_file": Path(path).name,
            "version": ver,
        })
    if not rows:
        return _fail("店铺表没有有效行")
    try:
        n = repo.save_shops(rows)
    except Exception as exc:
        return _fail(f"写入店铺映射失败：{type(exc).__name__}: {exc}")
    return _ok(f"店铺映射已导入：{n} 家（版本 {ver}）",
               {"version": ver, "rows": n, "table": repo.T_SHOP})


def shops(keyword: Any = "", limit: Any = 500, **_) -> dict:
    """店铺清单查询（供选择店铺编码）。数据库唯一保留的表。"""
    try:
        rows = repo.query_shops(keyword=str(keyword or ""), limit=_int(limit, 500))
    except Exception as exc:
        return _fail(f"查询店铺映射失败：{type(exc).__name__}: {exc}")
    return _ok(f"店铺 {len(rows)} 家", rows)
