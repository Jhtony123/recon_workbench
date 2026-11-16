# 有赞 csv 行尾多空字段致 pandas 索引推断整表错位

| Field | Value |
|-------|-------|
| id | `2026-11-16-youzan-trailing-fields-index-misalign` |
| date | 2026-11-16 |
| status | fixed |
| severity | high |
| area | `zip_input` |

## Symptom

用户删掉解压文件后从原始 zip 解压重跑：货款 0 行、收款 15,268,969.91（真值 39,567.24）、
退款恒 0、「类型」列变成订单号。此前用影刀预处理过的 csv 验证从未暴露。

## Root cause

有赞原始 csv 列头 21 列、数据行 23 个字段（行尾多 2 个空字段）。pandas 默认
index_col 推断把前两列当索引 → 整表错位。base.read 写死 index_col=None 也无法覆盖。

## Fix

youzan default_read_options 加 index_col=False；base.read 把 index_col 从 opts
提取单独传参（允许 read_option 覆盖）。

## Files

- `core/platforms/youzan.py`
- `core/platforms/base.py`

## Tests

YZ002（1260 行/货款 359）/ YZ003（157 行/货款 34）zip 入口 MCP 实测 + 基准逐 sheet 对平。

## Regression guard

有赞必须用**原始下载** csv 验证（预处理副本会掩盖此坑）；index_col=False 不得移除。
