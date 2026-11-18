---
name: recon_assist
description: "对账工作台插件说明书（2026-11-18 五能力全启用+UI精简+总体review）：所有操作由 AI 通过 MCP 工具执行（28 工具），人只提供数据源路径；管理页四页只读看板（总览/数据源与规则/作业与日志/设置）。数据库只存店铺映射，结果全落文件（交付 Excel 在批次根目录，底稿在 底稿/）；账单整理/SKU对账/成本匹配（成功迁移 已匹配成本/）/数据库回传与氚云对接（缺省 dry-run 预检，真实写外部系统须显式 dry_run=0+用户确认）五能力全可用；另有透视汇总（bill/sku_pivot_summary）、环境自检（env_status）、分发安全自检（dist_check）；清空历史只清插件内记录，输出目录文件一律保留。含 AI 工作流、数据落位、依赖环境与财务口径纪律。"
---

# 对账工作台 · 插件使用说明书

**插件**：`recon_workbench`（工具组 `ext_recon_workbench`）· 本地单机版 · v0.2.0
**承接**：原影刀 RPA 应用「资金账单匹配sku对账」（9013 行），已去 xbot 依赖、模块化分层
**激活**：用户在会话发 `/ext:recon_workbench`，或在管理页点选元素后发送 `el` chip

## 🖥️ 运行环境（分发用户/AI 首次使用先读）

插件跑在 **Desktop Agent 的 sidecar Python 运行时**（不是系统 Python）。
完整依赖文档 = 插件根 `docs/dependencies.md` + `requirements.txt`；
本技能目录 `environment_checks.yaml` 已声明 8 包检测清单（宿主 env_check/启动体检自动读取）。

**环境自检入口**：`recon_workbench__env_status()`（只读，不连库不发HTTP不安装）→
逐包 installed/version + 六能力就绪矩阵（ready/degraded/not_ready）+
`install_guidance`（venv_install 安装清单）+ ADB/氚云凭据就绪状态。

**缺依赖处理流程（AI 执行）**：
1. 调 `env_status` 看 `missing.required / conditional / optional`；
2. 调宿主 `venv_install(packages=[...], reason=…)` 安装（会请求用户确认；
   **禁止 Bash pip install**，系统 Python 装了也没用）；
3. 装完复检 `env_status` 确认 ready。

依赖分层（SSOT=`core/envcheck.py DEPENDENCIES`，三处清单有一致性断言）：
- **核心必需**：pandas / numpy / openpyxl —— 缺任一插件整体不可用；
- **条件依赖**：mysql-connector-python（SKU对账取数/回传真实落库）、requests（氚云真实推送）
  —— 缺失只降级对应功能真实路径，dry-run 预检与本地功能不受影响；
- **可选加速**：xlsxwriter（写引擎，回落 openpyxl）、pyarrow（parquet 底稿，回落 CSV）、
  python-calamine（大簿秒级读，回落 openpyxl 流式）—— 不装不影响功能正确性。

凭据与依赖分开配：RECON_ADB_*（巨益 ADB）/ RECON_H3_ENGINE_CODE、SECRET（氚云），
系统环境变量配置后重启生效；`env_status.credentials` 与 `db_status` 报就绪状态（不暴露值）。

**分发安全（分享给其他用户前必做）**：`recon_workbench__dist_check()` —— 只读扫描
插件目录工作区 + git 全历史（本机真实凭据值是否入文件 / 硬编码口令 / PEM 私钥 /
运行时文件混入），返回 verdict=safe/caution/blocked + 打码 findings + 打包排除清单。
凭据只存本机环境变量，插件目录零凭据文件；运行时数据（settings/plugin.db/results/
downloads/logs/氚云主数据快照）在 data/extensions/recon_workbench/（插件目录外），
正常打包不会带出。blocked 必须先清理再分享；caution 按 packaging_excludes 剔除
（建议连 .git/ 一起剔除）。完整纪律 = `docs/SECURITY.md`。

## 📚 平台技能文档（格式漂移 / 新费用类型应对，按需深读）

