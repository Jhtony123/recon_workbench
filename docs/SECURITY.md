# 凭据与分发安全（SECURITY）— 2026-11-18

本文档回答：**氚云 apikey / 数据库连接信息存在哪里？把插件分享给别人会不会泄露？**

## 一、凭据存储架构（结论：插件目录零凭据）

| 凭据 | 存储位置 | 读取链路 |
|---|---|---|
| 巨益 ADB：host / port / user / **password** / database | **本机环境变量** `RECON_ADB_*` | `core/config.py env()`：进程环境优先 → 缺失回落读 `HKCU\Environment` 用户级注册表（配完即用，不必重启） |
| 氚云 OpenAPI：**EngineCode / EngineSecret** | **本机环境变量** `RECON_H3_ENGINE_CODE` / `RECON_H3_ENGINE_SECRET` | `core/config.py load_h3_headers()`：仅在真实推送（dry_run=0）时读取并组装请求头 |

纪律（代码级保证）：
- 插件**不存任何凭据文件**：config.json / settings.json / 代码 / 文档里只有环境变量**名**，没有值；
- 原影刀脚本 5+ 处明文 host/user/password 与 main.py / error_push_chuanyun.py 的明文
  EngineCode/EngineSecret **已全部废弃**，未随移植进入插件（工作区与 git 全历史扫描 0 命中）；
- 所有状态探活（`env_status.credentials` / `db_status` / `h3_config_status`）只报
  「哪些变量已配/缺失」，**不回显值**；`AdbConfig.to_dict()` 口令恒 `***`；
- 氚云 SchemaCode 常量（H3_SCHEMA_*）是表单标识符，**非凭据**，公开无风险；
- dry-run 预检零 HTTP / 不连库，**不需要任何凭据**——接收方未配凭据也能用全部本地功能。

## 二、运行时数据在哪（分享插件目录不会带出）

全部在 **`data/extensions/recon_workbench/`（插件目录外）**：

| 文件 | 内容 | 敏感性 |
|---|---|---|
| settings.json | 仅 results_dir 路径 | 低（含本机用户名路径） |
| plugin.db | 仅店铺映射表（59 行店铺编码/名称） | 业务数据，非凭据 |
| results/ | 账单/SKU/成本/汇总产物 | 财务数据 |
| downloads/ | 下载副本 | 财务数据 |
| logs/jobs.jsonl | 作业留痕 | 低 |
| chuanyun_master.json | 氚云主数据快照（真实取数后生成；含店铺/科目/账户 ObjectId 与账号） | **业务敏感**，勿随包分发 |

**分享 = 只打包 `extensions/user/recon_workbench/` 插件目录**，上述运行时数据天然不在其中。

## 三、分发前自检（最后一道闸）

MCP 工具 **`recon_workbench__dist_check()`**（只读，不改文件不连库不发 HTTP）：

1. **工作区扫描**：把本机环境变量里的**真实凭据值**作为「已知秘密」在插件全部文本文件里
   找子串（值出现在文件里 = 打包必带出）；另扫硬编码口令样赋值（password/secret/token/api_key
   = "非占位值"）与 PEM 私钥块；
2. **git 全历史扫描**：秘密值/私钥是否曾提交过（分享 .git 时的泄露面）；
3. **运行时文件检查**：settings.json / plugin.db / results / downloads / logs /
   ssh_known_hosts 残留是否混入插件目录；
4. 返回 `verdict`：
   - **safe**：无任何命中，可分享；
   - **caution**：仅运行时文件/残留（非凭据），按 `packaging_excludes` 剔除后可分享；
   - **blocked**：凭据/私钥命中，先清理 findings 所列文件+行号再分享。
   命中值只回显首尾 2 字符打码（报告本身不成为二次泄露源）。

`packaging_excludes`（打包 zip 时剔除）：
`.git/ __pycache__/ *.pyc results/ downloads/ logs/ extracted/ *.db *.db-journal
*.parquet settings.json chuanyun_master.json *ssh_known_hosts* .venv/ .vscode/ .idea/`

> 建议连 `.git/` 一起剔除：历史虽经扫描无凭据，但含开发过程信息，分发无必要。

## 四、接收方（其他 Desktop Agent 用户）拿到插件后

1. 凭据为空 → `env_status` 如实报缺失 → **各自配置自己的环境变量**（值不会从你这里带过去）；
2. 不配凭据也能用：账单整理 / 成本匹配 / 透视汇总 / 回传与氚云的 dry-run 预检；
3. 要真实取数/回传/推送 → 配 RECON_ADB_* / RECON_H3_* 后重启桌面智能体；
4. 店铺映射需自行导入（plugin.db 不随包分发）；结果目录可在设置页自定义。

## 五、已知残留与注意事项

- 插件目录曾有一个 SSH 调试残留文件（`*ssh_known_hosts*`，仅 github.com **公钥**，
  无私钥、未被 git 跟踪）——无害，但 dist_check 会列入 caution，打包时剔除即可；
- **git 远端**（github.com/Jhtony123/recon_workbench）：推送前同样先跑 dist_check；
  若未来某次提交混入凭据，仅删除文件不够，须改写历史（filter-repo）并**轮换该凭据**；
- 会话/日志纪律：AI 与用户都不要在聊天里粘贴口令原文；配置环境变量用系统设置或
  setx，让值只存在于本机环境，不进对话记录。

## 六、审计记录（2026-11-18）

- 工作区全文本扫描：影刀明文 EngineCode/EngineSecret **0 命中**；硬编码口令样赋值 0 可疑；
- git 全历史（16 commits）：秘密值/私钥 **0 命中**（唯一 password 样行是环境变量名常量）；
- plugin.db 实测仅 `ext_recon_workbench_shop_map` 一张业务表；settings.json 仅 results_dir；
- 验证脚本 `scripts/verify_distcheck.py`（含「故意植入秘密→必须 blocked」的注入测试）。