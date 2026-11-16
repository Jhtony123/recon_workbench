# Code Map — recon_workbench 代码结构索引

> 最后更新：**2026-11-16** · 机器可读索引：[`index.json`](./index.json)
> 参照 AI_RPA `docs/codemap` 规范（Locate → Verify → Edit → Sync）。

## 架构一图

```
Desktop 宿主 (sidecar :8765)
  └─ extensions/user/recon_workbench/
       run.py            Job 入口（stage=bill 真执行；其余 deferred 占位）
       actions.py        MCP action 实现（23 个工具的后端）
       config.json       工具声明 / UI 页面 / 能力开关（SSOT）
       core/
         pipeline.py     编排：run_bill → 唯品会多旬分发 → _run_bill_single
         platforms/      11 平台适配器（base.py 骨架 + registry.py 注册）
         artifacts.py    批次目录/manifest/表格落盘（新命名+覆盖+vip两层）
         rules_store.py  rules/{platform}.json 文件态规则（原子写+mtime缓存）
         exporters.py    影刀基准格式 Excel 导出
         repo.py / db.py 店铺映射（数据库唯一表）
         pipeline_dormant.py  SKU对账/成本/回传封存算法（暂缓）
       rules/            默认可编辑规则（builtin-default 种子 259 条）
       ui/               vanilla JS 只读看板 + 清空历史人工入口
       skills/recon_assist/  AI 运行时说明书 + 12 份平台漂移文档
       docs/             本体系（codemap / bugfixlog / specs）
```

## 按任务查找代码

| 我想… | 先看 | task_lookup key |
|-------|------|-----------------|
| 新增平台适配器 | `core/platforms/base.py` 骨架 + `registry.py` 注册 + 种子脚本 | `add_platform` |
| 改某平台分类口径 | `core/platforms/{name}.py` + `skills/.../platform-{name}.md` | `platform_rules` |
| 改规则热修通道 | `core/rules_store.py` + `base.biz_code_map/prefixed_map/read_options` | `rules_store` |
| 改输出目录/覆盖策略 | `core/artifacts.py` bill_root / build_batch_dir / iter_batch_dirs | `batch_layout` |
| 改 zip 解压入口 | `core/platforms/base.py` collect_files / _extract_zip / expand_source_paths | `zip_input` |
| 改唯品会多旬分发 | `core/pipeline.py` _vip_expand_xuns / _vip_run_xuns | `vip_xuns` |
| 改京东日期窗口 | `core/pipeline.py` _run_bill_single 开头 jd 分支 | `jd_window` |
| 改 Excel 导出格式 | `core/exporters.py` + 各适配器 export_books | `export_format` |
| 加/改 MCP 工具 | `config.json` mcp_tools + `actions.py` 同名函数 | `mcp_tool` |
| 改管理页 UI | `ui/index.html` + `ui/app.js`（PAGE_LOADERS / bindAll） | `ui_page` |
| 改清空历史 | `actions.clear_history` + `artifacts.clear_history`（scoped） | `clear_history` |
| 启用 SKU 对账 | `core/pipeline_dormant.py` + `storage.ALLOWED_TABLES` 白名单评审 | `dormant_revive` |
| 跑基准回归 | 工作区 `scripts/verify_export_baseline.py` / `verify_batch2.py` | `regression` |
| 登记已修 bug | [`../bugfixlog/`](../bugfixlog/README.md) | `bugfixlog` |

## 入口点速查

| 用途 | 路径 |
|------|------|
| Job 入口 | `run.py` → `core.pipeline.run_bill` |
| MCP action | `actions.py`（config.json 声明驱动） |
| 平台注册表 | `core/platforms/registry.py` `_ADAPTERS`（11 平台） |
| 规则种子 | 工作区 `scripts/seed_builtin_rules.py`（幂等，不覆盖已改条目） |
| 基准回归 | 工作区 `scripts/verify_export_baseline.py`（第一批）/ `verify_batch2.py`（第二批+vip） |
| 结果落盘 | 用户自定义目录（settings.results_dir）→ `YYYY-MM/账单整理/YYYY-MM-平台-店铺-编号/`（2026-11-16 全流程 spec；同级预留 sku匹配/ 状态机） |

## 维护约定

- 结构性变更（新模块/新平台/新工具）→ 更新 `index.json` 的 modules/task_lookup 与本页日期。
- 行为口径变更 → 先写 `docs/specs/`，再同步 `skills/recon_assist/references/`。
- 本索引手工维护（项目规模小，暂不建 sync 脚本）；每次 push 前过一遍是否过期。