位于本技能 `references/` 目录（用 Read 读插件目录下
`skills/recon_assist/references/<文件名>`）。**遇到解析报错、未匹配/其他类金额异常、
金额对平门不平、或每月新账期例行检查时，先读对应文档再动手**：

| 文档 | 内容 | 何时读 |
|---|---|---|
| `bill-methodology.md` | 总体方法论：统一流水线、分类三层结构、规则表热修通道（含 biz_code **整表覆盖**警告）、格式漂移分诊 SOP、变更后验收、每月例行检查单 | 任何漂移/异常先读这份；每月新账期开跑前过一遍检查单 |
| `sku-match-methodology.md` | **SKU对账（账单追账）业务 SSOT**：收款/退款两段对账、秒退交叉核对、OMS 向前取 6 个月取数规律、额外入库冲成本/冲发出判断、五类财务判断（结转成本/冲成本/发出商品挂账/冲发出商品/成本不做处理）、对账状态月度闭环（已对账订单下月收款不做成本处理）、分摊四级策略与对平门 | **SKU对账实施前必读**；处理意见/秒退/回传状态任何疑问先读这份 |
| `platform-jd.md` | 京东：三源目录约定、**备注资金日期推断账期**（非用户账期）、钱包四级分类链、E卡硬门 + 商家结算款替换软门、匹配层≠账单层 | 京东报错/没替换/提现消失/账期疑问 |
| `platform-taobao.md` | 淘系（淘宝+天猫共用）：支付宝/微信**两条独立分类链**（顺序红线）、支付宝十步链、收支合计硬门、微信 xlsx 附加源 | 淘系镜像差异/新类别/微信簿缺失 |
| `platform-douyin.md` | 抖音：组件列展开（非打标签）、SALES/PLATFORM_FEE 公式、**账扣费用兜底桶**（新费用悄悄进这里不报错）、金额差异自检 | 抖音账扣费用异常增大/货款口径跳变 |
| `platform-pdd.md` | 拼多多：业务编码字典 + 未匹配显式暴露、**新编码 biz_code 热修标准流程**、#支出合计尾行、动态单号正则 | 拼多多未匹配/新费用编码（热修最友好） |
| `platform-kuaishou.md` | 快手：双源（资金账单+订单流水）、货款净额**硬门**、对平后货款改用订单流水 10 组件展开 | 快手硬门不平/缺订单流水/组件列漂移 |
| `platform-sph.md` | 视频号：动帐类型单层字典、全角竖线「订单交易｜先用后付」易漂移点 | 视频号未匹配/缺列 |
| `platform-xhs.md` | 小红书：**结算单口径**（非日账单）、双 sheet、空账期导全零簿（export_when_empty）、金额差异自检 | 小红书金额差异非0/新业务款项/全零簿疑问 |
| `platform-youzan.md` | 有赞：utf-8 编码（特例）、类型字典+充值二次分类、\t 尾巴 | 有赞乱码/未匹配/保证金归类 |
| `platform-hyk.md` | 好衣库：全列反引号剥离（StringDtype 坑）、16 费用组件、现金流**硬门**、HYK001/002 编码注意 | 好衣库0行/硬门不平/新费用列 |
| `platform-akc.md` | 爱库存：单文件**多店** 13 sheet 白名单、款号=sku[:-5] 推导、文件名不含店铺 | 爱库存未知sheet/店铺归属/款号异常 |
| `platform-vip.md` | 唯品会：**按旬出账不做日期过滤**、7 文件族、品牌款号前缀规则、舍入顺序（先round再除税）、大文件后台跑 | 唯品会族失配/未知店铺/不含税漂移 |

核心纪律（各文档反复强调，此处摘要）：
1. **字典可热修，链序不可乱动**——备注关键字链顺序即口径，改序必跑基准回归；
2. **钱不能丢**——对平门不平先定位差额行给人类，禁止调容差放行；
3. **新类别必须引导人类定类**（归既有类/新建类/挂账待定），不许悄悄归「其他」；
4. **biz_code 规则表是整表覆盖**——首次热修先全量导入内置字典再补新编码；
5. 变更后验收 = 8 月基准回归（全部数值列，防假绿）+ 新账单 preview_bill + 人类确认。

