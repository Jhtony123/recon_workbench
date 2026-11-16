# zip 内层目录名带冒号落盘 WinError 123 + 纯 zip 目录判定防双份计账

| Field | Value |
|-------|-------|
| id | `2026-11-16-zip-colon-winerror-and-pure-zip-guard` |
| date | 2026-11-16 |
| status | fixed |
| severity | high |
| area | `zip_input` |

## Symptom

① 有赞原始 zip 自动解压报 `OSError [WinError 123]`：内层目录名
`2026-08-01 00:00:00-…` 带冒号，Windows 非法。
② 京东目录既有已解压子目录又有 zip 时，_roots 误解压 zip 导致结算单被当
2 列 csv 读取（ParserError: Expected 2 fields, saw 10）。

## Root cause

① _extract_zip 未净化路径段非法字符。② expand_source_paths 初版只排除
数据文件不排除子目录，「有子目录+zip」被当纯 zip 目录解压。

## Fix

① 逐段 `re.sub(r'[\\/:*?"<>|]', "_", seg)` 净化（保留目录结构，兼防 zip-slip）。
② 纯 zip 目录判定改为「无任何非 zip 条目（文件+子目录）」才解压；
collect_files 同口径：目录已能直接收集到账单文件时不再碰 zip。

## Files

- `core/platforms/base.py`

## Tests

有赞 zip 入口 MCP 实测 1260 行对平；京东 JD001 回归 17670 行对平（第一批 ALL_OK）。

## Regression guard

zip+已解压副本共存时绝不双份计账；解压路径段必须净化 Windows 非法字符。
