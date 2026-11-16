# 快手资金账单 NaN 单号 strip 崩溃 + 货款退款组件名/类别混淆

| Field | Value |
|-------|-------|
| id | `2026-11-16-ks-nan-strip-and-refund-component` |
| date | 2026-11-16 |
| status | fixed |
| severity | high |
| area | `platform_rules` |

## Symptom

KS001 8月账单解析 TypeError（float.strip）；修复后导出 KeyError「货款退款」。

## Root cause

① StringDtype 下 NaN 仍是 float，`.map(str.strip)` 对 NaN 崩溃（资金转账类无关联单号）。
② 订单流水组件列名「货款退款」是独立明细列，业务类别须映射回「货款」，两者混用。

## Fix

① 改 `.astype(str).str.strip().replace("nan","")`；② ORDER_COMPONENTS 组件名与
业务类别映射分离（货款退款→货款）。

## Files

- `core/platforms/kuaishou.py`

## Tests

KS001 2026-08 基准回归：64 行标准账单，硬门对平后逐 sheet 一致（verify_batch2.py ks）。

## Regression guard

NaN 处理保持 .str.strip() 写法；组件列名与业务类别不得合并。
