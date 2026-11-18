# recon_workbench 运行环境与依赖（分发用户必读）— 2026-11-18

本插件运行在 **Desktop Agent 的 sidecar Python 运行时**里（不是系统 Python）。
本文档随插件分发；依赖清单 SSOT = `core/envcheck.py DEPENDENCIES`，
与 `requirements.txt`、`skills/recon_assist/environment_checks.yaml` 三处同步
（`scripts/verify_envcheck.py` 有一致性断言）。

## 一、快速自检（推荐入口）

对 AI 说「检查对账插件环境」，或直接调 MCP 工具：

```
recon_workbench__env_status()
```

返回：
- `packages[]`：逐包 installed/version/tier/purpose/fallback；
- `features{}`：六能力就绪矩阵（ready / degraded / not_ready + 原因）；
- `missing{required,conditional,optional}` + `install_guidance`（venv_install 安装清单）；
- `credentials{adb,h3yun}`：环境变量凭据就绪状态（不暴露口令）。

只读探测：不连数据库、不发 HTTP、不安装任何东西。

宿主侧也有两道自动检测（插件安装/激活后生效）：
- `env_check`（L0）：读 `skills/recon_assist/environment_checks.yaml` 逐包探测；
- 启动体检 / 设置页「系统环境」：同清单注册项。

## 二、依赖分层

| 层 | 包 | 用途 | 缺失后果 |
|---|---|---|---|
| **核心必需** | pandas>=2.0 | 全部能力的数据处理 | 插件整体不可用 |
| | numpy>=1.24 | 数值计算（随 pandas 安装） | 同上 |
| | openpyxl>=3.1 | Excel 读写兜底引擎 | 导出/汇总不可用 |
| **条件依赖** | mysql-connector-python>=8.0 | 巨益 ADB 驱动（SKU对账取数/数据库回传真实落库） | 仅这两项真实路径报可读错误；账单整理/成本匹配/汇总/dry-run 预检不受影响 |
| | requests>=2.28 | 氚云 OpenAPI（真实推送） | 仅真实推送不可用；dry-run 预检零 HTTP 不受影响 |
| **可选加速** | xlsxwriter>=3.1 | Excel 写引擎首选 | 自动回落 openpyxl，功能不变 |
| | pyarrow>=14.0 | 底稿 parquet 格式 | 自动落 CSV（utf-8-sig），列序与金额口径完全一致（fastparquet 亦可） |
| | python-calamine>=0.2 | 大簿秒级读（VIP 67MB 级汇总） | 自动回落 openpyxl 流式读（分钟级），结果一致 |

版本下限按本机实测通过版本族给出（pandas 3.0.4 / numpy 2.5.0 / openpyxl 3.1.5 /
requests 2.34.2 / mysql-connector-python 26.10.0 / python-calamine 0.9.1）。
代码未用 pandas 3.x 独有 API，2.x 理论可用但未验证。

## 三、缺环境怎么装（Desktop Agent 用户）

**让 AI 装（推荐）**：对 AI 说「安装对账插件缺的依赖」。AI 会：
1. 调 `recon_workbench__env_status` 拿缺失清单；
2. 调宿主 `venv_install(packages=[...])` 安装到 sidecar 运行时（会请求用户确认）；
3. 装完复检 env_status 确认 ready。

**不要** `pip install` 到系统 Python——插件不跑在那里；也不要 Bash pip（宿主纪律禁止）。

最小安装组合：
- 只跑账单整理/成本匹配/透视汇总（纯本地）：`pandas numpy openpyxl`（+可选加速包）；
- 加 SKU 对账/数据库回传：再装 `mysql-connector-python`；
- 加氚云真实推送：再装 `requests`。

## 四、凭据（环境变量，与依赖分开配）

依赖装齐 ≠ 能连外部系统。真实取数/回传/推送还需要凭据（代码与 config.json 均不存口令）：

| 变量 | 用途 |
|---|---|
| RECON_ADB_HOST / RECON_ADB_PORT / RECON_ADB_USER / RECON_ADB_PASSWORD / RECON_ADB_DATABASE | 巨益 ADB（SKU对账取数、数据库回传）；DATABASE 缺省 xianxia |
| RECON_H3_ENGINE_CODE / RECON_H3_ENGINE_SECRET | 氚云 OpenAPI（真实推送） |

在**系统环境变量**配置后重启 Desktop Agent 生效。`env_status` 的 credentials 段与
`db_status` 工具都会报就绪状态与缺哪些变量（不暴露值）。
dry-run 预检、账单整理、成本匹配、透视汇总**不需要任何凭据**。

## 五、分发注意

- 插件目录整体拷贝/打包即完成分发（含本文档、requirements.txt、environment_checks.yaml）；
- 结果目录可在管理页「设置」自定义（results_dir），缺省 data/extensions/recon_workbench/results；
- 数据库只存店铺映射（首次使用需导入店铺表，见 SKILL.md）；
- 回传/氚云缺省 dry-run，真实写外部系统须显式 dry_run=0 且经用户确认（安全纪律，勿改）。