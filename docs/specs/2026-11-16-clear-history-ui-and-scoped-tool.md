# 总览页清空历史控件 + 细粒度清理工具

| Field | Value |
|-------|-------|
| date | 2026-11-16 |
| status | implemented |
| scope | ui/index.html + ui/app.js / actions.clear_history / artifacts.clear_history |

## 背景与问题

AI 原生化改版把人工操作入口全部移除后，用户在首页找不到清空历史按钮，
无法人工清理；同时希望 AI 能按条件直接调用清理。

## 决策（用户确认）

1. 对账总览页新增「清空历史」面板：红框危险按钮 + **两步确认**
   （首次点击变红字提示，5 秒内再点才执行；WebView iframe 里 window.confirm
   可能被拦，用按钮态实现）+ 清理范围说明 + 结果回显 + 就地刷新总览；
2. 旁边「问 AI：按账期/平台清理」按钮（data-ask=clear）：AI 列条件组合让用户
   选，确认后才执行；
3. `clear_history` 工具增强：新增 period/platform/shop_code 可选条件，
   只删命中批次目录（downloads 副本与作业留痕保留，无法可靠归属单批次）；
   全空=全清（原行为）；confirm=yes 门保持。

## 实现要点

- artifacts.clear_history(scoped)：按 manifest+目录名双解析匹配条件；
  新格式目录需平台名可识别或含 manifest 才认作批次（防误删手工目录）；
- actions.clear_history 透传条件并给条件化 summary；config.json params 同步。

## 验收

UI 探活零报错（面板渲染、12 按钮）；实测确认门拒绝空 confirm、不存在账期
0 删除、youzan/2026-08 条件正确命中 2 批次（未动真实数据）。

## 遗留与后续

- 无。
