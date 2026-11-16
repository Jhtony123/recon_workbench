# 唯品会不含税金额舍入顺序漂移（先 round 再除税）

| Field | Value |
|-------|-------|
| id | `2026-11-16-vip-round-before-tax-division` |
| date | 2026-11-16 |
| status | fixed |
| severity | medium |
| area | `platform_rules` |

## Symptom

vip2（2026.08.01-08.10）不含税结算金额合计与基准差 0.14 元。

## Root cause

折扣/价保/差异类用未舍入的含税金额直接除税；原脚本（bill_arr 4101-4102 行）
先把含税金额 round(2) 再除税再 round(2)。另 H 表税率**不 round**（4714 行），
与其余 sheet 不同。

## Fix

折扣/价保/差异段：amt.round(2) → /(1+tax.round(2)) → round(2)；H 表税率保持不 round。

## Files

- `core/platforms/vip.py`

## Tests

三旬全量回归：vip1 301497 / vip2 289895 / vip3 278951 行，全部数值列合计对平。

## Regression guard

舍入顺序即口径；H 表与其余 sheet 的税率处理差异不得"统一"。
