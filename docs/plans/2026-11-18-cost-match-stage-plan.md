# 实施 Plan — 实际成本匹配启用（stage=cost · SKU 对账下游）

| Field | Value |
|-------|-------|
| date | 2026-11-18 |
| spec | [../specs/2026-11-18-cost-match-stage.md](../specs/2026-11-18-cost-match-stage.md)（reviewed，Q1–Q6 全确认 + A1–A8 审核修订） |
| 业务 SSOT | skills/recon_assist/references/cost-match-methodology.md（含 §5b 用户确认决策） |
| 状态 | 已实施（2026-11-18 T1–T6 完成；实测修订见 spec「实施修订」R1–R5 与 bugfixlog cost-match-stage-fixes） |

## 0. 现状盘点（探查结论，决定 task 粒度）

| 资产 | 现状 | 对 task 的影响 |
|---|---|---|
| core/cost.py | load_cost_base（mode=style 默认、cost_sheet_for_period 按账期推 sheet、unit_cost_raw 原始精度）、apply_cost、_round_ship（元域 Decimal HALF_UP）、enrich_from_db 已存在 | T2 改造而非新建；**两处带病**：①apply_cost 重算标准成本（A1，会覆盖 FR 特例）②采购单价展示列存分（A2）；空值语义是 match 阶段口径（NaN 区分账单/补录），cost 阶段须独立分支 |
| core/pivots.py | _add_margin(stage="cost", include_tax) 双口径已实现（/1.13、bp 存率）；_assemble_pivot 支持 stage | T2 复用；**列集**：SHOP/PRODUCT/MATCH_PIVOT_COLUMNS 均含店铺编码（match 阶段口径），cost 阶段 13/18/14 无店铺编码须按阶段裁列（A3/Q5） |
| core/exporters.py | export_match_result 6-sheet；DETAIL_SHEET_COLUMNS 31 列（2026-11-18 裁列）；MATCH_SHEET_COLUMNS 15 列 | T2 新增 cost 阶段列集常量（明细 32=31+特殊情况说明；透视 13/18/14）并接线——**常量定义了必须接线**（11-14/11-17 两次同类缺陷教训） |
| core/artifacts.py | sku_root/build_match_batch_dir 三态目录族已就绪（未匹配成本/已匹配成本/已回传数据库）；MATCH_DETAIL_STEM 底稿 csv；**无迁移函数** | T3 新增 move 函数（临时名+校验后 shutil.move）；VIP 多旬目录不清（既有特例沿用） |
| core/pipeline.py | run_match 已启用（文件态）；run_cost 是占位 deferred；ACTIVE_STAGES=(bill,match) | T2 新增 run_cost（读 match 底稿→回填→透视→导出→迁移）；不搬 dormant 整段（其依赖退役库表） |
| core/pipeline_dormant.py | run_match 旧版含 cost_file/cost_mode/include_tax/force_std_as_ship 参数通道与成本段参考实现 | 只作口径参考；force_std_as_ship 是整店开关（A6 审核：须改行级 唯品会&FR） |
| core/b2b.py | VIP FR 标准成本=结算金额已在 SKU 阶段实现（行级）；AKC/VIP 明细发货成本恒 0（阶段口径正确） | T1 补跑直接复用；T2 透传即可（A1 禁重算） |
| run.py / actions.py | **match 接线遗留**：stage=match 过 ACTIVE_STAGES 门后仍调 run_bill；sku_match()/cost_match() 均 _deferred；capability_status 把 match 报 deferred | T4 一并补齐（spec C5）：按 stage 分发 bill/match/cost |
| config.json / UI | match/cost 页均「暂缓」占位卡；sku_match/cost_match 工具声明为占位描述 | T4 同步 |
| 上游数据 | 10 月 38 店 match 产物在 未匹配成本/（三项校验 38/38 OK）；**VIP 未跑、AKC 已跑**；成本表 物料成本_202610.xlsx 在 桌面\2026.10月账单\ | T1 补跑 VIP 多旬；成本表位置与固定约定（结果根/{YYYY-MM}/）不同，T2 缺省查找须兼容或先归位 |
| 基准 | 影刀成本匹配基准 52 份（SKU匹配汇总\成本匹配\）；HYK/VIP/AKC/DY009 关键锚点已实测取证（spec 审核记录 A1–A8） | T5 回归对照就绪 |
| 回归脚本 | scripts/run_match_oct_all.py / verify_match_totals.py / diagnose_match_diff.py 已入库 | T1 复用批量入口；T5 新增 verify_cost_match.py（含守恒校验） |

