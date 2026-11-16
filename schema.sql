-- recon_workbench：电商财务月度对账工作台数据层
--
-- 【2026-11-14 方案改版】数据库**只存店铺映射**一张表。
--   用户决策：
--     1) 不存 SKU 对账明细结果，也不存账单整理结果；
--     2) 插件只做影刀现有能力：账单整理（可用）+ SKU对账/成本匹配/数据库回传/氚云对接（占位暂缓）。
--   原先 11 张表 → 1 张表，其余职责改由文件承担：
--     · 平台解析规则  -> {plugin}/rules/{platform}.json      （core/rules_store.py）
--     · 作业留痕      -> data/extensions/recon_workbench/logs/jobs.jsonl （core/joblog.py）
--     · 账单整理产物  -> results/{账期}_{平台}_{店铺}_{时间戳}/*.xlsx + *.parquet （core/exporters.py）
--     · SKU对账/成本/回传产物 -> 占位暂缓，启用时再决定落盘形态（**不入本库**）
--   为什么规则与日志不放库里：它们是「配置」与「过程记录」，不是对账结果；
--   放文件可进版本管理、可 diff、可手工修，也避免库表随功能演进反复迁移。
--
-- 金额口径：库内已无金额列（店铺映射表只有编码与名称）。
--           账单/匹配产物的金额仍以「分」的 int 在内存与 parquet 中流转，导出 Excel 时转元。
-- 账期口径：period 统一 'YYYY-MM'；账单起止 s_date/e_date 为 'YYYY-MM-DD'。

-- ============ 历史遗留表清理（幂等；老库升级后自动收窄到单表）============
-- 注意顺序：先删索引再删表。DROP TABLE 本身也会连带删除该表的索引，
-- 这里显式 DROP INDEX 只是为了在表已被手工删除的情况下仍保持幂等、不留孤儿索引。
DROP INDEX IF EXISTS idx_recon_bill_batch;
DROP INDEX IF EXISTS idx_recon_bill_period;
DROP INDEX IF EXISTS idx_recon_oms_order;
DROP INDEX IF EXISTS idx_recon_match_batch;
DROP INDEX IF EXISTS idx_recon_match_state;
DROP INDEX IF EXISTS idx_recon_match_period;
DROP INDEX IF EXISTS idx_recon_pivot_shop;
DROP INDEX IF EXISTS idx_recon_pivot_prod;
DROP INDEX IF EXISTS idx_recon_rules;
DROP INDEX IF EXISTS idx_recon_cost;

DROP TABLE IF EXISTS ext_recon_workbench_bill_rows;
DROP TABLE IF EXISTS ext_recon_workbench_bill_goods;
DROP TABLE IF EXISTS ext_recon_workbench_oms_rows;
DROP TABLE IF EXISTS ext_recon_workbench_match_detail;
DROP TABLE IF EXISTS ext_recon_workbench_pivot_shop;
DROP TABLE IF EXISTS ext_recon_workbench_pivot_product;
DROP TABLE IF EXISTS ext_recon_workbench_writeback_log;
DROP TABLE IF EXISTS ext_recon_workbench_platform_rules;
DROP TABLE IF EXISTS ext_recon_workbench_cost_base;
DROP TABLE IF EXISTS ext_recon_workbench_job_log;

-- ============ 唯一保留表：店铺映射 ============
-- 店铺映射是「跨账期、跨平台复用的主数据」，不是某次运行的结果，
-- 所以它留在库里；带 version 便于按导入批次留痕与回滚对照。
CREATE TABLE IF NOT EXISTS ext_recon_workbench_shop_map (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_code TEXT NOT NULL,
  shop_id TEXT NOT NULL DEFAULT '',
  shop_name TEXT NOT NULL DEFAULT '',     -- shopCode_shopName
  platform TEXT NOT NULL DEFAULT '',
  chuanyun_name TEXT NOT NULL DEFAULT '', -- 氚云名称（氚云对接启用后使用）
  active INTEGER NOT NULL DEFAULT 1,
  source_file TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  imported_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (version, shop_code)
);

CREATE INDEX IF NOT EXISTS idx_recon_shop_code ON ext_recon_workbench_shop_map(shop_code, active);