## ⚠️ 2026-11-14 方案改版（先读这段，口径与旧版不同）

用户决策两条：
1. **数据库不存 SKU 对账明细结果，也不存账单整理结果，只存店铺映射。**
2. **插件只做影刀现有能力**：账单整理（可用）+ SKU对账（**2026-11-17 启用**，不带成本）+ 成本匹配（**2026-11-18 启用**，SKU 对账下游）+ 数据库回传（**2026-11-18 启用，缺省 dry-run 预检不写库**）+ 氚云对接（**2026-11-18 启用，缺省 dry-run 预检零 HTTP 不推送**）。五能力全可用。

因此本地 SQLite 从 11 张表收窄到 **1 张表**：

| 数据 | 旧位置 | 新位置 |
|---|---|---|
| 店铺映射 | `ext_recon_workbench_shop_map` | ✅ **仍在数据库**（唯一保留表） |
| 平台解析规则 | `platform_rules` 表 | `{plugin}/rules/{platform}.json`（可进版本管理、可 diff） |
| 作业留痕 | `job_log` 表 | `data/extensions/recon_workbench/logs/jobs.jsonl` |
| 账单整理结果 | `bill_rows` / `bill_goods` 表 | 结果根目录（默认 `data/extensions/recon_workbench/results/`，可由 `save_settings` 自定义）下 `{账期}_{平台}_账单整理_{店铺}_{时间戳}/`<br>根目录只放**交付 Excel**；`底稿/` 子目录放 `manifest.json`、`*_统一明细.xlsx`、E卡底稿、`bill_detail.parquet\|.csv`、`bill_goods.parquet\|.csv` |
| SKU对账结果 | `oms_rows`/`match_detail`/`pivot_*`/`writeback_log` | **文件态**（2026-11-17 启用）：`{results}/{YYYY-MM}/sku匹配/未匹配成本/{批次名}/` 交付 Excel（6 sheet，明细 31 列）+ `底稿/`（match_detail/pivot_*/oms_matched csv，含内部字段）；不写任何库表 |
| 成本匹配结果 | `cost_base`/`writeback_log` | **文件态**（2026-11-18 启用）：同目录同名交付 Excel（6 sheet，透视 13/18/14 列无店铺编码，明细 32 列=31+特殊情况说明）+ `底稿/` cost_detail/cost_pivot_* csv + manifest（cost_file/sheet/缺失款号清单）；成功后整批迁移 `sku匹配/已匹配成本/`（VIP 全旬成功才迁移；已匹配成本内重跑原地覆盖）；不写任何库表 |
| 回传结果 | `writeback_log` | **文件态**（2026-11-18 启用）：作业留痕进 `logs/jobs.jsonl`；Job data 带 unit_reports/failed_detail/sql_preview；真实回传成功后整批迁移 `sku匹配/已回传数据库/`（dry-run 不迁移）；不写任何插件库表 |

`storage.ALLOWED_TABLES` 是**运行时硬闸门**：任何想往退役表写的代码会抛
`ValueError: 表名不在白名单 …（该表已在 2026-11-14 方案改版中退役）`。
五能力现全部走文件态，不需要加表；若未来有人想恢复库表落盘，必须先在这里显式加表名
—— 那等于一次有意的方案评审，不能悄悄绕过。

**已退役的 MCP 工具（调用会报「工具不存在」，不要再试图调）**：
`query_match_detail` `unmatched_summary` `query_pivot` `cost_summary` `query_writeback` `load_cost_file`
以及 `start_recon` 的 `stage=full` 与 `cost_mode/use_db/writeback` 等参数。
（2026-11-18 起 `stage=match|cost|writeback|chuanyun` 与 `bill_batch_id/cost_file/include_tax/lookback_months/dry_run` 参数**已恢复可用**。）

