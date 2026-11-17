---
name: recon_assist
description: "对账工作台插件说明书（2026-11-16 AI 原生化）：所有操作由 AI 通过 MCP 工具执行，人只提供数据源路径；管理页是只读看板+Excel下载。数据库只存店铺映射；账单整理结果落文件（交付 Excel 在批次根目录，过程底稿在 底稿/）；SKU对账/成本匹配/数据库回传/氚云对接占位暂缓。含 AI 工作流、工具清单、数据落位与财务口径纪律。"
---

# 对账工作台 · 插件使用说明书

**插件**：`recon_workbench`（工具组 `ext_recon_workbench`）· 本地单机版 · v0.2.0
**承接**：原影刀 RPA 应用「资金账单匹配sku对账」（9013 行），已去 xbot 依赖、模块化分层
**激活**：用户在会话发 `/ext:recon_workbench`，或在管理页点选元素后发送 `el` chip

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
2. **插件只做影刀现有能力**：账单整理（可用）+ SKU对账 / 成本匹配 / 数据库回传 / 氚云对接（**占位暂缓**）。

因此本地 SQLite 从 11 张表收窄到 **1 张表**：

| 数据 | 旧位置 | 新位置 |
|---|---|---|
| 店铺映射 | `ext_recon_workbench_shop_map` | ✅ **仍在数据库**（唯一保留表） |
| 平台解析规则 | `platform_rules` 表 | `{plugin}/rules/{platform}.json`（可进版本管理、可 diff） |
| 作业留痕 | `job_log` 表 | `data/extensions/recon_workbench/logs/jobs.jsonl` |
| 账单整理结果 | `bill_rows` / `bill_goods` 表 | 结果根目录（默认 `data/extensions/recon_workbench/results/`，可由 `save_settings` 自定义）下 `{账期}_{平台}_账单整理_{店铺}_{时间戳}/`<br>根目录只放**交付 Excel**；`底稿/` 子目录放 `manifest.json`、`*_统一明细.xlsx`、E卡底稿、`bill_detail.parquet\|.csv`、`bill_goods.parquet\|.csv` |
| SKU对账/成本/回传结果 | `oms_rows`/`match_detail`/`pivot_*`/`writeback_log`/`cost_base` | **不落任何地方**（能力暂缓） |

`storage.ALLOWED_TABLES` 是**运行时硬闸门**：任何想往退役表写的代码会抛
`ValueError: 表名不在白名单 …（该表已在 2026-11-14 方案改版中退役）`。
日后启用 SKU 对账时，必须先在这里显式加表名 —— 那等于一次有意的方案评审，不能悄悄绕过。

**已退役的 MCP 工具（调用会报「工具不存在」，不要再试图调）**：
`query_match_detail` `unmatched_summary` `query_pivot` `cost_summary` `query_writeback` `load_cost_file`
以及 `start_recon` 的 `stage=match|writeback|full` 与 `cost_file/cost_mode/include_tax/lookback_months/use_db/writeback/dry_run` 等参数。

**总览不再给「匹配率 / 未匹配差异」**：SKU 对账暂缓且本地无明细，恒 0 的匹配指标会被误读成
「账全部对上了」，比留空更危险。总览现在给的是账单批次、账单行、货款行、店铺映射、规则条数。

## 五个能力的开放状态

| 能力 | 状态 | 工具 / 页面 | 说明 |
|---|---|---|---|
| 账单整理 | ✅ 可用 | `start_recon(stage=bill)` · `preview_bill` · `bill_batches` · `query_bill_rows` · `#/bill` | 各平台账单 → 统一五元口径 → Excel + parquet |
| SKU 对账 | ⏸ 暂缓 | `sku_match()` → `status=deferred` · `#/match` 占位卡 | 算法封存在 `core/pipeline_dormant.py` |
| 成本匹配 | ⏸ 暂缓 | `cost_match()` → `status=deferred` · `#/cost` 占位卡 | 同上 |
| 数据库回传 | ⏸ 暂缓 | `writeback()` → `status=deferred` · `#/writeback` 占位卡 | **不会**修改生产库任何对账标记 |
| 氚云对接 | ⏸ 暂缓 | `chuanyun_sync()` → `status=deferred` · `#/chuanyun` 占位卡 | 账单整理已导出「氚云导入模板」sheet，当前靠手工导入 |

