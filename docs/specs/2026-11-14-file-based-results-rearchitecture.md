# 方案改版：数据库只存店铺映射，账单结果落文件

| Field | Value |
|-------|-------|
| date | 2026-11-14 |
| status | implemented |
| scope | 全插件（storage / pipeline / artifacts / UI / MCP 工具面） |

## 背景与问题

旧版把账单行/匹配明细/透视/回传日志全部落 SQLite（11 张表），本地库膨胀且
与「数据库只是中转」的定位冲突；用户决策收缩能力面。

## 决策（用户确认）

1. 数据库**只存店铺映射**（shop_map，唯一保留表）；
2. 插件只做影刀现有能力：账单整理可用；SKU对账/成本匹配/数据库回传/氚云对接
   **占位暂缓**（deferred 终态，不静默成功也不抛栈）；
3. 账单整理结果落文件：交付 Excel（影刀基准格式）+ 机器可读表格
   （parquet 优先，无引擎回落 utf-8-sig CSV），金额内部一律整数分；
4. 规则迁出数据库 → `rules/{platform}.json`（可 diff、可版本管理）；
   作业留痕 → `logs/jobs.jsonl`；
5. `storage.ALLOWED_TABLES` 是运行时硬闸门：写退役表抛错；重新启用 SKU 对账
   必须显式加表名 = 一次有意的方案评审；
6. 总览不再给匹配率/未匹配差异（恒 0 会被误读成「全对上了」）。

## 实现要点

- `core/pipeline.py` 只保留 stage=bill；封存算法整体在 `core/pipeline_dormant.py`
  （三层回归全绿成果，不重写）；
- `core/artifacts.py`：批次目录即索引（目录名含账期/平台/店铺），manifest 补口径与合计；
- 已退役 MCP 工具从 config.json 移除（query_match_detail 等 6 个 + stage/参数）。

## 验收

第一批 4 平台（DY007/PDD003/TBQY001/JD001）8 月基准逐 sheet/列头/行数/全部数值列
合计 ALL_OK；validate + 热重载 + UI 探活零报错。

## 遗留与后续

- 结果目录命名与覆盖策略在 2026-11-16 spec 中改版（时间戳目录→固定名覆盖）；
- SKU 对账启用时从 pipeline_dormant 取回三段并决定落盘形态。
