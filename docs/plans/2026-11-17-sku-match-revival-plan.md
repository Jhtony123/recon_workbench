# 实施 Plan — SKU 对账启用（账单追账 · 不带成本第一步）

| Field | Value |
|-------|-------|
| date | 2026-11-17 |
| spec | [../specs/2026-11-17-sku-match-revival.md](../specs/2026-11-17-sku-match-revival.md)（confirmed，Q1–Q8 全确认） |
| 业务 SSOT | skills/recon_assist/references/sku-match-methodology.md |
| 状态 | 实施中 |

## 0. 现状盘点（探查结论，决定 task 粒度）

| 资产 | 现状 | 对 task 的影响 |
|---|---|---|
| core/db.py | **已存在且完整**：fetch_oms_rows（lookback 默认 6、ORDER BY id ASC、退款单去重）、fetch_product_info（分批4000）、fetch_order_dates（ORDER BY 取最新）、凭据走 RECON_ADB_* 环境变量、adb_cursor 上下文 | T1 从「新建 oms.py」降为「核验+补缺」，不新建模块 |
| core/config.py | DEFAULT_LOOKBACK_MONTHS=6 已定；period_label 中文标签已实现 | Q1/Q2 代码侧已就绪 |
| core/matching.py | match_all（finalize 开关）/分摊后终判 finalize_detail/build_oms_writeback/recount 齐全；**无 B2B 分支** | T2 需新增 B2B 匹配（爱库存/唯品会） |
| core/allocation.py | 分 int 四级分摊 + verify_or_raise 对平门 | 直接复用 |
| core/pivots.py / cost.py / exporters.py | 透视/成本 enrich/6-sheet 导出已存在；cost.force_std_as_ship 支持唯品会 FR | T2 复用；Q3 笔误需核对 pivots 是否已带病迁移 |
| core/artifacts.py | read_goods / match_window / resolve_batch 已就绪（注释明确「SKU 对账启用后从这里取输入」）；**无 sku匹配 目录族** | T3 新增目录族与落盘函数 |
| core/pipeline_dormant.py | run_match（L293-559）完整但依赖退役库表：repo.query_goods_window/query_goods_rows/T_MATCH/T_PIVOT_* insert | T2 搬出+改造：读改 artifacts、写改文件 |
| core/joblog.py + repo.save_job_log | 存活（jsonl 留痕，不入库） | 直接复用 |
| run.py / actions.py / config.json / UI | ACTIVE_STAGES=("bill",)；sku_match 工具与 match 页均占位 | T4 恢复 |

## 1. Task 分解与实施路径

### T1 取数层核验（db.py，小改）
- 核验 fetch_oms_rows 窗口 = s_date-6月 ~ e_date+1天、三类拆分、退款单去重键；
- 核验凭据链：进程环境 → 用户级注册表兜底；缺失抛 ConfigError（黄色引导态）；
- 用 db.test_connection() 实测连通（需机器已配 RECON_ADB_*；未配则先向用户要配置，不硬编码）；
- 补缺：lookback_months 可从批次参数覆盖（run_match 已有 kwargs 通道，核验即可）。
- **验收**：test_connection ok；HYK001 试取数行数量级合理（半年窗口）。

### T2 run_match 接线（核心，pipeline.py）
1. 从 dormant 搬 run_match 及私有辅助（_to_int/_to_text/_detail_to_db_rows 中仍需要的部分）；
2. **读侧改造**：_load_bill_frame/_resolve_bill_batch/query_goods_window →
   artifacts.resolve_batch + read_goods + match_window（京东匹配窗口≠账期，dormant 注释的
   JD001 实测教训必须保留）；
3. **写侧改造**：repo.insert_rows(T_MATCH/T_PIVOT_SHOP/T_PIVOT_PRODUCT) →
   artifacts 落 parquet 底稿 + T3 的 Excel 交付；ALLOWED_TABLES 不动；
   OMS 快照样本（save_oms_snapshot）改落底稿 parquet 或暂缓；
4. **顺序纪律**（dormant 注释的实测教训，逐条保留）：
   match_all(finalize=False) → 分摊 verify_or_raise → finalize_detail 终判 → 回灌收/退 →
   成本 enrich → 透视 → recount 重算 stats；
