# 规则剥离后 drop_tail 混入 pandas 读取参数 TypeError

| Field | Value |
|-------|-------|
| id | `2026-11-16-drop-tail-non-pandas-read-key` |
| date | 2026-11-16 |
| status | fixed |
| severity | medium |
| area | `rules_store` |

## Symptom

内置规则种子到 rules/taobao.json 后，淘系解析报
`read_csv() got an unexpected keyword argument 'drop_tail'`。

## Root cause

drop_tail 是**清洗参数**（_read_alipay 里 self.rule() 消费），不是 pandas
读取参数；种子把它写进 read_option 后，read_options() 原样并入 **opts
传给 read_csv。剥离前它只在代码 default 里、从不进 opts，故未暴露。

## Fix

base.py 增 `_NON_PANDAS_READ_KEYS = ("drop_tail",)`，read_options() 跳过
这些键（仍可由 self.rule("read_option","drop_tail") 读取）。

## Files

- `core/platforms/base.py`

## Tests

第一批 4 平台 + 第二批 12 用例 + vip 三旬全量回归 ALL_OK。

## Regression guard

新增非 pandas 的 read_option 键必须同步进 _NON_PANDAS_READ_KEYS。
