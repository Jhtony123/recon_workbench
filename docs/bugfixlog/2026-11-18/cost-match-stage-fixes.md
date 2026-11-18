# 成本匹配阶段实施中修复的缺陷与口径修正（2026-11-18）

## 背景

cost-match-stage spec（reviewed，A1–A8 审核修订）实施过程中，T2–T4 验收暴露并修复的缺陷。
业务 SSOT：`skills/recon_assist/references/cost-match-methodology.md`。

## 1. apply_cost 重算标准成本会覆盖 VIP FR 特例（spec A1，预防性修复）

- **现象**：既有 `apply_cost`（match 阶段语义）重算 标准成本=单价×数量；若 cost 阶段复用，
  会覆盖 SKU 阶段写入的 VIP FR「标准成本=结算金额」特例（基准 FR 62044 行 0 行=单价×数量）。
- **修复**：cost 阶段独立分支 `apply_cost_stage`，标准成本/标准成本单价一律透传（safe_int 只规整类型）。
- **验证**：VIP 上旬基准 FR 62044 行 发货=标准=结算 全等（verify_cost_match.py 锚点）。

## 2. 采购单价展示列存分导致与基准差小数尾（spec A2，预防性修复）

- **现象**：既有实现展示列存 unit_cost_fen（量化到分）；基准该列是原始精度小数（52.212389380531）。
- **修复**：内部列 `采购单价_raw` 携带原始精度，出口 `_write(raw_overrides={"采购单价": …})` 覆盖展示，
  不经 `_fen_to_yuan` 量化；内部计算列仍存分。
- **验证**：HYK 冒烟逐行 采购单价=表值（1e-6）0 差。

## 3. cost 透视吊牌价合计归零（T2 冒烟实测缺陷）

- **现象**：HYK 冒烟 款号/匹配透视吊牌价合计 got=0 base=259990/294444。
- **根因**：`_cost_pivot_tag_text` 已把吊牌价转元文本（'688.0000'），`_write` 又对它过
  `_fen_to_yuan` → `from_fen('688.0000')` 归 0。
- **修复**：cost 透视 money 列集合排除吊牌价（pivot_money/match_pivot_money）。
- **验证**：重跑冒烟 SMOKE_OK。

## 4. 失败注入暴露的三处终态/可读性缺陷（T3）

- 缺 sheet 报错不带 sheet 名、未落 joblog failed 终态 → load_cost_base 报错带 sheet；
  run_cost 捕获后返回 failed StageResult + joblog 留痕。
- VIP 单旬失败/迁移失败时成功单元被吞、误报 failed → 成功单元计入 unit_reports，终态 partial。
- `plat` 在成本表失败分支未定义（UnboundLocalError）→ 前置定义。
- **验证**：test_cost_migration.py 11/11 ALL_OK。

## 5. `_json` 未定义（T4 MCP 真实触发暴露）

- **现象**：MCP cost Job failed：UnboundLocalError `_json`。
- **根因**：`_run_cost_unit` 下载副本段先用 `_json.dumps`，import 在函数后段。
- **修复**：import json as _json 提到函数首。
- **验证**：t4_mcp_jobs.py 4/4 ALL_OK（cost/match Job 真实触发成功）。

## 6. spec A5 口径修正：id 空值是空白单元格，不是字面串 "None"

- **原记录**：A5 称影刀 astype(str) 副作用使 id 空值→字面串 "None"（47 行）。
- **实测**（probe_cost_cells.py，openpyxl 单元格级）：成本基准 id 空值是 **NoneType（空白单元格）**，
  HYK 47 行、VIP/AKC 全空。原记录是 pandas 读 Excel 把空白读成 NaN 再 str 化的假象。
- **实现**：id 空值→None（空白）；spec/验收锚点同步改为「id 空白 47 行」。

## 7. 占位符分平台语义（取证补充，probe_cost_cells3/4）

- B2C 品牌/品类空→「未知」；VIP/AKC 品类空→字面 'None'（影刀读 Excel astype(str) 副作用，
  VIP 70498 行、AKC 匹配透视 'None' 组实测）；AKC 季节空保持空白；
  吊牌价是文本列（B2C/VIP '688.0000' 4 位小数、AKC '1118.0' 1 位小数），出口按平台格式化。

## 8. 货款金额补录/秒退行误填 0（T5 归因取证暴露）

- **现象**：KS001/DY020 退款匹配 货款金额 got=0.0 base=None 被回归标 DIFF。
- **根因**：出口层误把空值 fillna(0)。单元格级实测基准是**空白**（HYK 收 11/退 16、
  KS001 7、DY020 3，均为 成本不做处理/未匹配 或 冲发出商品/已匹配 的无账单金额行）。
  此前探针报「0 空白」是 pandas 把 None 读成 NaN、`is None` 判断漏检的假象。
- **修复**：_cost_detail_for_export 不再 fillna(0)，空值由 _fen_to_yuan 归 None → 空白。
- **验证**：HYK 冒烟重跑 SMOKE_OK；T5 全量重跑 KS001/DY020 该项消除。

## 9. match-skipped 空批次被 cost 误报 failed（T5）

- **现象**：DY017（账期货款 0 行）/DYY001（账期无出入库单）match 阶段 skipped，
  目录为空；run_cost 报「无 match 底稿（半截批次？）」failed。
- **修复**：无底稿且无交付件 → skipped_batches（跳过不算失败）；有交付件无底稿才是半截 failed。
- **验证**：T5 重跑终态不再含这两个批次的失败项。

## 关联

- spec：docs/specs/2026-11-18-cost-match-stage.md
- plan：docs/plans/2026-11-18-cost-match-stage-plan.md
- 回归：scripts/run_cost_smoke_hyk.py / test_cost_migration.py / t4_mcp_jobs.py / verify_cost_match.py
