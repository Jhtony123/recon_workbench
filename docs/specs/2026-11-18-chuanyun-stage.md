# 氚云对接阶段启用（chuanyun stage）— 2026-11-18

状态：**implemented**（代码功能完成；按用户决策**测试不向氚云传任何数据**，入口恒缺省 dry-run）

## 背景与用户决策

五环节最后一步，承接影刀「api导入氚云」分支（main.py L729-771 → api_chuanyun.py）。
与数据库回传同模式：**只做代码移植（可优化适配），dry-run 缺省，不真实推送**。

## 影刀原逻辑（api_chuanyun.py）

1. 五张氚云主数据表 LoadBizObjects（只读）：账单科目 / 管理科目 / 店铺（决算部门）/
   资金账户（第三方资金平台+启用）/ 决算部门-巨益编号；
2. 逐 xlsx 读 sheet0（氚云导入模板：店铺名称/业务类别/资金方向/金额，元），
   映射 ObjectId/平台/公司/品牌/巨益编号/科目链/税率，四道校验门
   （店铺/平台/公司/业务类别/预算科目 未匹配 → 跳过该文件）；
3. 资金账户匹配：爱库存按「爱库存」关键字；其余按决算部门 contains，
   文件名含「微信」选微信户（账号置空），否则排除微信户；
4. 表头：收/付款合计、净现金流、账户余额、当月余额、月末结算日、
   摘要 note、录入查重=YYYY-MM+账户obj、预算管控模式、金蝶推送状态=未生成；
5. 子表 26 字段：含税/不含税（÷(1+税率)）/税费、预算标签=年_月_科目_部门、
   收款=释放（负占用）/付款=占用；
6. CreateBizObject（SchemaCode=D283056ad6c8…，IsSubmit=true，BizObject 为 JSON 字符串），
   每文件 sleep(5) 限流；成功 move 到 氚云导入成功/；
7. 缺陷：明文 EngineCode/EngineSecret、`except: print` 吞异常、
   **api_moneyValue 时序 bug**（moneyAcct_obj 还是空串就查余额 → 恒 0）。

## 插件实现（优化适配）

| 层 | 文件 | 要点 |
|---|---|---|
| 引擎 | `core/chuanyun.py`（新建） | `load_template`（读交付 Excel 模板 sheet）/ `fetch_master_data`（五表，字段映射逐位核对，状态过滤同口径）/ `fetch_account_balance`（**时序修复：先匹配账户再查余额**）/ `build_payload`（主表+子表 26 字段全映射，四道校验门，微信/爱库存分支）/ `push_biz_object` / `save_preview` / 主数据快照 save/load（runtime/chuanyun_master.json，真实取数自动留存，dry-run 复用） |
| 编排 | `core/pipeline.py run_chuanyun` | 批次发现 = artifacts.list_batches（账单整理批次，bill_batch_id 可选）；逐交付 Excel 读模板 sheet；空模板 skipped；dry-run 预览落 `底稿/chuanyun_preview/{文件名}.json`；真实推送成功文件移入批次内 `氚云导入成功/` 并更新 manifest excel/excel_list；失败留守；joblog 留痕 |
| 凭据 | `core/config.py` | RECON_H3_ENGINE_CODE / RECON_H3_ENGINE_SECRET 环境变量（原明文废弃）；H3_API_URL + 8 个 SchemaCode 常量（非凭据）；h3_config_status 探活不暴露密钥 |
| 异常 | `core/errors.py` | ChuanyunError（单文件失败聚合，整体不可用才抛） |
| 入口 | `run.py` / `actions.py` / `config.json` | stage=chuanyun 进 ACTIVE_STAGES（五能力全 active）；`chuanyun_sync()` MCP 工具恢复（period 必填，dry_run 缺省 1）；start_recon 分发；allowed_domains 增 www.h3yun.com |
| UI | `ui/index.html` / `app.js` | #/chuanyun 真实表单：账期+账单批次下拉、预检按钮（恒 dry_run=1）、file_reports 结果表（收/付/净/子表行数/跳过原因）；占位卡全部退役 |

**dry-run 纪律（三层一致）**：零 HTTP 调用——连主数据只读查询也不发；
无快照时 ObjectId 留空 + warnings 明示「待真实取数后补全」，金额/税额/预算标签等
本地可算字段照常生成（骨架预览）。真实推送须显式 `dry_run=0` 且经用户确认。

## 验收（零真实 HTTP）

1. `scripts/verify_chuanyun.py`：**62/62 PASS**
   - payload 主表 F 字段 + 子表 26 字段与影刀逐字段核对（税额拆分 1000/1.13=884.96、
     释放/占用符号、预算标签、录入查重、摘要口径）；
   - 微信/爱库存账户分支、四道校验门跳过、无快照骨架（无 'nan' 泄漏）；
   - dry-run 路径 requests.post / fetch_master_data 均为「调用即失败」桩 → 证明零 HTTP；
   - FakeResp 真实路径：HTTP 序列 5 主数据+1 余额+1 推送、余额时序修复（台账 500）、
     成功移动+manifest 更新、失败留守、快照自动留存；
   - 端到端（临时 results 根 + 伪造账单批次 Excel）。
2. `scripts/t8_chuanyun_dryrun.py`（宿主 MCP 通道真实 Job，恒 dry-run）：**4/4 ALL_OK**
   - 全账期 2026-10：**41 批次 / 60 模板文件，50 预检通过 / 10 跳过**（无模板 sheet 的批次），
     pushed_to_h3=False、master_loaded=False（骨架模式）；
   - HYK 单批次预检通过；capability 五能力 active；`氚云导入成功/` 子目录 0（零移动）。
3. `validate_extension` ok；`reload_extension_runtime` + `inspect_extension_ui` 全绿。

## 真实启用条件（日后）

用户确认 → 配 RECON_H3_ENGINE_CODE/SECRET 环境变量 → AI 显式
`chuanyun_sync(period=…, dry_run=0)` → 首次真实取数自动留主数据快照（此后 dry-run
可出完整 ObjectId 映射预览）→ 建议先单店试跑核对氚云表单，再全账期。
注意：真实推送会提交流程（IsSubmit=true）并触发氚云预算占用，务必先与财务确认账期无重复
（录入查重字段=YYYY-MM+账户obj 可防重）。