**总览不做跨批次「匹配率 / 未匹配差异」聚合**：重跑批次叠加会让指标失真（恒 0 或虚高
都会被误读）。总览给的是账单批次、账单行、货款行、店铺映射、规则条数；
SKU/成本结果按批次下钻（`sku_batches` + 底稿）。

## 五个能力的开放状态

| 能力 | 状态 | 工具 / 页面 | 说明 |
|---|---|---|---|
| 账单整理 | ✅ 可用 | `start_recon(stage=bill)` · `preview_bill` · `bill_batches` · `query_bill_rows` | 各平台账单 → 统一五元口径 → Excel + parquet |
| SKU 对账 | ✅ 可用 | `sku_match()` / `start_recon(stage=match)` · `sku_batches` | 2026-11-17 启用、2026-11-18 入口接线补齐（Q8）；bill_batch_id 或 period+platform(+shop_code) 定位账单批次；批量入口 `scripts/run_match_oct_all.py` |
| 成本匹配 | ✅ 可用 | `cost_match()` / `start_recon(stage=cost)` · `sku_batches` | 2026-11-18 启用；period 必填，cost_file 缺省查 结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx（{月}月末 sheet）；bill_batch_id 缺省=该账期全部批次（含已匹配成本重跑）；业务 SSOT=`references/cost-match-methodology.md` |
| 数据库回传 | ✅ 可用（缺省预检） | `writeback()` / `start_recon(stage=writeback)` | 2026-11-18 启用，承接影刀 `look_sku.update_sql`：按「已匹配的巨益单号」（oms_matched 底稿）的 OMS 单据 id 执行 `UPDATE mid_jedz_datas SET bill_state='已对账', bill_period=中文账期 WHERE id=%s`。**dry_run 缺省 1=预检**（只统计+SQL预览，不连库不写库不迁移）；真实落库须显式 `dry_run=0` **且先经用户确认**（当前版本纪律：只做代码功能，不真实更新数据库）。成功后整批迁移 `已回传数据库/`，失败批次留守；executemany 1000/批+commit/rollback，凭据走环境变量 |
| 氚云对接 | ✅ 可用（缺省预检） | `chuanyun_sync()` / `start_recon(stage=chuanyun)` | 2026-11-18 启用，承接影刀 `api_chuanyun.file_arrange`：账单整理交付 Excel 的「氚云导入模板」sheet 经五张氚云主数据表（账单科目/管理科目/店铺/资金账户/巨益编号）映射组装「店铺资金账单」主表+子表（26 字段）→ OpenAPI CreateBizObject 推送（IsSubmit=true）。**dry_run 缺省 1=预检**：零 HTTP 调用（主数据只读查询也不发），payload 预览落 `底稿/chuanyun_preview/`；无主数据快照时 ObjectId 留空+warnings 明示。真实推送须显式 `dry_run=0` **且先经用户确认**（凭据走 RECON_H3_ENGINE_CODE/SECRET 环境变量；首次真实取数自动留快照 runtime/chuanyun_master.json）；成功文件移入批次内 `氚云导入成功/` 并更新 manifest，失败留守。已修复原脚本余额时序 bug（先匹配账户再查余额）。注意真实推送会提交流程并触发预算占用，录入查重=YYYY-MM+账户obj 防重 |

问「现在能做什么」→ 先调 `capability_status`，它给出五个能力的开放状态与 dry-run 边界。

### 独立透视汇总工具（2026-11-18 新增，同步、纯本地聚合）

五能力流水线之外的额外服务，承接影刀 `cost_match.final_bill_pivot / final_pivot`；
不连数据库、不推氚云、不修改任何批次产物，调用即返回汇总簿路径 + 下载链接 + 合计：

