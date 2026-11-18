# 独立透视汇总工具（bill_pivot_summary / sku_pivot_summary）— 2026-11-18

状态：**implemented**（纯本地聚合同步工具；不连数据库、不推氚云、不修改任何批次产物）

## 背景与用户需求

五能力流水线之外，用户要求增加两个**独立 MCP 工具**做额外服务：
1. 店铺账单整理透视汇总 —— 源码 `cost_match.final_bill_pivot`（L709）
2. 店铺sku匹配透视汇总 —— 源码 `cost_match.final_pivot`（L638）

两者都是「扫结果目录 xlsx → 合并透视 → 输出汇总簿」的纯文件聚合，
无外部系统写入，因此做成**同步工具**（非异步 Job），调用即返回结果与下载链接。

## 影刀原逻辑

### final_bill_pivot（账单整理透视汇总）
- glob res_dir/*.xlsx，逐文件读 **sheet_name=1**（位置假设）；
- 校验 8 列：店铺名称/店铺编码/账单周期/账单类型/业务类别/收款/退款/现金流，
  缺列直接 raise ValueError 整体中断；
- concat → 单 sheet「账单整理透视汇总表.xlsx」。

### final_pivot（SKU 匹配透视汇总）
- glob res_dir/*.xlsx（排除 ~$），逐文件按 sheet 名读 5 张：
  店铺数据透视/款号数据透视/匹配透视/收款匹配/退款匹配；
- 三张透视纯 concat（不再聚合）；
- 收退款明细：订单日期→订单年月（%Y%m，无日期='无日期'），
  按 店铺名称×订单年月 pivot sum 货款分摊金额，outer merge 成
  收款金额/退款金额两列；
- 输出「sku匹配数据透视汇总表_{时间戳}.xlsx」4 sheet：
  店铺数据透视汇总/款号数据透视汇总/数据透视明细汇总/按订单年月透视。

## 插件实现（优化适配）

| 项 | 文件 | 要点 |
|---|---|---|
| 引擎 | `core/summaries.py`（新建） | `bill_pivot_summary` / `sku_pivot_summary`；period 必填防跨账期叠加；platform/shop_code/bill_batch_id 可选过滤；SKU 增 state 过滤（缺省三态全扫：未匹配成本/已匹配成本/已回传数据库——同一批次只在一个状态目录，天然不重复） |
| 入口 | `actions.py` + `config.json` | 两个同步 action 工具（mcp_tools 24→26）：`recon_workbench__bill_pivot_summary` / `recon_workbench__sku_pivot_summary`，归一 {ok,status,summary,data}，失败可读不抛栈 |
| 输出 | `results/{YYYY-MM}/汇总/` | 时间戳文件名重跑不覆盖；同步 downloads 副本（管理页/聊天可下载）；返回 totals（收/退款/现金流合计、店铺数）与 skipped/errors 清单 |

相对原脚本的关键适配：
- **修复 sheet 位置假设**：final_bill_pivot 读 sheet_name=1，平台 sheet 序一变就错
  （插件各平台透视 sheet 名不同：抖音账单透视/快手账单透视/账单透视…）→
  改为**按 8 列结构识别**透视 sheet；缺列文件记 skipped 而非整体抛错；
- **批次发现改文件态布局**：原平铺目录 glob → artifacts.list_batches /
  list_sku_batches（账单整理/{批次}、sku匹配/{状态}/{批次}），VIP 多旬交付件逐份计入；
- **大簿提速**：SKU 汇总读 VIP 67MB 级交付簿，calamine 引擎优先（秒级），
  回落 openpyxl read_only 流式；收退款明细只取汇总所需 3 列（usecols）；
- **防自吞**：只扫批次根目录交付 xlsx（不递归底稿/汇总/下载副本），排除 ~$ 临时文件，
  重跑不会把上次汇总输出再吞进去（verify 有专项断言）；
- 原读失败 print 吞掉 → 逐文件聚合 errors/skipped，终态如实报。

口径保持：三张透视**纯 concat 不再聚合**（与影刀一致，跨店同名行保留）；
订单年月 outer merge 后 NaN 补 0；金额单位元（交付 Excel 已是元）。

## 验收

1. `scripts/verify_summaries.py`：**38/38 PASS**（临时 results 根 + 伪造批次）
   - 账单：8 列列序、合计（收 2950.5/退 300.25/净 2650.25）、无透视文件 skipped、
     sheet 位置可变仍识别、过滤/period 必填/无批次可读报错；
   - SKU：4 sheet 名与影刀一致、三态全扫 4 文件（含 VIP 两旬）、
     店铺透视 concat 不聚合（5 行）、订单年月 outer merge（202609 收 400/
     202610 收 500 退 100/无日期桶 100/退款 NaN→0）、VIP 两旬合并 600、
     totals 守恒 3600、state/bill_batch_id 过滤、重跑不吞上次输出、
     批次产物未被修改、actions 归一出口。
2. `scripts/t9_summaries.py`（宿主 MCP 通道，真实 2026-10 数据）：**6/6 ALL_OK**
   - 账单汇总：41 批次/60 文件 → **386 行**（收 70,840,182.46 / 退 65,696,115.43 /
     现金流 5,144,067.03 元，48 店；9 文件无透视 sheet 跳过）；
   - HYK 单批：11 行（收 44,510 / 退 36,055.18 / 净 8,454.82）；
   - SKU 汇总：41 批次 → 店铺透视 120 / 款号透视 35,645 / 匹配明细 54,222 /
     订单年月 140 行（收 119,424,242.91 / 退 94,647,070.55，46 店）；
   - state=已匹配成本 单态 39 批次（行数与全扫一致——其余 2 批为无交付件批次）；
   - **HYK 独立重算**：直接读交付簿收退款明细 groupby 重算 = 汇总表 75,957.0，逐行一致；
   - 下载副本 HTTP 200 可达。
3. `validate_extension` ok（26 工具）；`reload_extension_runtime` +
   `inspect_extension_ui` 全绿。

## 使用

```
recon_workbench__bill_pivot_summary(period="2026-10")            # 全账期
recon_workbench__bill_pivot_summary(period="2026-10", bill_batch_id="…HYK001")
recon_workbench__sku_pivot_summary(period="2026-10")             # 三态全扫
recon_workbench__sku_pivot_summary(period="2026-10", state="已匹配成本")
```
返回 data.file（汇总簿绝对路径）+ data.downloads（下载链接）+ totals/rows/skipped/errors。