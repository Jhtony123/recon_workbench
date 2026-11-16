# 设置页存储路径设成自定义后无法再修改（编辑入口被改版误删）

| Field | Value |
|-------|-------|
| id | `2026-11-16-settings-dir-readonly-no-edit-entry` |
| date | 2026-11-16 |
| status | fixed |
| severity | medium |
| area | `ui_page` |

## Symptom

设置自定义存储路径（C:\Users\贺杰\Desktop\账单）后，#/settings 页只剩
「当前生效：…（自定义）」只读文字，没有输入框和保存按钮，页面上无法再次修改
或恢复默认，只能回会话找 AI 调 save_settings。

## Root cause

2026-11-16 AI 原生化改版把「设置保存」当作人工操作入口整体移除，设置页改成
纯只读展示；但 loadSettingsPage() 仍在找 `set-results-dir` 输入框（不存在，
`if (input)` 静默跳过），后端 save_settings 工具完好——纯前端入口缺失，
控制台零报错。与「清空历史」同性质：配置类低频操作也应有页面人工入口。

## Fix

- index.html：补回 `set-results-dir` 输入框 + 「保存路径」「恢复默认」按钮
  + `set-save-result` 结果回显；lead 文案改为「本页可直接修改」；
- app.js：新增 `saveResultsDir(restore)`（restore=true 传空串=恢复默认；
  成功/失败均 toast + 就地回显 + 刷新当前生效行），bindAll 绑定两按钮；
- styles.css：`.field-label` / `#set-results-dir` 等宽字体 / 结果行间距。

## Files

- `ui/index.html`
- `ui/app.js`
- `ui/styles.css`

## Tests

reload_extension_runtime + inspect_extension_ui：静态资源/9 页面/控制台零报错，
按钮数 12→14；save_settings 同路径重写实测 ok（用户既有设置未变）。
后端校验链（绝对路径 + mkdir + 写探针）复用原逻辑未动。

## Regression guard

配置类操作（存储路径/清空历史）必须保留页面人工入口；删除 UI 控件时同步
检查 JS 里引用该 id 的代码是否变成静默死分支（`if (el)` 吞掉缺失）。
