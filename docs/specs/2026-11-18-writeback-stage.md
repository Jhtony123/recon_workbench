# 数据库回传阶段启用（writeback stage）— 2026-11-18

状态：**implemented**（代码功能完成；按用户决策**不真实更新数据库**，入口恒缺省 dry-run）

## 背景与用户决策

前序已完成：账单整理（bill）→ SKU对账（match）→ 成本匹配（cost），全量回归通过
（41 单元 / 124.9 万行，见 2026-11-18-cost-match-stage.md）。本阶段承接影刀
「匹配单据回传数据库」分支（main.py L662-670 → look_sku.update_sql L2524）。

**用户明确要求（2026-11-18）**：只做代码功能，把影刀代码移植过来（可优化适配），
**不要真的去更新数据库**。因此：
- `dry_run` 缺省 `1`（actions / run.py / UI 三层一致）；
- UI 回传页按钮恒 `dry_run=1`，真实落库无页面入口；
- 真实路径代码完整保留（`dry_run=0` 显式触发 + 用户确认后才允许），已用 FakeConn
  全量验证，但本次实施**未对生产库执行任何 UPDATE**。

## 影刀原逻辑（look_sku.update_sql）

1. 扫 data_path 下全部 xlsx，读 sheet「已匹配的巨益单号」（id 列 str）；
2. `period = df['账单周期'].iloc[0]`（中文账期标签，VIP 带日）；
3. `UPDATE mid_jedz_datas SET bill_state=%s, bill_period=%s WHERE id=%s`，
   参数 ("已对账", period, id)，executemany 1000/批 + commit，失败 rollback；
4. 8 线程并发（每文件独立连接）；成功文件 move 到 `执行成功/`；
5. 缺陷：`except: pass` 吞异常、明文口令、连接泄漏、失败仅写桌面 txt。

## 插件实现（优化适配）

| 层 | 文件 | 要点 |
|---|---|---|
| 引擎 | `core/writeback.py` | `prepare_rows`（id 去重去空）/ `resolve_label`（优先底稿中文标签，回落 period_label）/ `sql_preview` / `writeback(dry_run=True)`；真实路径 executemany 1000/批 + commit/rollback，4 并发，批失败聚合 failed_detail 不静默；凭据走环境变量（db.adb_connection） |
| 编排 | `core/pipeline.py run_writeback` | 批次发现 = sku匹配/{未匹配成本\|已匹配成本}/（bill_batch_id 可选过滤）；逐单元读底稿 `oms_matched` 表；空表 skipped（B2B/AKC 不回传）；**真实回传**全单元成功才 `migrate_sku_batch → 已回传数据库/`（影刀「移动到执行成功/」的文件态等价），失败留守；joblog 留痕 |
| 连接 | `core/db.py adb_connection` | 新增连接级 contextmanager（写路径需要 conn.commit/rollback；只读仍用 adb_cursor） |
| 入口 | `run.py` / `actions.py` / `config.json` | stage=writeback 进 ACTIVE_STAGES；`writeback()` MCP 工具恢复（period 必填，dry_run 缺省 1）；start_recon 增 dry_run 参数；capability_status 四能力 active |
| UI | `ui/index.html` / `app.js` | #/writeback 真实表单：账期+批次下拉（三态批次表）、预检按钮（恒 dry_run=1）、SQL 预览面板；占位卡仅剩氚云 |

数据口径与 match 阶段闭环：oms_matched 底稿由 `matching.build_oms_writeback`
生成，已按「巨益单据匹配状态=已匹配 **且** 历史对账记录=未对账」双条件过滤，
不会覆盖历史已对账单据的真实账期。

## 验收（不连真实库）

1. `scripts/verify_writeback.py`：**38/38 PASS**
   - SQL 模板与影刀逐字一致；参数三元组 (已对账, 中文账期, id)；
   - dry-run 路径 monkeypatch 连接工厂为「调用即失败」→ 证明不连库；
   - FakeConn 捕获：2500 行 → 3 连接 3 commit、批大小 500/1000/1000、id 全覆盖无重复；
   - 批失败 rollback + failed 明细；连接失败聚合 failed 不抛栈；
   - 端到端（临时 results 根）：dry-run 不迁移 / 真实成功迁移 已回传数据库/ /
     失败留守 / 空批次跳过 / joblog 留痕 / period 必填。
2. `scripts/t7_writeback_dryrun.py`（宿主 MCP 通道真实 Job，恒 dry-run）：**4/4 ALL_OK**
   - 全账期 2026-10：37 单元 / **172,040 条**预检通过，stored_in_db=False、migrated=[]；
   - HYK 单批次：**303 条**（与影刀 10 月基准 HYK 303 行一致）；
   - capability active 含「数据库回传」；`已回传数据库/` 目录 0 批次（零迁移）。
3. `validate_extension` ok；`reload_extension_runtime` + `inspect_extension_ui`
   静态资源/控制台/探活全绿。

## 真实启用条件（日后）

用户确认 → AI 显式 `writeback(period=…, dry_run=0)`（或 start_recon stage=writeback
dry_run=0）→ 环境变量凭据就绪（RECON_ADB_*）→ 成功后批次自动迁移 已回传数据库/。
建议先单店试跑核对 rowcount，再全账期。