| 工具 | 参数 | 输出 |
|---|---|---|
| `bill_pivot_summary` 店铺账单整理透视汇总 | period 必填；platform/shop_code/bill_batch_id 可选 | 扫账单整理批次交付 Excel 的 8 列透视 sheet（按列结构识别，各平台 sheet 名不同），concat → `results/{YYYY-MM}/汇总/账单整理透视汇总表_{时间戳}.xlsx`（单 sheet 8 列，元），返回收/退款/现金流合计与店铺数 |
| `sku_pivot_summary` 店铺sku匹配透视汇总 | period 必填；state 缺省三态全扫（未匹配成本/已匹配成本/已回传数据库，同批次不重复），可指定单态；platform/shop_code/bill_batch_id 可选 | 扫 SKU 批次交付 Excel 5 sheet：店铺/款号/匹配三张透视纯 concat + 收退款明细按 店铺名称×订单年月 汇总货款分摊金额 outer merge → `sku匹配数据透视汇总表_{时间戳}.xlsx` 4 sheet（店铺数据透视汇总/款号数据透视汇总/数据透视明细汇总/按订单年月透视）；VIP 多旬逐份计入 |

注意：汇总输出在 `汇总/` 目录（时间戳不覆盖、重跑不自吞）；skipped/errors 清单如实报
（无透视 sheet 的文件跳过不报错）；大簿 calamine 引擎秒级读。
业务 SSOT=`docs/specs/2026-11-18-pivot-summaries.md`。

唯一占位入口 = `start_recon(stage=full)`（一键全链路），返回
`ok=True, status="deferred"`（**不是失败，也不是成功**）。原因：回传/氚云属真实写
外部系统动作，须显式 dry_run=0 且经用户确认，不能随全链路静默执行——请逐阶段触发。
不要把 deferred 当成执行成功去汇报结果。

历史封存：`core/pipeline_dormant.py` = 改版前库表版编排（仅历史口径参考，
**不在任何运行路径上**，依赖退役表名被白名单拦下）。五能力的现行实现全在
`core/pipeline.py`（run_bill/run_match/run_cost/run_writeback/run_chuanyun，文件态）。

## 凭据（重要）

环境变量 `RECON_ADB_HOST/USER/PASSWORD`（可选 `PORT`、`DATABASE`，默认库 `xianxia`）。
已写入本机**用户级**环境变量。`core.config.env()` 先读进程环境，缺失时回落读 `HKCU\Environment` 注册表 ——
因为进程环境变量是启动时快照，新写入的变量在已运行的 sidecar 里读不到，靠注册表兜底就不必重启桌面智能体。

**账单整理完全不需要数据库**；ADB 仅 SKU对账取数与回传真实落库需要（回传缺省 dry-run 不连库）。
`db_status` 未连通时按「引导态」处理（`ok=True` + `data.ok=false`），不要当成插件故障。
**禁止**把口令写进 config.json、代码、Chat 回复或 Job result；`AdbConfig.masked()` 已脱敏。

## ⚠️ 2026-11-16 AI 原生化（人只提供数据源）

用户决策：**移除全部人工操作入口**。管理页不再有整理表单/试跑按钮/店铺表导入/规则编辑/
设置保存/清空历史按钮——这些能力全部由 AI 通过 MCP 工具执行，人只需在会话里给出
数据源路径与意图。页面保留：只读看板、刷新、Excel 下载、「问 AI」「在会话中引用」。

**AI 独占的操作工具**（页面上没有对应按钮，不要引导用户去页面操作）：

| 用户说什么 | AI 调什么 |
|---|---|
| 「整理 X 平台 X 店 X 月账单，路径 …」 | `start_recon(stage=bill, …)` → `job_status` 轮询终态 |
| 「先试试能不能解析」 | `preview_bill(platform, file_paths)`（不落盘） |
| 「导入店铺表，路径 …」 | `load_shop_file(file_path)` |
| 「给 X 平台补/改/停用规则」 | `upsert_rule` / `delete_rule` |
| 「结果存到 D:\…」 / 「恢复默认目录」 | `save_settings(results_dir=…/空串)` |
| 「清空对账历史」 | **先向用户复述删除范围并确认**，再 `clear_history(confirm="yes")` |

