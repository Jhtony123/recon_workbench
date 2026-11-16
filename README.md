# 对账工作台（recon_workbench）

电商财务月度对账工作台插件，承接原影刀 RPA「资金账单匹配sku对账」（9013 行），
去 xbot 依赖、模块化分层。**AI 原生**：操作由 AI 通过 MCP 工具执行，
管理页是只读看板 + Excel 下载 + 清空历史人工入口。

- id: `recon_workbench` · activate: `/ext:recon_workbench`
- entry: `run.py` → `core.pipeline.run_bill`（stage=bill 真执行；其余能力占位暂缓）
- 远程仓库: https://github.com/Jhtony123/recon_workbench

## 能力面（2026-11-14 方案改版）

| 能力 | 状态 |
|------|------|
| 账单整理（11 平台） | ✅ 可用：淘系/抖音/京东/拼多多/快手/视频号/小红书/有赞/好衣库/爱库存/唯品会 |
| SKU对账 / 成本匹配 / 数据库回传 / 氚云对接 | ⏸ 占位暂缓（算法封存 `core/pipeline_dormant.py`） |

数据库**只存店铺映射**；账单结果落文件（交付 Excel + 机器可读表格，金额内部整数分）；
规则文件态 `rules/{platform}.json`（builtin-default 种子 259 条，可编辑）；
作业留痕 `logs/jobs.jsonl`。

## 目录

```
run.py / actions.py / config.json   Job 入口 / MCP action / 工具声明 SSOT
core/                               编排 + 11 平台适配器 + 落盘 + 规则 + 导出
rules/                              默认可编辑解析规则（JSON）
ui/                                 vanilla 只读看板（9 页面）
skills/recon_assist/                AI 运行时说明书 + 12 份平台漂移文档
scripts/                            基准回归 + 规则种子脚本
docs/                               codemap / bugfixlog / specs（开发迭代体系）
```

## 开发迭代

先读 [`docs/README.md`](./docs/README.md)：Spec → Locate(codemap) → Edit →
Verify(基准回归 ALL_OK + validate + 热重载 + UI 探活) → Record(bugfixlog) → Push。

回归基准：影刀 8 月真实账单（外部路径见 `docs/codemap/index.json` external_refs），
比对覆盖逐 sheet/列头/行数/**全部数值列合计**（防假绿）。
