# 第二批 7 平台对接（快手/视频号/小红书/有赞/好衣库/爱库存/唯品会）

| Field | Value |
|-------|-------|
| date | 2026-11-16 |
| status | implemented |
| scope | core/platforms/ 新增 7 适配器 + registry + pipeline 空账期分支 |

## 背景与问题

第一批 4 平台（淘系/抖音/京东/拼多多）对平后，补齐影刀 bill_arr.py 其余平台，
目标 11 平台全量接入（得物 8 月无样本未接）。

## 决策（口径要点，逐平台详见 skills/recon_assist/references/platform-*.md）

- **快手**：双源（资金账单 csv + 订单流水 xlsx）；货款净额硬门对平后，货款改用
  订单流水 10 组件展开；缺订单显式报错提醒用户补；
- **视频号**：动帐类型单层字典；「订单交易｜先用后付」全角竖线是易漂移点；
- **小红书**：结算单口径（xhs_daily 订单/日账单口径弃用）；空账期按基准导
  全零透视簿（新增 export_when_empty 机制）；
- **有赞**：utf-8（特例）+ index_col=False（行尾多空字段防错位）；充值二次分类；
- **好衣库**：全列反引号剥离（StringDtype 坑）；16 费用组件现金流硬门；
  目录标 HYK002 但映射/基准是 HYK001；
- **爱库存**：单文件多店 13 sheet 白名单（未知 sheet 显式报错）；款号=sku[:-5]
  推导（去 ADB 依赖）；文件名不含店铺；
- **唯品会**：按旬出账不做日期过滤；7 文件族；品牌款号前缀规则顺序敏感；
  含税先 round(2) 再除税（H 表税率不 round）；多店一簿。

## 实现要点

- 每平台继承 BillAdapter，差异写 detail_frame/export_books；
- pipeline 空行分支：export_when_empty=True 走全零簿导出而非 skipped；
- 回归脚本 verify_batch2.py（工作区 scripts/）：vip 大文件后台跑落日志。

## 验收

ks / sph×4 / xhs×3（含空账期）/ yz×2 / hyk / akc / vip 三旬
（301497/289895/278951 行）全部逐 sheet/列头/行数/全部数值列合计 ALL_OK；
第一批复跑无回归。

## 遗留与后续

- 得物（dw_arr）待有真实账单样本后接入；
- 本会话修复的 5 个缺陷见 bugfixlog/2026-11-16/。