**清空历史确认纪律（2026-11-18 改版）**：`clear_history` **只清插件内信息**——
作业留痕 logs/jobs.jsonl（支持 period/platform/shop_code 条件过滤）+ downloads 下载副本
（插件运行时缓存，仅全清时删）。**输出目录（results/ 或自定义 results_dir）的批次目录、
交付 Excel、底稿、汇总簿一律保留不删**；店铺映射/规则/设置同样保留。
清记录后批次清单仍可见（扫输出目录的文件态），属预期行为，不要向用户报「批次已删除」。
AI 必须先说明范围并得到用户明确同意才传 confirm=yes；
用户消息本身已明确说「清空/删除历史」视为已确认，可直接执行。
若用户要删输出目录文件，那是文件管理操作（Delete/资源管理器），与本工具无关，须另行确认。

## 页面（2026-11-18 UI 精简：只剩四页，全部只读看板+设置）

五个能力页（账单整理/SKU对账/成本匹配/数据库回传/氚云对接）已按用户需求移除——
全部操作由 AI 在会话中通过 MCP 工具执行，作业进度在「作业与日志」页看。

| 插件页 | 内容 |
|---|---|
| 对账总览 | 账单批次/行数/货款行、店铺映射与规则条数、能力开放状态、账期与平台覆盖、最近批次、「清空历史」人工按钮（两步确认，只清插件内记录） |
| 数据源与规则 | 数据库凭据状态、店铺映射清单、平台解析规则清单（只读；维护走 AI 工具） |
| 作业与日志 | Job 进度（AI 触发的作业在此轮询展示）、作业留痕（JSONL） |
| 设置 | 结果存储路径（可编辑，`save_settings`）、清空历史说明 |

## 标准动作（按顺序，不要跳步）

1. **先看能力边界**：`capability_status` → 用户要的环节是否可用；`data_status` → 店铺映射有没有、
   账单批次几个、规则文件哪些；`db_status` → 仅当用户要做 SKU对账/回传时才需要关心
2. **改规则或接新平台先试跑**：`preview_bill(platform, file_paths)` → 看原始列 vs 归一列、样例行、
   已加载规则条数。**不落盘、不入库**，安全
3. **整理账单**：`start_recon(stage=bill, platform, shop_code, period 或 s_date+e_date, file_paths, extra_files?)`
   → `file_paths` 必填，缺失会直接被拒（不会起一个必失败的 Job 去污染作业日志）。
   店铺名称不用传：自动查店铺映射取氚云名称；映射缺失时结果会带 note，提示用户给 OMS店铺.xlsx 路径
4. **看结果**：`job_status` 拿终态 → `bill_batches(period/shop_code)` 看批次与货款合计、
   交付 Excel 路径与 downloads 下载链接 → `query_bill_rows(batch_id)` 逐行下钻。
   向用户汇报时给**交付件**清单与货款合计；底稿（统一明细/E卡/manifest/表格）在批次内 `底稿/`，
   不必逐个罗列
5. **SKU对账**：`sku_match(platform, shop_code, period)` 或 `start_recon(stage=match, bill_batch_id=…)`
   → `job_status` 终态 → `sku_batches(period)` 看批次与产物。B2B（vip/akc）不查 OMS。
6. **成本匹配**（SKU 对账下游，财务给成本表后）：`cost_match(period, cost_file?, bill_batch_id?)`
   → `job_status` 终态。cost_file 缺省查 结果根/{YYYY-MM}/物料成本_YYYYMM.xlsx；
   财务表在别处（如 桌面\2026.10月账单\）时显式传路径。成功后批次自动迁移 已匹配成本/；
   汇报时给 缺失款号清单（data.missing_styles）与留守批次（data.failed_batches）。
   业务口径疑问先读 `references/cost-match-methodology.md`。
7. **查差异**：账单层（业务类别、收款/退款合计）+ SKU 层底稿（match_detail csv）
8. **数据库回传 / 氚云推送**（成本匹配下游，可选）：`writeback(period)` / `chuanyun_sync(period)`
   缺省 dry-run 预检（不连库/零 HTTP），先给用户看预检统计与 SQL/payload 预览；
   **真实执行须用户明确确认后显式传 `dry_run=0`**，成功后批次自动迁移
   （已回传数据库/ · 氚云导入成功/），失败留守
