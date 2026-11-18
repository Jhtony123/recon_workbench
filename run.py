"""对账工作台 — Job 入口（2026-11-14 方案改版：只有账单整理会真正执行）。

约定：导出 run(*, progress, params=None, ctx=None) -> dict

params:
  stage         bill|match|cost|writeback|chuanyun（可执行；writeback/chuanyun
                缺省 dry_run=1 预检，不写库/不向氚云发请求）/ full（占位暂缓 deferred）
  platform      平台标识或中文名（taobao / douyin / jd / pdd ...）
  shop_code     店铺编码
  shop_name     店铺名称（缺省用 shop_code）
  period        账期 'YYYY-MM'
  s_date/e_date 账期起止 'YYYY-MM-DD'
  file_paths    账单目录或文件（分号分隔，或 JSON 数组）
  extra_files   附加源 {"wechat": [...], "ecard": [...], "wallet": [...]}
  export        0 则不导出 Excel（parquet 与 manifest 仍会落盘）
  agent_session_id  传入则完成后通知本会话（铃铛 + 下一轮 hint）

已移除的 params（对应能力占位暂缓，传了也忽略）：
  cost_file / cost_mode / cost_sheet / include_tax / force_std_as_ship /
  lookback_months / use_db / writeback / dry_run / save_oms_snapshot / oms_snapshot_limit

失败一律返回 ok=False + 可读 message，绝不 exit()。
占位阶段返回 status="deferred" + ok=True（不是失败，也不是成功）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

_PLUGIN_DIR = Path(__file__).resolve().parent
for _p in (str(_PLUGIN_DIR), r"D:\DesktopAgent"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PLUGIN_NAME = "recon_workbench"


def _progress_wrapper(progress: Callable[[int, str], None]) -> Callable[[int, str], None]:
    """包一层：保证 progress 异常不影响主流程（并转达 Runtime Kit 的取消信号）。"""

    def fn(pct: int, msg: str) -> None:
        try:
            progress(int(pct), str(msg))
        except Exception:
            # 取消信号（ExtensionJobCancelled）必须向上抛，其余进度失败忽略
            name = type(progress).__name__
            if "Cancel" in name:
                raise
    return fn


def _deferred_payload(stage: str) -> dict[str, Any]:
    """占位阶段的 Job 终态：ok=True + status=deferred，并写一条留痕便于追溯。"""
    from core import joblog, pipeline

    res = pipeline.deferred_result(stage)
    batch_id = f"deferred-{stage}-{time.strftime('%Y%m%d%H%M%S')}"
    try:
        joblog.save_job_log(
            batch_id, stage=stage, status="deferred", message=res.message,
            duration_sec=0.0,
        )
    except Exception:
        pass  # 留痕失败不该让占位终态变成红色报错
    return {
        "ok": True,
        "status": "deferred",
        "message": res.message,
        "row_count": 0,
        "stage": stage,
        "batch_id": batch_id,
        "data": dict(res.data or {}),
    }


def run(
    *,
    progress: Callable[[int, str], None],
    params: dict[str, Any] | None = None,
    ctx: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    params = {**(params or {}), **kwargs}
    _ = ctx
    prog = _progress_wrapper(progress)

    stage = str(params.get("stage") or "bill").strip().lower()

    t0 = time.time()
    try:
        from core import joblog, pipeline  # noqa: WPS433 延迟导入，保证 Job 轻量启动
    except Exception as exc:
        return {
            "ok": False, "status": "failed",
            "message": f"引擎加载失败：{type(exc).__name__}: {exc}",
        }

    # 占位暂缓：不执行、不写数据、不连数据库
    if stage in pipeline.DEFERRED_STAGES and stage not in pipeline.ACTIVE_STAGES:
        return _deferred_payload(stage)
    if stage not in pipeline.ACTIVE_STAGES:
        return {
            "ok": False, "status": "failed",
            "message": (
                f"未知阶段：{stage}（可执行：{'/'.join(pipeline.ACTIVE_STAGES)}；"
                f"暂缓开放：{'/'.join(pipeline.DEFERRED_STAGES)}）"
            ),
        }

    try:
        # 按 stage 分发（2026-11-18 cost-match spec C5：补上 match 的 Q8 遗留，
        # 不再统一调 run_bill）。
        if stage == "match":
            res = pipeline.run_match(
                platform=str(params.get("platform") or ""),
                shop_code=str(params.get("shop_code") or ""),
                shop_name=str(params.get("shop_name") or ""),
                period=str(params.get("period") or ""),
                s_date=str(params.get("s_date") or ""),
                e_date=str(params.get("e_date") or ""),
                bill_batch_id=str(params.get("bill_batch_id") or ""),
                lookback_months=params.get("lookback_months"),
                export=str(params.get("export", "1")) not in ("0", "false", "False"),
                progress=prog,
            )
        elif stage == "cost":
            res = pipeline.run_cost(
                period=str(params.get("period") or ""),
                cost_file=str(params.get("cost_file") or ""),
                bill_batch_id=str(params.get("bill_batch_id") or ""),
                platform=str(params.get("platform") or ""),
                shop_code=str(params.get("shop_code") or ""),
                include_tax=str(params.get("include_tax", "0")) in ("1", "true", "True"),
                progress=prog,
            )
        elif stage == "writeback":
            # 2026-11-18 启用：缺省 dry-run 预检（不连库不写库）；
            # 真实落库须显式 dry_run=0（用户确认后才允许）。
            res = pipeline.run_writeback(
                period=str(params.get("period") or ""),
                bill_batch_id=str(params.get("bill_batch_id") or ""),
                platform=str(params.get("platform") or ""),
                shop_code=str(params.get("shop_code") or ""),
                s_date=str(params.get("s_date") or ""),
                e_date=str(params.get("e_date") or ""),
                dry_run=str(params.get("dry_run", "1")) not in ("0", "false", "False"),
                progress=prog,
            )
        elif stage == "chuanyun":
            # 2026-11-18 启用：缺省 dry-run 预检（零 HTTP，不向氚云发送任何请求）；
            # 真实推送须显式 dry_run=0（用户确认后才允许）。
            res = pipeline.run_chuanyun(
                period=str(params.get("period") or ""),
                bill_batch_id=str(params.get("bill_batch_id") or ""),
                platform=str(params.get("platform") or ""),
                shop_code=str(params.get("shop_code") or ""),
                dry_run=str(params.get("dry_run", "1")) not in ("0", "false", "False"),
                progress=prog,
            )
        else:
            res = pipeline.run_bill(
                platform=str(params.get("platform") or ""),
                shop_code=str(params.get("shop_code") or ""),
                shop_name=str(params.get("shop_name") or ""),
                period=str(params.get("period") or ""),
                s_date=str(params.get("s_date") or ""),
                e_date=str(params.get("e_date") or ""),
                file_paths=pipeline._as_list(params.get("file_paths")),
                extra_files=pipeline._as_file_map(params.get("extra_files")),
                batch_id=str(params.get("batch_id") or ""),
                export=str(params.get("export", "1")) not in ("0", "false", "False"),
                progress=prog,
            )
        out = _stage_result(res)

    except Exception as exc:  # 业务异常与意外异常都要有可读终态
        exc_name = type(exc).__name__
        message = str(getattr(exc, "message", "") or exc)[:500]
        detail = getattr(exc, "detail", None)
        prog(100, f"执行失败：{message}")
        try:
            joblog.save_job_log(
                str(params.get("batch_id") or f"failed-{int(t0)}"),
                stage=stage, status="failed",
                platform=str(params.get("platform") or ""),
                shop_code=str(params.get("shop_code") or ""),
                period=str(params.get("period") or ""),
                message=f"{exc_name}: {message}",
                duration_sec=time.time() - t0,
            )
        except Exception:
            pass
        return {
            "ok": False, "status": "failed",
            "message": f"{exc_name}: {message}",
            "error_detail": detail if isinstance(detail, dict) else {},
            "duration_sec": round(time.time() - t0, 2),
        }

    out.setdefault("duration_sec", round(time.time() - t0, 2))
    return out


def _stage_result(res) -> dict[str, Any]:
    """StageResult → Job 终态字典。"""
    data = dict(res.data or {})
    row_count = int(
        data.get("bill_rows")
        or data.get("matched_rows")
        or data.get("total")
        or 0
    )
    return {
        "ok": res.status not in ("failed",),
        "status": res.status,
        "message": res.message,
        "row_count": row_count,
        "stage": res.stage,
        "batch_id": res.batch_id,
        "data": data,
    }
