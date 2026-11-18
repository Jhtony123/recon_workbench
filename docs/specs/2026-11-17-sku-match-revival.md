# SKU 对账启用（账单追账 · 不带成本第一步）

| Field | Value |
|-------|-------|
| date | 2026-11-17 |
| status | implemented（2026-11-17 启用并三轮精修对齐基准；2026-11-18 全平台40批次回归+三项金额校验38/38 OK；入口接线遗留见文末「实施现状」） |
| scope | core/pipeline.py、core/matching.py、core/allocation.py、core/pivots.py、core/db.py、core/artifacts.py、run.py、actions.py、config.json、UI；参照影刀 look_sku.py / main.py |

## 背景与问题

工程第二目标「货物视角」（2026-11-16 全流程 spec）：从账单整理结果分离**货款**收/退款，
按订单号撞巨益 OMS 出入库单据，分摊到 SKU，给出会计处理意见，支撑
主营业务成本 / 发出商品 / 不做处理 的判断。这就是业务上的「账单追账」：钱和货互相追认。

现状：
- 账单整理（stage=bill）已上线，10 月 41 批次 / 1,033,395 行全量对平；
- SKU 对账算法已迁移至 core/matching.py / allocation.py / pivots.py / cost.py / writeback.py，
  曾三层回归全绿（匹配 8/8、成本 5/5、分摊差异恒 0 分），但整体封存于
  pipeline_dormant.py 依赖链，未接线（ACTIVE_STAGES 仅 bill）；
- 封存代码依赖已退役库表（T_MATCH / T_PIVOT_* 等已从 storage.ALLOWED_TABLES 移除），
  直接运行会被白名单闸门拦下——须按「结果落文件」方案改造后启用。

本 spec 范围：**只启用 SKU 对账（match，不带成本）**。成本匹配 / 数据库回传 / 氚云回传
仍 deferred，口径沿用 2026-11-16 spec，届时另立 spec。

## 业务逻辑基线（影刀源码精读结论，迁移 SSOT）

源码：`E:\AIcoding_project\xbot_robot\look_sku.py`（2659 行）+ main.py 分流 +
cost_match.py / trade_no_fill.py / oms_shop.py。逐条口径：

> **业务逻辑 SSOT 已沉淀**：`skills/recon_assist/references/sku-match-methodology.md`
> （收款/退款两段对账、秒退交叉核对、取数规律、五类财务判断、对账状态月度闭环、实施检查单）。
> 本节为摘要，判定条件细节与行号以该文档为准；实施前必读。

### B1 输入准备
- 账单侧：读账单整理批次产物，只留 业务类别=货款；货款金额=round(收款,2)-round(退款,2)；
  收款、退款分别按订单号 sum 透视（一单一行）；空订单号填「无订单号」；金额 0 行剔除。
- OMS 侧（oms_sql_select，L168）：ADB `xianxia.mid_jedz_datas`，
  `bill_date >= s_date减X个月 AND bill_date < e_date+1天 AND shop_code=店铺编号`；
  X（month_no）原为影刀弹窗人工输入。拆三类单据：
  - OMS出库 = B2C配货单；OMS入库 = B2C退换货通知单；
  - OMS退款 = 退款单，按 单号+sku+结算金额 去重；
  - 关键字段：id（回传主键）、历史对账记录（对账状态列）、对账周期、结算金额；
  - 三类全空 → 终止（插件语义应为 status=skipped，新店首月属正常）。

### B2 匹配主链（result()，顺序不可变）
1. 收款 left join OMS出库 on 订单编号；一对多展开时重复行货款金额置 0；有单号=已匹配。
2. 退款两段：先 left join OMS入库；仍未匹配的再 left join 退款单；重复行同样置 0。
3. 秒退判断：未匹配收款订单在退款侧汇总货款金额，两边相等→标「是」；
   秒退收款单独再撞退款单；退款侧按单号/订单号反向标记。
4. 额外入库判断：账期内、历史未对账、未被退款核对占用的入库单，用 订单号_sku 找对应出库：
   - 出库存在且未参与收款核对且未对账 → 出库并入收款侧标「成本不做处理」，入库标「冲发出商品」；
   - 找不到对应出库 → 入库标「冲成本」。
5. 货款分摊（calculate_receipt_allocation，L56）四级策略：
   ①合计相等（容差 1e-6）→按结算金额直分；②单行等额→全给该行；
   ③子集组合 DP（SCALE=1e6 整数化，精确命中或 |差|≤1 即容差 1e-6 元）；
   ④按结算金额比例 round(2) 分摊，末行兜底补差。
   未匹配→分摊=货款原值；结算金额全≤0 且合计≈0→首行拿全部货款。
   **硬门：分摊合计与货款合计差异必须=0，否则整批失败。**