9. **月度汇总交付**：`bill_pivot_summary(period)` / `sku_pivot_summary(period)`
   → 汇总簿下载链接 + 合计（纯本地聚合，随时可重跑，时间戳不覆盖）

## 批次收敛纪律（查询必读）

每次整理**新建一个带时间戳的批次目录**，永不覆盖历史。所以：

- `query_bill_rows` 未指定 `batch_id` 时，只返回该范围内**最新一次**批次；
- 要跨批次比对必须显式传 `batch_id`；
- 不要自己扫目录后把多个批次的行数相加当作「本期账单行」—— 那会把重跑结果叠在一起。

这是旧库版本踩过的真实坑：DY014 重跑十余次后管理页成本卡片显示 131,014.52 元、
单次真值 1,800.26 元；`inspect` / `validate` 查不出（不是代码错误，是查询口径问题），
回归脚本也因手写 batch_id 过滤而掩盖了它 —— **只有真实 UI 能暴露**。

## 账单口径要点（改版后仍适用）

- 统一五元：`订单编号 / 资金日期 / 业务类别 / 收款 / 退款`（无订单号时填 `无订单号`）
- 金额全程「分」的 int（表格里是 `*_fen` int64），只在导出 Excel 时转元；
  避免 float 累加分位漂移
- **表格产物格式由环境决定，不要假设扩展名**：有 pyarrow/fastparquet 就写 `.parquet`，
  都没有就写 `.csv`（utf-8-sig 带 BOM，Excel 双击不乱码），两种格式列序与金额口径一致；
  Excel 写引擎 xlsxwriter→openpyxl 自动回落。当前装了哪些包以 `env_status` 为准，
  不要凭历史快照假设。读结果一律用 `artifacts.read_detail() / read_goods()` 或
  `BillBatch.detail_path`，**不要自己拼 `.parquet` 后缀**（改版验收就是在这里翻的车）
- `bill_detail.parquet`（账单层，资金流水全景）与 `bill_goods.parquet`（匹配层输入货款行）
  **刻意分开**：多源平台两者不等价。京东 JD001 实测 —— 账单层是钱包流水(1097行/24类)+结算单(16604行)
  合并、商家结算款已被结算单替换；而 SKU 对账输入是另外三源子集：结算单货款 2295 行 +
  钱包「货款退款」315 行（改判货款、订单号从备注正则提取）+ E卡 82 行（E卡不进账单层，
  只与钱包 E卡货款交叉校验）。按账单层筛「货款」会错取钱包货款、漏掉 E卡与货款退款，
  货款金额与影刀基准 276,668.76 / 185,311.32 元对不上
- **匹配窗口** `match_s_date/match_e_date` 与账单账期也不等价（京东用钱包备注资金日期 min~max，
  07-31~08-30 而非 08-01~08-31），差一天就让补录行在收款/退款侧翻转；随 `bill_goods.parquet` 落盘，
  由 `artifacts.match_window()` 读取
- 业务账期中文标签由 `core.config.period_label()` 生成（`2026年06月-2026年06月`），
  写进 manifest 的 `totals.bill_period_label`；回传启用时必须用同格式，
  写 `YYYY-MM` 会让同一账期在生产库裂成两种写法
- 平台适配：11 平台原生适配器（taobao/douyin/jd/pdd/kuaishou/sph/xhs/youzan/hyk/akc/vip）+
  `GenericAdapter`（纯规则文件驱动，结构与已支持平台相近的新平台可不写代码）；
  清单以 `platforms()` 工具为准
- 编码坑：拼多多/支付宝账单为 `gb18030`；表头行数不匹配时用 `read_option` 规则调

## 巨益 ADB 真实表结构（2026-11-13 连生产库 xianxia 实测；SKU对账启用时必读）

**不要凭猜写列名**。原影刀脚本用 `SELECT *` + `pd.DataFrame(results, columns=中文col)` 按位置重命名，
真实列名全是英文；插件已改为显式列名 SELECT + rename（`core/db.py` 的三张映射表）。