## 1. Task 分解与实施路径

### T1 补跑上游（VIP 多旬 + AKC 复核）
- run_match B2B 分支补跑唯品会 10 月各旬（含 9 月下旬跨期旬，按账单整理批次 manifest 分发）；
  AKC 已有产物复核即可，不重跑（最小必要重跑集教训）；
- VIP 大文件后台跑落日志；产物落 未匹配成本/（VIP 目录多旬累积，不清目录）；
- 与影刀 SKU 阶段基准比对（白名单：品牌字典漂移 QS→FR 待确认项沿用既有登记）。
- **验收**：VIP 各旬 6-sheet 落盘、分摊差 0 分（B2B 无分摊则明细行数/金额与基准一致）、AKC 复核 OK。

### T2 run_cost 核心（pipeline.py + cost.py + pivots.py + exporters.py）
1. **run_cost 新写**（文件态，不搬 dormant 整段）：
   批次发现（未匹配成本/ + 已匹配成本/ 重跑场景）→ 读底稿 match_detail.csv（分域全列，C2.0）
   → load_cost_base（账期推 sheet；缺失/sheet 不存在 → failed 可读报错）
   → cost 阶段回填 → 重新透视 → 导出 → 校验 → T3 迁移；
2. **cost.py 阶段分支**（A1/A2 关键修复）：
   · 标准成本/标准成本单价**一律透传禁重算**（保 FR 结算金额特例）；
   · 采购单价展示列带 unit_cost_raw 原始精度（出口原样写出，内部计算仍分域）；
   · cost 阶段空值语义：采购单价全 fillna(0)、占位符重写（None→无款号/无吊牌价、
     id 空值→字面串 "None"，A5）；match 阶段 NaN 语义分支保留不动；
   · FR 行级特例：店铺名称含「唯品会」& 品牌==FR → 发货成本=标准成本（替代整店开关）；
   · 特殊情况说明列（第 32 列）：无款号/成本表无此款/空；缺失款号清单进 CostResult；
   · 发货成本=_round_ship 原始精度元域乘（HALF_UP 已论证 0 分歧，维持）；
3. **pivots.py**：cost 阶段从重写后明细**重新透视**（A3，分组键已变）；
   明细保持正数、仅透视退款侧取负（A4）；_add_margin(stage="cost") 复用；
   删行规则照 methodology §3.2；
4. **exporters.py**：新增 COST_DETAIL_SHEET_COLUMNS（32）/COST_SHOP（13）/COST_PRODUCT（18）/
   COST_MATCH（14）常量并**接线**（回归断言守着，防第三次「定义了没人用」）；
   采购单价列走 raw 精度出口（不经 _fen_to_yuan）；
5. 守恒自检（A8）：回填前后 货款/分摊/结算/数量/标准成本 逐列合计恒等，不等 → failed；
6. ACTIVE_STAGES 增 "cost"，DEFERRED_STAGES 移除 cost；dormant 文件头同步。
- **验收**：HYK 单店冒烟——采购单价=表值（1e-6）、发货成本逐行=round(|数量|×原始单价,2)、
  毛利锚点 21499.84、id "None" 47 行、守恒校验通过、6-sheet 落盘。

### T3 artifacts 迁移 + 状态机
- 新增 migrate_sku_batch(batch_dir, from_state, to_state)：先导出到临时名 → 校验 6 sheet
  完整 → shutil.move（同盘原子）→ 失败清理临时名、原目录留守；
- VIP 粒度：**全部旬成功才迁移整目录**，单旬失败留守并汇报失败旬；AKC=渠道批次粒度；
- 已匹配成本/ 内重跑：原地覆盖不迁移；joblog 留痕迁移事件；
- 扫描/清理函数兼容已匹配成本目录（clear_history 不误删）。
- **验收**：成功迁移无残留；失败注入（成本表缺 sheet/磁盘只读模拟）留守；VIP 单旬失败整目录留守。

