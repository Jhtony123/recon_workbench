# 京东导出文件名用数据窗口月份而非账期（跨月窗口写成 09月-10月）

| Field | Value |
|-------|-------|
| id | `2026-11-16-jd-filename-window-vs-period` |
| date | 2026-11-16 |
| status | fixed |
| severity | medium |
| area | `platform_rules` / `export_format` |

## Symptom

10 月 JD001 只给 period 自动推「上月末~月末」窗口（09-30~10-31）后，
导出文件名成 `…_2026年09月-2026年10月_京东钱包整理.xlsx`，
基准是 `…_2026年10月-2026年10月_…`，回归报 [MISSING]。

## Root cause

ParseContext.period_cn 用 s_date/e_date 生成中文标签；京东的数据窗口
（一天到账延迟补跑）跨月，把窗口月份当成了业务账期。8 月显式日期回归
（08-01~08-31）不跨月故未暴露。

## Fix

base.ParseContext.period_cn：platform=jd 时只用 period 生成标签
（period_label(self.period)），数据窗口仅用于过滤不进业务标签。

## Files

- `core/platforms/base.py`

## Tests

10 月 JD001 重跑：文件名 2026年10月-2026年10月，4515 行逐 sheet 对平 [OK]。

## Regression guard

京东业务标签一律用账期；数据窗口（s/e）不得进文件名/账单周期字段。
