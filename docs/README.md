# recon_workbench · 文档总索引

> 对账工作台插件（承接影刀 RPA「资金账单匹配sku对账」）的设计与记忆体系。
> 参照 AI_RPA 项目的 docs 规范建立（2026-11-16）。

| 目录 | 作用 | 何时读 / 写 |
|------|------|------------|
| [codemap/](./codemap/README.md) | 代码结构索引（模块树 / 入口 / task_lookup） | **改代码前**先 Locate；结构变更后 Sync |
| [bugfixlog/](./bugfixlog/README.md) | 已落地 bug 修复记忆（按日期目录 + index.json） | 修完可验证的 bug **当天登记**；排查同类问题先查 |
| [specs/](./specs/README.md) | 按日期管理的设计规格（YYYY-MM-DD-主题.md） | **迭代前**先写/读 spec；行为口径的 SSOT |

插件技能文档（AI 运行时按需深读）在 `skills/recon_assist/`：
SKILL.md 是插件说明书；`references/bill-methodology.md` 是总体方法论；
`references/platform-*.md` 每平台一份漂移应对手册。
**docs/ 面向开发迭代，skills/ 面向 AI 运行**，两者不重复：
口径变更先改 spec → 改代码 → 回归 → 同步 skills 文档与 codemap。

## 迭代工作流（约定）

```
1. Spec     docs/specs/YYYY-MM-DD-<主题>.md 写清背景/口径/验收（重大变更先与用户确认）
2. Locate   docs/codemap/index.json → task_lookup 定位模块；bugfixlog 查同类坑
3. Edit     最小 diff；链序/口径改动必须跑基准回归（全部数值列，防假绿）
4. Verify   8 月基准回归 ALL_OK + validate_extension + reload_runtime + UI 探活
5. Record   修了 bug → bugfixlog 当天条目；结构变更 → codemap 同步；口径变更 → skills 文档
6. Push     git commit + push（远程 github.com/Jhtony123/recon_workbench）
```

## 关键纪律（历史教训沉淀）

- **钱不能丢**：对平门不平先定位差额行给人类，禁止调容差放行。
- **回归防假绿**：比对必须覆盖全部数值列（2026-11-14 毛利率列漏比教训）。
- **字典可热修，链序不可乱动**：备注关键字链顺序即口径，改序必回归。
- **biz_code 整表接管**：停用单条≠回落单条；排除编码应改值为「未匹配」。
- **规则文件损坏显式报错**，不静默回落内置映射。