### T4 入口接线（run.py / actions / config.json / UI，含 match 遗留一并补）
- run.py / actions.start_recon：按 stage 分发 bill→run_bill、match→run_match、cost→run_cost
  （**补上 match 的 Q8 遗留**：不再统一调 run_bill；match 参数 bill_batch_id 或 period+platform+shop_code）；
- cost_match() MCP 工具接真执行（异步 Job；参数 period/cost_file/bill_batch_id/include_tax）；
  sku_match() 同步接 run_match；
- capability_status：match/cost 改 active（match 状态表述修正）；config.json 工具声明与
  页标题去「暂缓」；UI 占位卡换真实表单（批次选择+成本表路径+触发+终态+下载）；
- 成本表缺省查找：结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx，找不到再回落用户指定路径；
  异形表转换层留接口（遇真实异形表再实现映射，转换结果落底稿留痕）。
- **验收**：MCP 触发 match/cost Job 成功；UI 表单可选批次、可看终态、可下载；
  capability_status 五能力状态与真实一致。

### T5 基准回归（防假绿）
- 新增 scripts/verify_cost_match.py：以影刀成本匹配基准 52 份为对照；
- 用例：HYK 冒烟 → 全平台批量（含 VIP 各旬、AKC）；
- 比对：逐 sheet 列头/行数/**全部数值列（含毛利率列——假绿教训）**，容差 0.011 元；
  明细逐行 采购单价=表值（原始精度 1e-6）、发货成本公式；
  **守恒校验**（A8）：货款/分摊/结算/数量/标准成本 与 match 产物逐列合计相等；
  回传表逐格一致；
- 锚点：HYK 毛利 21499.84；VIP FR 62044 行发货成本=标准成本且标准成本=结算金额；
  AKC 3727 行全查表；DY009 缺失 9 行填 0+特殊情况说明；
- 白名单：OMS 数据时点漂移（沿用 diagnose_match_diff 归因）、L147 笔误修正行、
  第 32 列附加、id "None" 复刻；白名单外差异=FAIL；
- 三项金额校验（verify_match_totals.py）对成本阶段产物复跑（货款=分摊/与账单对平不受影响）。
- **验收**：ALL_OK（白名单差异单独列示人工确认）。

### T6 收尾
- validate_extension + 热重载 + UI 探活零报错；
- codemap index.json（cost 激活、run_cost/迁移函数/新列集常量、回归脚本索引）；
- bugfixlog 登记（A1/A2 若属修复既有缺陷则登记；L147 笔误修正）；
- SKILL.md 能力状态段同步（cost 可用、match 接线补齐）；methodology 实施现状更新；
- spec status → implemented；最终 commit + push。

## 2. 依赖与风险

| 风险 | 对策 |
|---|---|
| apply_cost 改动影响已启用的 match 阶段 | 阶段分支隔离（match 语义不动）；T5 复跑 match 回归防回归 |
| FR 特例改行级后 VIP 非 FR 行取价错误 | 基准 82276 非 FR 行逐行对照；AKC 无特例对照 |
| 采购单价 raw 精度出口与 _fen_to_yuan 管线冲突 | 出口层单独带 raw 列绕过量化；回归断言守列集接线 |
| VIP 多旬迁移半截状态 | 全旬成功才 move；临时名+校验；失败注入测试 |
| 成本表位置/命名漂移（现在 桌面\2026.10月账单\） | 缺省查找+用户指定双通道；异形先转固定格式（Q3） |
| 占位符重写改变透视分组 → 与 match 产物键不一致被误判 DIFF | 回归按 cost 阶段键集合比对（A3）；守恒校验兜底金额 |
| 中途 kill 留半截批次 | 最小必要重跑集；失败目录清理后再跑；joblog 终态必写 |
| 入口接线改动面大（run.py/actions/UI） | T4 独立 commit；validate_extension+探活；match 遗留修复单独验证 |

## 3. 执行顺序

T1 → T2 → T3 → T4 → T5 → T6（串行；T2/T3 可交叉，T4 依赖 T2 的 run_cost 签名）。
文档阶段成果（spec/methodology/plan）先行 commit+push；代码按 task 分次 commit；
每 task 验收通过再进下一个，回归 FAIL 即停不硬闯。
