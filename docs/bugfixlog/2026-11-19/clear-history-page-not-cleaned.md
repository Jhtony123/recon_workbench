# 清空历史「失效」：清完页面数字/批次表不变

| Field | Value |
|-------|-------|
| date | 2026-11-19 |
| status | fixed |
| scope | core/config.py · core/artifacts.py · core/repo.py(经 coverage/list_batches) · core/pipeline.py · core/summaries.py · actions.py · config.json · ui/index.html · ui/app.js · skills/recon_assist/SKILL.md · scripts/verify_clear_history.py |

## 现象

用户点对账总览「清空历史」（两步确认）后，作业留痕归 0，但账单批次 41、
账单行 1,033,395、货款行 619,505、账期/平台覆盖、最近批次表原样残留，
用户判定「清理失效」。

## 根因

2026-11-18 改版把 clear_history 收敛为「只清插件内记录（jobs.jsonl +
downloads）」，而总览数字来自 artifacts.coverage() → list_batches()
**扫输出目录**（文件态）。清记录不动输出目录 → 页面统计必然不变。
这是口径与用户预期（页面清干净）的冲突，不是按钮没触发。

## 修复（清理水位方案，不违背「不删产出」纪律）

1. 全清时写运行时水位 `data/extensions/recon_workbench/history_cleared.json`
   （cleared_at 时间戳）；条件清理不记水位。
2. `list_batches` 新增 `include_hidden=False` 缺省：展示层
   （coverage/overview/data_status/bill_batches）按 manifest created_at
   （回落目录 mtime）< 水位 隐藏旧批次 → 页面数字归零、表格清空。
3. **执行链路显式 include_hidden=True**：resolve_batch（SKU对账/成本/回传）、
   氚云 run_chuanyun、账单透视汇总——清历史不影响后续作业取输入。
4. 水位之后重跑的批次重新可见；恢复历史展示 = 删 history_cleared.json。
5. UI：清空面板/设置页文案、armed toast、结果回显同步新口径；
   clearHistoryFlow 补 catch（原来异常静默）。
6. actions.clear_history summary 报「页面已隐藏历史批次 N 个」；
   config.json 工具描述与 SKILL.md 确认纪律同步。

## 验收

- scripts/verify_clear_history.py 45/45 PASS（临时目录，不碰真实数据）：
  输出目录逐文件零变化（含 mtime）、展示清空、执行链路仍见批次、
  新批次可见、删水位恢复。
- 真实环境：clear_history(confirm=yes) 隐藏 41 批次 → overview 批次/行/
  货款全 0、periods/platforms/recent_batches 空；reload_extension_runtime
  + inspect_extension_ui 控制台零报错。输出目录 C:\Users\贺杰\Desktop\账单
  文件全部保留。

## 教训

「只清记录不删文件」的改版必须同时回答**展示层从哪取数**：数据源是扫目录
时，清记录永远清不干净页面；隐藏水位（展示过滤）与物理删除要分开设计。
