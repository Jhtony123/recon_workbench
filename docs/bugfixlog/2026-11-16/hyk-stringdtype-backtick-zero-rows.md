# 好衣库 StringDtype 剥反引号失效致 0 行 + 商品数量文本列

| Field | Value |
|-------|-------|
| id | `2026-11-16-hyk-stringdtype-backtick-zero-rows` |
| date | 2026-11-16 |
| status | fixed |
| severity | high |
| area | `platform_rules` |

## Symptom

HYK001 8月账单账期过滤后 0 行（隐蔽假空，不报错）；商品数量合计与基准不可比。

## Root cause

dtype='str' 读入是 StringDtype（不是 object），旧 `dtype==object` 判断整列跳过
剥反引号 → 资金日期 '`2026-08-31…' 解析 NaT → 账期过滤全灭。商品数量是文本列。

## Fix

一律 `astype(str)` 后剥 \`，不依赖 dtype 判断；商品数量 to_numeric 转 int64。

## Files

- `core/platforms/hyk.py`

## Tests

HYK001 2026-08：14642 行标准账单 / 明细 4386 行，现金流硬门对平，逐 sheet 一致。

## Regression guard

全列反引号剥离不得按 dtype 条件跳过；0 行结果必须核对日期解析而非当作无数据。