6. 产品信息回填：AI_product_info 按 sku 批量查（每批 4000）补
   款号/商品名称/品类/年份/季节/品牌/吊牌价/标准成本单价；标准成本=数量×标准成本单价；
   品牌字典归一（COCOON→CO、秋水伊人→QS、FirstView→FV…）。
7. 订单日期补全：缺失且非秒退 → je_saleorders 按 trade_no 查补（trade_no_fill.py）。
8. 处理意见判定：C=账单周期是否晚于历史对账周期。
   - 收款：B2C配货单+非秒退+C=0+历史未对账→结转成本；分摊≤0 或秒退→成本不做处理
     （历史未对账的改回未匹配）；
   - 退款：B2C退换货通知单同条件→冲成本；
   - 交叉修正：收款侧「成本不做处理」的 订单_sku → 退款侧冲成本改冲发出；
     收款侧「结转成本」的 → 退款侧冲发改回冲成本。
9. 回传表：历史未对账 & 已匹配的行按 id 透视 → [id, 账单周期, 数量]
   （「已匹配的巨益单号」sheet；本期只产出文件，不写生产库）。

### B3 输出（每店一份 6-sheet Excel）
店铺数据透视 / 款号数据透视 / 匹配透视 / 收款匹配 / 退款匹配 / 已匹配的巨益单号。
明细 Excel **31 列**（exporters.DETAIL_SHEET_COLUMNS 严格裁列，尾列=处理意见）。
~~原「32 标准列 + 数据来源 + 店铺编码」附加列白名单~~ **2026-11-18 用户确认作废**：
数据来源/店铺编码/成本缺失原因 是插件内部字段（分摊分组/成本诊断用），全平台影刀基准
实测均 31 列，内部字段仅保留在 底稿/match_detail.csv，不进 Excel。
透视口径：收款正数、退款负数；毛利=分摊金额-标准成本；毛利率 round(4)，分母 0→0；
退款侧另列 冲发出商品数量/金额；全 0 行删除。

### B4 平台分流
- B2C 平台走 B1–B3 全链；
- B2B（爱库存/唯品会）走 B2B_match：不查 OMS 出入库，账单自带 sku/数量直接算成本，
  「已匹配的巨益单号」为空；唯品会 FR 品牌成本=结算金额（特例）。
- 京东：结算单+钱包+E卡三源合并（jdwallet_time / bill_arrange_jd），
  钱包「货款退款」从备注正则提取订单号——该部分已在账单整理层实现，match 层只消费产物。

## 决策（2026-11-17 用户全部确认，逐条可验收）

| # | 问题 | 确认口径 | 状态 |
|---|------|----------|------|
| Q1 | OMS 取数窗口 X 个月默认值与配置方式 | settings 新增 `oms_lookback_months`，默认 **6**，UI 可按批次覆盖（收款结算滞后，须向前覆盖旧发货） | ✅ 已确认 |
| Q2 | ADB 凭据管理（影刀硬编码 host/user/password） | 移入本地 settings/环境变量（RECON_ADB_*），不入 git；连接失败→failed 并留痕 | ✅ 已确认（按建议默认） |
| Q3 | 源码笔误：L1804-1805 链式赋值（冲发出商品数量被标准成本覆盖）+ L1536 交叉修正二空表判断用错变量 | 两处均按意图修正；回归中与影刀基准的差异行单独标注、人工确认 | ✅ 已确认（按建议默认） |
| Q4 | 金额精度：影刀 float+round(2)，插件「分」int | 维持分 int；DP 精确命中（分级整数无浮点误差），比例分摊末行补差，对平门恒 0 分 | ✅ 已确认（按建议默认） |
| Q5 | 结果落盘形态 | 文件态：`{period}/sku匹配/未匹配成本/{批次名}/` 每店一份 6-sheet Excel + parquet 底稿；三态目录预留，本期只写 未匹配成本/；不加回任何库表（ALLOWED_TABLES 不动） | ✅ 已确认（按建议默认） |
| Q6 | 本期渠道范围 | **B2C 主链 12 渠道（含好衣库、京东）+ B2B 2 渠道（爱库存、唯品会）**。⚠️ 好衣库走 B2C 主链（查 OMS、参与状态回传），勿归入 B2B；分流实证见 sku-match-methodology.md §8.0 | ✅ 已确认（范围表述修正后确认） |
| Q7 | 产品字典/订单日期数据源 | 沿用 ADB AI_product_info / je_saleorders（与 OMS 同一连接）；连接失败→failed，查不到→填「无款号」等默认值继续；订单日期取最新 order_date（既有有意改进） | ✅ 已确认（按建议默认） |
| Q8 | 触发方式与入口 | run.py stage=match 真执行；输入=指定账单整理批次（bill_batches 选择）；UI 占位卡换真实表单；config.json 恢复 match 工具 | ✅ 已确认（按建议默认） |

