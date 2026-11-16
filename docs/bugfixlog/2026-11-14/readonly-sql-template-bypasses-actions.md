# read_only sql_template 工具不经 actions.py：后端后处理是死代码

| Field | Value |
|-------|-------|
| id | `2026-11-14-readonly-sql-template-bypasses-actions` |
| date | 2026-11-14 |
| status | fixed |
| severity | medium |
| area | `mcp_tool` |

## Symptom

给工具在 actions.py 补后处理逻辑，探活返回数据完全没变化；
另一类表现：UI rowsOf() 只认 res.data，read_only 工具行在顶层 rows，
表格恒空且控制台 0 报错（假空态）。

## Root cause

config.json 里 permission=read_only + sql_template 的工具由宿主
execute_readonly_query 直接查库返回，不路由到 actions.py 同名函数；
两类工具响应契约不同（action: data.rows；read_only: 顶层 rows）。

## Fix

需要后处理的工具声明为 permission=action；UI rowsOf() 兼容两种契约。
（该教训源自 hongguo_drama 插件，同类结构在本插件同样适用。）

## Files

- `config.json`（工具声明）
- `ui/app.js`（rowsOf）

## Tests

inspect_extension_ui 探活 + 页面实测数据可见。

## Regression guard

加后处理前先确认工具 permission；改响应结构必须同步 UI 取数契约。