问「现在能做什么」→ 先调 `capability_status`，它给出五个能力的开放状态、启用条件与休眠代码位置。

占位能力一律返回 `ok=True, status="deferred"`（**不是失败，也不是成功**），
`data` 里带 `capability / note / dormant_module`。不要把 deferred 当成执行成功去汇报结果。

## 休眠代码（启用暂缓能力时读这里）

`core/pipeline_dormant.py` = 改版前 `pipeline.py` 全文（匹配 / 分摊 / 成本 / 透视 / 回传 / 全链路），
三层回归全绿：账单 30/30、匹配 8/8、成本 5/5、分摊差异恒 0 分。**不在任何运行路径上**，
直接运行会失败（依赖已退役表名，被白名单拦下）。

重新启用四步：
1. 先决定结果落盘形态（方案约束：优先 `artifacts` 文件，不进数据库）；
2. 若坚持走库表，须在 `storage.ALLOWED_TABLES` + `schema.sql` 显式加回表名；
3. 把 `run_match / run_writeback / run_full` 从 dormant 搬回 `core/pipeline.py`，
   把 `repo.insert_rows(T_MATCH, …)` 改成 `artifacts.write_parquet(…)`；
4. `config.json` 恢复对应 `mcp_tools`，UI 把占位卡换回真实表单。

相关模块仍在仓库：`core/matching.py`（46.7KB）`core/pivots.py`（26.7KB）`core/cost.py`（16.1KB）
`core/allocation.py`（12.4KB）`core/db.py`（15.5KB）`core/writeback.py`（6.9KB）。

## 凭据（重要）

环境变量 `RECON_ADB_HOST/USER/PASSWORD`（可选 `PORT`、`DATABASE`，默认库 `xianxia`）。
已写入本机**用户级**环境变量。`core.config.env()` 先读进程环境，缺失时回落读 `HKCU\Environment` 注册表 ——
因为进程环境变量是启动时快照，新写入的变量在已运行的 sidecar 里读不到，靠注册表兜底就不必重启桌面智能体。

**账单整理完全不需要数据库**；ADB 只在 SKU对账与回传启用后才需要。
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

**清空历史确认纪律**：`clear_history` 会删所有批次目录（含底稿）、downloads 副本与作业留痕，
不可恢复。AI 必须先说明范围（保留店铺映射/规则/设置）并得到用户明确同意才传 confirm=yes；
用户消息本身已明确说「清空/删除历史」视为已确认，可直接执行。

## 页面（全部只读）

| 插件页 | 内容 |
|---|---|
| 对账总览 | 账单批次/行数/货款行、店铺映射与规则条数、能力开放状态、账期与平台覆盖、最近批次 |
| 账单整理 | AI 引导文案 + 批次清单（Excel 下载链接，只列交付件）+ 最新批次明细行 |
| SKU对账 / 成本匹配 / 数据库回传 / 氚云对接 | 占位卡：原本做什么 / 启用前需决定什么 |
| 数据源与规则 | 数据库凭据状态、店铺映射清单、平台解析规则清单（只读；维护走 AI 工具） |
| 作业与日志 | Job 进度、作业留痕（JSONL） |
| 设置 | 当前结果存储路径（只读；修改走 `save_settings`） |

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
5. **查差异**：当前只能查账单层（业务类别、收款/退款合计）；**未匹配差异汇总属暂缓能力**，
   别向用户承诺「帮你看未匹配」

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
  都没有就写 `.csv`（utf-8-sig 带 BOM，Excel 双击不乱码），两种格式列序与金额口径一致。
  2026-11-14 本机实测：运行时 Python（pandas 3.0.4）**既无 pyarrow 也无 fastparquet，
  且无 xlsxwriter（只有 openpyxl）**，所以实际落 CSV；`core/exporters._excel_engine()`
  已改为 xlsxwriter→openpyxl 回落（此前硬编码 xlsxwriter，导出必然 ImportError）。
  读结果一律用 `artifacts.read_detail() / read_goods()` 或 `BillBatch.detail_path`，
  **不要自己拼 `.parquet` 后缀**（改版验收就是在这里翻的车）
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
- 平台适配：`taobao`（淘系/天猫）`douyin` `jd` `pdd` 四个原生适配器 +
  `GenericAdapter`（纯规则文件驱动，结构与已支持平台相近的新平台可不写代码）
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

## 财务口径纪律（暂缓能力启用后适用，先记着别丢）

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