5. **B2B 分支新增**（matching.py 或独立 core/b2b.py）：爱库存/唯品会不查 OMS，
   账单自带 sku/数量 → 收款结转成本/退款冲成本；爱库存款号=sku[:-5]（账单层已推导，核验）；
   唯品会 FR 标准成本=结算金额（收退两侧）+ force_std_as_ship；回传表空；
   唯品会账期标签带日（period_label 已支持，核验）；
6. **Q3 笔误核对**：检查 pivots.py 款号透视-退款段是否带病迁移了影刀 L1804-1805
   链式赋值与 L1536 空表判断；带病则按意图修正，登记 bugfixlog；
7. ACTIVE_STAGES 增 "match"，DEFERRED_STAGES 移除 match；dormant 文件头同步更新。
- **验收**：单店（DY 或 HYK）跑通 stage=match，分摊差异 0 分，6-sheet 落盘，joblog 留痕。

### T3 artifacts sku匹配目录族 + 导出
- 新增 `{period}/sku匹配/{未匹配成本|已匹配成本|已回传数据库}/{批次名}/`；本期只写 未匹配成本/；
- 批次名沿用账单整理命名 `YYYY-MM-平台-店铺-编号`（爱库存/唯品会特例沿用）；
- 每店一份 6-sheet Excel（exporters.export_match_result 已有，改落位）+ parquet 底稿 + manifest；
- 重跑同名覆盖（防 resolve 叠批教训）；扫描/清理函数兼容新目录族。
- **验收**：目录结构符合 2026-11-16 全流程 spec 状态机；重跑不产生重复批次。

### T4 入口恢复（run.py / actions / config.json / UI）
- run.py：stage=match 走真执行（参数：bill_batch_id 或 period+platform+shop_code、lookback_months 可选）；
- actions.py：sku_match 从 _deferred 改真实现（触发异步 Job）；start_recon 支持 stage=match；
- config.json：sku_match 工具声明恢复参数与描述；match 页标题去「暂缓」；
- UI：占位卡换真实表单（批次选择 + 触发 + 结果下载）；capability_status 同步。
- **验收**：MCP 工具触发 match Job 成功；UI 表单可选批次、可看终态、可下载 Excel。

### T5 基准回归（防假绿）
- 工作区 scripts/verify_match_revival.py：以影刀历史运行结果为基准；
- 用例：**≥3 家 B2C（必含 HYK001）+ 爱库存 + 唯品会（单旬）**；
- 逐 sheet / 列头 / 行数 / **全部数值列（含毛利率 bp 列）** 比对；
- 预期差异白名单：Q3 两处笔误修正行、订单日期取最新（既有有意改进）、ORDER BY id 行序；
  白名单外差异=FAIL；分摊对平门每店恒 0 分；
- OMS 无数据店 → skipped 不产半成品批次。
- **验收**：ALL_OK（白名单差异单独列示人工确认）。

### T6 收尾
- validate_extension + 热重载 + UI 探活零报错；
- codemap index.json/README 更新（match 激活、新模块）；bugfixlog 登记 Q3 修正；
- SKILL.md 能力状态段同步（match 可用）；
- 最终 commit + push。

## 2. 依赖与风险

| 风险 | 对策 |
|---|---|
| RECON_ADB_* 未配置 → T1/T5 阻塞 | 先 test_connection 探测；缺失即向用户要配置，绝不硬编码 |
| 单店半年 OMS 40 万行，回归耗时长 | 后台跑+落日志；先单店冒烟再扩用例；最小必要重跑集（教训） |
| B2B 分支无封存代码可搬 | 按影刀 B2B_match（L2031-2520）新写，口径以方法论 §8 为准 |
| 影刀基准行序不可复现 | ORDER BY id 已定；逐行比对用白名单机制，金额合计恒等是硬门 |
| 中途 kill 留半截批次 | 失败批次目录清理后再重跑；joblog 终态必须写 |

## 3. 执行顺序

T1 → T2 → T3 → T4 → T5 → T6（串行，T2/T3 可交叉）；
文档阶段成果（spec/skill/plan）先行 commit+push，代码按 task 分次 commit。