| 表 | 列数 | 真实列（顺序即原脚本位置映射） |
|---|---|---|
| `mid_jedz_datas` | 22 | id, bill_type, bill_date, bill_code, sku, qty, shop_code, shop_name, detail_id, order_code, delivery_status, refund_status, create_time, bill_amount, bill_time, bill_state, settlement_amount, category_name, order_time, refund_type, mall_type, bill_period |
| `AI_product_info` | 9 | sku, product_code(=款号), sku_name, category_name, year, season, brand_name, marked_price(=吊牌价), standard_cost(=标准成本单价) |
| `je_saleorders` | 5 | shop_name, shop_code, trade_no(=订单编号), order_date, sales_order_id |

关键事实：
- `bill_type` 只有三种取值：`B2C配货单`（出库，2277 万行）/ `退款单`（1386 万行）/ `B2C退换货通知单`（入库，564 万行）
- `settlement_amount` 是 **Decimal 元**（如 `132.0000`），不是分；`bill_amount`/`mall_type` 常为 NULL
- `bill_state` 取值 `未对账` / `已对账`；未对账约 3325 万行
- `bill_period` 存中文标签，格式 `2026年06月-2026年06月`（历史遗留还有 `2025年05-05月`）
- 单店半年窗口数据量很大：PDD002 六个月约 **41.6 万行** OMS，因此 OMS 只在内存匹配、不整表落地
- 真实店铺编码样本：`TM001` `TM003` `TM005` `TM008`（淘系天猫）、`DY007` `DY009` `DY011` `DY012` `DY016` `DY018`（抖音）、
  `PDD002` `PDD003`（拼多多）、`JD001`（京东）、`VIP00101` `VIP00201`（唯品会）、`AKC003`（爱库存）、`TBQY001`（淘宝企业店）
- 订单编号形如 `261031-113560860974058`（拼多多带日期前缀），sku 形如 `625408A10006907303`，款号形如 `625425M418482`
- **`je_saleorders.trade_no` 不唯一**：200 个订单号抽样中有 4 个对应多行。原脚本 `dict(zip(...))` 是「后者覆盖」
  且 `SELECT` 无 `ORDER BY` → **原脚本的订单日期本身不可复现**。插件改为显式
  `ORDER BY trade_no, order_date ASC` 后取**最新** order_date，确定性可复现。
  这是一处**有意的口径改进**，不是等价迁移；与影刀结果比对时若发现订单日期不同属预期差异，
  差异订单号可从 `core.db.LAST_ORDER_DATE_DUPLICATES` 取得。

## 财务口径纪律（五能力全启用；回传/氚云真实执行须用户确认）

- **五类财务判断**（详见 `references/sku-match-methodology.md`）：`结转成本`（收款对上发货且历史未对账）/
  `冲成本`（退货对应发货已结转过成本或找不到出库）/ `冲发出商品`（退货对应发货从未确认收入）/
  `成本不做处理`（秒退、分摊≤0、已对账重复收款等）；「发出商品」是出库未参与收款核对时的挂账状态，不是分录标签
- **秒退判断**：未匹配收款订单的货款金额 == 该订单退款侧货款合计（round 2 相等）→ `是否秒退判断=是`，
  不结转成本；秒退收款单独再撞退款单，退款侧按单号/订单号双路回标（不是「同日数量一致」）
- **对账状态闭环**：每月回传 `bill_state=已对账 + bill_period=中文账期`；下月窗口（向前取 6 个月）
  仍会取到旧单据，靠 历史对账记录 + C 列账期比较拦截 → 已对账订单的新收款落「成本不做处理」，不重复结转
- **分摊自检**：货款分摊后合计必须与账单货款合计**分位相等**（差异恒为 0 分），
  不等就是硬差异，必须暴露，不许四舍五入抹平
- 毛利率有两种口径（账单结算 / 订单结算），内部以基点 bp 存（1bp=0.01%），
  展示时 `/100` 转百分数。**回归脚本比对列集合必须包含毛利率列** ——
  曾经漏比导致「bp 列被裁掉、入库恒 NULL」这种结构性缺陷三层回归全绿都报不出来（假绿）
