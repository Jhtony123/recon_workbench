# 回归比对列漏项造成「全绿假象」：毛利率列恒 NULL 三层回归不报

| Field | Value |
|-------|-------|
| id | `2026-11-14-regression-missing-columns-fake-green` |
| date | 2026-11-14 |
| status | fixed |
| severity | high |
| area | `regression` |

## Symptom

三层回归全绿（账单 30/30、匹配 8/8、成本 5/5），但浏览器实测店铺/款号透视
「账单毛利率」「订单毛利率」两列全部为空（23964/23964 行 NULL），毛利额却有值。

## Root cause

回归脚本比对列集合只放金额与数量列、从未包含毛利率列——结构性缺陷
（bp 列被裁掉、入库恒 NULL）在回归里不可能被报出，全绿是假绿。

## Fix

比对改为**全部数值列合计**（现 verify_export_baseline.py / verify_batch2.py
的 num_cols 扫全部列头），并逐 sheet/列头/行数比对。

## Files

- 工作区 `scripts/verify_export_baseline.py` / `scripts/verify_batch2.py`（随仓库 scripts/ 分发）

## Tests

此后所有平台对接（第一批+第二批+vip 三旬）均按全列比对验收。

## Regression guard

**比对必须覆盖全部数值列**；新增导出列自动进比对，不得手工挑列。
