# SKU 对账产物与影刀 10 月基准全面对齐（三轮精修）

| Field | Value |
|-------|-------|
| date | 2026-11-17 |
| area | export_format / pivots / cost / matching / b2b |
| status | fixed |
| spec | docs/specs/2026-11-17-sku-match-revival.md（Q3 笔误修正白名单） |
| 验证 | HYK001 6/6 sheet OK · VIP 第一旬 6/6 OK · AKC 除品牌字典漂移外全 OK；分摊差异恒 0 分 |

## 背景

SKU 对账启用后首次与影刀 RPA 10 月实际运行结果（`桌面\2026.10月账单\SKU匹配汇总-已有结果可作为对照验证`）逐 sheet 全列比对，发现 8 类不一致。按用户要求「先精准定位根源、每平台单店冒烟」三轮修复收敛。

## 根因与修复（按发现顺序）

### A1 匹配透视「结算金额」整列放大 100 倍
- 根因：`exporters.export_match_result` 给匹配透视误传 `PIVOT_MONEY_COLUMNS`（含「订单结算金额」、不含「结算金额」），定义好的 `MATCH_PIVOT_MONEY_COLUMNS` 从未接线——与 11-14「列序常量定义了没人用」同类缺陷。
- 修复：换用 `MATCH_PIVOT_MONEY_COLUMNS` + 新增 `MATCH_SHEET_COLUMNS`（基准 unicolumns 15 列序）。

### A2/A3 店铺/款号透视毛利恒 100%、冲发出商品恒 0
- 根因：`pivots._assemble_pivot/_add_margin` 搬的是 **cost_match 成本匹配阶段**口径（扣发货成本 + 除税 1.13）；本阶段基准是 `look_sku.result` L1706/L1895：**发货成本恒 0、毛利=收入−标准成本、不除税、冲发出商品取标准成本**。
- 修复：`_add_margin/_assemble_pivot/pivot_shop/pivot_product` 增加 `stage="match"|"cost"` 分支，默认 match；dormant 成本管线显式传 `stage="cost"`。

### A4 B2B 明细发货成本填了标准成本
- 根因：`b2b.match_b2b_frame` 把 cost_match 阶段的 FR 特例（发货成本=标准成本）提前用到 SKU 阶段；原 B2B_match L2080/L2116 发货成本恒 0。
- 修复：删除 `detail["发货成本"] = detail["标准成本"]`。

### A5 占位字面量语义错（无款号/无品牌成批出现）
- 根因：`cost.enrich_from_db` 用 `astype(str).fillna(占位)`——NaN 已变 'nan' 串，fillna 永不生效，再被 `mask(=="")` 统一换占位。原脚本逐列语义不同：
  - 款号/吊牌价：`astype(str)` 链 → NaN 保持空白；
  - 商品名称/年份/季节：`.str` 链保留 NaN → fillna 生效填「无商品名称/无年份/无季节」；
  - 品牌：`brand_dict.get` 无默认值 → 字典外 NaN（透视层 fillna("未知")）；
  - 品类：B2C 不在 enrich 赋值（保留 OMS join NaN）；B2B 查字典 miss→NaN。
- 修复：逐列重写缺失语义 + `is_b2b` 参数；B2B 年份/季节空串经 `.str` 链 fillna 失效 → 保持空白（VIP 86/99 行实测）。

### B1 回传表「账单周期」全空
- 根因：从明细带列（补录行为空）；原 L1553 在 id 透视后**统一赋 bill_time**。
- 修复：`build_oms_writeback(..., period=)` 末尾统一赋值。

### B2 三张透视缺「店铺编码」列 / 匹配透视列序错
- 根因：11-14 把店铺编码当内部列裁掉（实际基准三张透视第 2 列都有）；匹配透视缺品类/年份维度。
- 修复：`SHOP/PRODUCT_SHEET_COLUMNS` 补店铺编码；新增 `MATCH_SHEET_COLUMNS`；`build_match_pivot` 输出 15 列。

### B3 空「已匹配的巨益单号」表列头单列占位
- 修复：`exporters._write` 空 df 分支保留真实列头（id/账单周期/数量）。

### C 空值语义成批差异（合计金额全相等）
- 采购单价：账单侧行→NaN（原 L659/L695 初始 ""）；补录行（货款金额 NaN）→0（L917/L943）；**收款侧**秒退行→0（L824），退款侧秒退交叉标记行不改（保持 NaN）。
- 货款金额：组内重复行 mask 为 NaN（原 Excel 往返读回空白），非 0。
- 标准成本单价：B2C fillna(0)（L1278）；B2B 无 fillna（L2187）→ miss 保持 NaN（VIP 6 万行）。
- 是否秒退判断：初始 ""，已匹配行强制 ""（L794），不写「否」；新增 `mark_refund_instant`（L860-887 收款秒退按单号/订单号传导退款侧）。
- `_fen_to_yuan`：NaN/None 保持空白，不归 0。

### 多店一簿店铺编码张冠李戴
- 根因：`_shop_code_of` 取该侧首个编码套给所有店铺（AKC 可可尼/秋水伊人互换）。
- 修复：新增 `_shop_code_map` 按店铺名称逐行映射。

### B2B 透视分组键 fillna 失效特例
- 根因：原脚本品类/季节经 `.astype(str)/.str` 链变 object dtype，`fillna("未知")` 失效 → 基准空白（AKC 品类 52 行、VIP 季节 18 行）；B2C 保留真 NaN → fillna 生效「未知」。
- 修复：`build_match_pivot` 对 B2B（mutate_detail=False）品类/季节保持空白；B2C 就地改写明细（原脚本无 .copy()，明细 sheet 品类/品牌也显示「未知」）。

### 测试便利：VIP 单旬过滤
- `resolve_batch` 按 batch_id 逐份扫多旬 manifest；`run_match` B2B 分支按 manifest files.excel 收敛单旬。

## 白名单（预期差异，人工确认）

| 项 | 说明 |
|---|---|
| 冲发出商品数量 84 行（HYK） | spec Q3 已确认修正的源码笔误（L1804-1805 链式赋值把数量覆盖成标准成本金额；基准 -49.8 等是金额不是数量） |
| 订单日期格式 | 基准带 00:00:00 + 取最新 order_date 有意改进 |
| 品牌 QS→FR（AKC 26 款） | DB AI_product_info 现值与 10 月运行时不同，两侧同走 brand_dict，非代码缺陷；待拿当时字典快照确认 |
| ~~明细尾列 数据来源/店铺编码/成本缺失原因~~ | 2026-11-18 用户确认移除：全平台基准实测均 31 列；内部字段仅留底稿 csv，exporters.DETAIL_SHEET_COLUMNS 严格裁列（spec B3 附加列白名单作废） |

## 教训

1. **阶段口径不能混用**：look_sku（SKU 对账）与 cost_match（成本匹配）的透视毛利/发货成本/冲发出商品口径完全不同，迁移时必须按阶段分支，不能默认搬后者。
2. **常量定义了必须接线**：MATCH_PIVOT_MONEY_COLUMNS / SHOP_SHEET_COLUMNS 两次「定义了没人用」造成 100 倍单位 bug 与裁列错误——出口层常量要有回归断言守着。
3. **pandas fillna 对 astype(str) 后的 NaN 无效**：原脚本多处占位 fillna 实际失效，基准真实形态是空白/未知混合，必须逐列实测而非照抄代码字面。
4. **空值语义是财务信号**：NaN（未取价/字典缺失）vs 0（补录/秒退）不能互相替代，合计相等不代表逐行一致。