## 实现要点（模块/函数级，确认后执行）

1. **core/oms.py（新增）**：ADB 连接（settings 凭据）+ oms_sql_select 等价取数
   （参数化 SQL，窗口=s_date-X月 ~ e_date+1天）+ 三类单据拆分/退款单去重 +
   产品字典批量查询 + 订单日期补全查询。只读，无任何写生产动作。
2. **core/pipeline.py**：新增 `run_match(batch_id, period, shop, …)`：
   读账单整理批次 bill_goods → split_bill → match_receipt/match_refund →
   mark_instant_refund/rematch → supplement_oms_remainder → allocation（对平门）→
   产品回填 → finalize_advice → pivots → build_oms_writeback（仅落文件）→
   artifacts 落盘。ACTIVE_STAGES 增 "match"；DEFERRED_STAGES 移除 match。
3. **pipeline_dormant.py**：run_match 搬出后保留成本/回传段；文件头四步启用说明同步更新。
4. **core/artifacts.py**：新增 sku匹配 目录族（未匹配成本/已匹配成本/已回传数据库 三态
   预留，本期只写 未匹配成本/）；批次命名沿用 `YYYY-MM-平台-店铺-编号`。
5. **run.py / actions.py / config.json / UI**：stage=match 接线；批次选择表单；
   job_log 留痕（skipped=OMS 无数据；failed=对平门不过/连接失败）。
6. **规则外置**：品牌归一字典、处理意见标签等常量放 rules/ 或 config，不硬编码散落。

## 验收（回归命令 + 预期 ALL_OK 范围）

- 工作区 `scripts/verify_match_revival.py`（新建）：选 ≥3 家 B2C 店（**必含好衣库 HYK001**，
  验证其走 B2C 主链查 OMS）+ 爱库存、唯品会各 1 份（B2B 分流），
  以影刀历史运行结果（`E:\AIcoding_project\xbot_robot\历史运行结果`）为基准，
  逐 sheet / 列头 / 行数 / **全部数值列**（含毛利率 bp 列，防假绿教训）比对；
  Q3 若按意图修正，差异行单独列出并人工确认；
- 分摊对平门：每店 分摊合计-货款合计 ≡ 0 分；
- OMS 无数据店铺 → status=skipped 且不产半成品批次；
- validate_extension + 热重载 + UI 探活零报错；
- 全程无写生产库动作（代码评审确认 oms.py 只读）。

## 遗留与后续

- 成本匹配（cost_match 含税口径 /1.13、FR 特例）→ 下一份 spec；
- 数据库回传（update_sql 等价，显式确认门 + 成功迁移/失败留守状态机）→ 下一份 spec；
- 氚云模板导出、跨店合并大表工具 → 独立任务；
- 影刀源码中其余疑似笔误如回归中发现，登记 bugfixlog 后再定修正口径。

## 实施现状（2026-11-18 更新）

**已完成**：
- run_match 全链启用（ACTIVE_STAGES=(bill, match)），10 月 40 批次（除 VIP 单独跑）实测；
- 与影刀 10 月基准（`桌面\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证`）逐 sheet 对齐：
  12 店完全一致；其余差异经行级归因均为 **OMS 数据时点漂移**（单据状态/字典/数量事后更新、
  基准生成后新录单据）+ 白名单笔误修正（冲发出商品数量 84 行），无逻辑回归；
- 三项金额校验 38/38 OK（scripts/verify_match_totals.py）：秒退逐单收付货款相等、
  货款合计=分摊合计、与账单整理 bill_goods 收/退合计对平（含京东三源）；
- 明细裁列 31 列（e14fab7）；回归/归因/校验脚本入库（08c620b、55102f3）。

**遗留（Q8 入口接线未完全落地）**：
- run.py stage=match 虽过 ACTIVE_STAGES 门，但后续仍统一调 run_bill（未按 stage 分发到
  run_match）；actions.start_recon 同样硬编码 stage=bill 且强制 file_paths；
  sku_match() MCP 工具仍返回 deferred；capability_status() 仍把 match 报为 deferred；
  config.json UI 页标题仍是「SKU对账（暂缓）」。
- **当前 match 真执行通道 = 直接调 core.pipeline.run_match**（scripts/run_match_oct_all.py
  批量入口，参数取自账单整理批次 manifest）。
- 补齐 Q8 需：run.py/actions 按 stage 分发、sku_match 工具接 run_match（输入=bill_batch_id）、
  capability_status/UI/config.json 状态同步——另起小任务实施，勿在文档层假装已接。