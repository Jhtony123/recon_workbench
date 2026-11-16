# 唯品会款号前缀规则遇空款号 NaN 崩溃（StringDtype 同款坑再现）

| Field | Value |
|-------|-------|
| id | `2026-11-16-vip-nan-kh-len-typeerror` |
| date | 2026-11-16 |
| status | fixed |
| severity | high |
| area | `platform_rules` |

## Symptom

10 月第三旬（2026.10.11-10.20）parse 崩
`TypeError: object of type 'float' has no len()`（vip.py detail_frame
款号前缀规则循环）；前两旬正常。8 月三旬回归未暴露。

## Root cause

`df["款号"].astype("str")` 在 StringDtype 下 NaN 仍是 float，
`len(kh)` 崩——与快手 NaN strip、好衣库反引号同族坑。
第三旬数据含空款号行（H 表差异详情等来源），8 月样本恰好没有。

## Fix

改 `df["款号"].fillna("").astype(str)` 后再进前缀规则循环。

## Files

- `core/platforms/vip.py`

## Tests

第三旬重跑 422,354 行成功；三旬交付件 vs 10 月基准
逐 sheet/列头/行数/全部数值列合计 ALL_OK（verify_vip_oct.py）。

## Regression guard

StringDtype 列进 len/startswith 等 str 操作前必须 fillna("")；
新账期数据可能带旧样本没有的空值形态，平台循环逐行处理处都要防 NaN。
