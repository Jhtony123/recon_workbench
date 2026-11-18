# 总体 review：实施收尾对齐 + 功能接线/文档/死代码审查 — 2026-11-18

| Field | Value |
|-------|-------|
| date | 2026-11-18 |
| status | implemented |
| scope | docs/specs 索引、codemap、SKILL.md、core/pipeline.py、actions.py、config.json、死代码清理（8 文件）、4 个验证脚本断言 |
| 方法 | superpowers requesting-code-review（分 pass 自审：接线/文档/bug与死代码）+ verification-before-completion（修后新鲜验证） |

## 背景

五阶段（bill/match/cost/writeback/chuanyun）与配套服务（summaries/envcheck/distcheck）
全部实施完毕后，做一轮总体 review：实施文档销账、功能接线核对、SKILL 文档对齐精简、
潜在 bug 与死代码探查。

## 一、实施文档销账（docs）

- specs/README.md 索引补齐 4 份 11-18 spec（chuanyun/writeback/pivot-summaries/cost-match）；
- 2026-11-16-sku-workflow-and-output-layout：confirmed → **implemented**，
  「遗留与后续」四项全部销账（SKU/成本迁移/合并大表/氚云均有承接 spec）；
- 2026-11-17-sku-match-revival：Q8 入口接线遗留段改写为「已于 T4 补齐销账」；
- codemap v1.8.0 → **v1.9.0**：28 工具、五阶段 ACTIVE_STAGES、4 页 UI、
  core 文件清单补 chuanyun/summaries/envcheck/distcheck、references 14 份、
  docs 模块补 plans/SECURITY/dependencies。

## 二、功能接线审查（全绿）

- config.json 28 工具 ↔ actions.py 函数一一对应，无缺失、无多余 def；
- UI app.js 引用的工具全部在 config 声明内，无悬空调用；
- run.py 五 stage 分发正确；actions.start_recon 按 stage 校验必填参数；
- **dry_run 双层门完好**：actions 层缺省 "1" 且只有显式 0/false 才置 "0"；
  run.py 层同样缺省 "1"。writeback/chuanyun 直调工具转发 start_recon，门不旁路。

## 三、过时表述修复（review 发现的「文档性 bug」）

| 位置 | 问题 | 修复 |
|---|---|---|
| core/pipeline.py DEFERRED_STAGES | 仍列 writeback/chuanyun（已 active，靠 ACTIVE 门遮蔽才没出错） | 收窄为仅 full |
| core/pipeline.py DEFERRED_NOTE / 模块头 | 「四能力占位暂缓」陈旧叙述 | 改为五阶段全启用+full 占位语义（真实写外部系统须确认，不能随全链路静默执行） |
| actions.py capability_status | summary 恒拼「暂缓：」空串 | 条件拼接 + dry-run 边界提示 |
| actions.py db_status needed_by | 「SKU对账/回传（暂缓）」 | 改为取数/真实落库需要，dry-run 不连库 |
| config.json overview/db_status 描述 | 「SKU对账暂缓」 | 对齐现状 |
| SKILL.md | 回传/氚云「仍属暂缓」×3、休眠启用四步教程、4 平台清单、2026-11-14 环境快照 | 全部对齐；标准动作补第 8 步（回传/氚云 dry-run→确认→真实）与第 9 步（月度汇总）；环境以 env_status 为准 |

## 四、死代码清理（12 个零引用函数，AST 全扫 + 引用计数 + 逐一人工核实）

| 函数 | 文件 | 移除理由 / 现行口径 |
|---|---|---|
| run_full | core/pipeline.py | run.py/actions 对 full 一律走 deferred 占位门，从不路由到此；旧实现还会误报 writeback/chuanyun 暂缓 |
| _subset_sum_fen | core/allocation.py | 「最接近+残差补尾行」旧版；现行只取精确相等子集（_subset_sum_exact）+比例兜底 |
| verify_direction | core/allocation.py | 被 allocate() 硬校验 + verify_match_totals 三项校验覆盖（更严） |
| apply_advice | core/matching.py | 早期三分版；现行五类财务判断在 match_receipt/match_refund 内条件赋值 |
| _brand_code | core/cost.py | 现行 enrich_from_db 直接 BRAND_DICT.get（缺失保 NaN），语义不同勿复活 |
| cost_summary_by_advice | core/cost.py | 旧管理页成本卡片载荷；能力页已下线 |
| period_from_label | core/config.py | 文件态以目录名 {YYYY-MM} 为准，无需中文标签反解 |
| _read_parquet | core/artifacts.py | 旧别名，无调用方；一律 _read_table/read_detail/read_goods |
| apply_biz_codes | core/platforms/base.py | 各平台直接消费 biz_code_map()（整表接管+各自 fallback）；通用版会丢兜底口径 |
| ensure_schema / vacuum / upsert_rows | storage.py | write_connection 建连即 executescript；单表走 upsert_row；库维护归宿主 db_maintenance |

保留：core/pipeline_dormant.py（历史封存，有意保留，不在运行路径）；
artifacts.write_parquet（pipeline 在用）。每处移除均留注释说明现行口径，防误复活。

## 五、验证脚本断言对齐（过时断言=假红/假绿源）

- verify_envcheck：mcp_tools 27 → 28；
- verify_writeback：「chuanyun 仍 deferred」→「已 active」；
- t4_mcp_jobs / t7_writeback_dryrun：capability active 集合改为五能力。

## 验收（修后新鲜运行，2026-11-18）

- py_compile 70 文件 0 失败；codemap/config JSON 解析通过；
- verify_writeback 38/38 · verify_chuanyun 62/62 · verify_envcheck 25/25 ·
  verify_summaries 38/38 · verify_clear_history 39/39 · verify_distcheck 24/24
  = **226 项全 PASS**；
- 导入冒烟：ACTIVE_STAGES 五阶段、DEFERRED 仅 full、capability_status 五能力 active；
- reload_extension_runtime + inspect_extension_ui 探活零报错。

## 遗留与后续

- git commit + push（推送前必跑 dist_check；建议连 .git/ 一起按 packaging_excludes 处理分享包）；
- 插件目录 ssh_known_hosts 公钥残留可删（caution → safe）。