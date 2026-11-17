# Specs — 按日期管理的设计规格

> 命名：`YYYY-MM-DD-<kebab-主题>.md`（日期=定稿日）；参照 AI_RPA `docs/specs` 规范。
> spec 是**行为口径的 SSOT**：改口径先改/写 spec，再改代码、回归、同步 skills 文档。

## 模板

```markdown
# <主题>

| Field | Value |
|-------|-------|
| date | YYYY-MM-DD |
| status | draft / confirmed / implemented / superseded-by:<id> |
| scope | 影响的模块 / 平台 |

## 背景与问题
## 决策（用户确认的口径，逐条可验收）
## 实现要点（模块/函数级）
## 验收（回归命令 + 预期 ALL_OK 范围）
## 遗留与后续
```

## 索引（新→旧）

| 日期 | spec | 状态 |
|------|------|------|
| 2026-11-17 | [SKU 对账启用（账单追账 · 不带成本第一步）](./2026-11-17-sku-match-revival.md) | confirmed（Q1–Q8 全部确认；开发未开始） |
| 2026-11-16 | [全流程 workflow 与产物目录状态机（年月/账单整理/sku匹配）](./2026-11-16-sku-workflow-and-output-layout.md) | confirmed（目录改版+10月已实施；SKU/成本/回传/氚云口径确认、实现暂缓） |
| 2026-11-16 | [内置规则剥离为默认可编辑规则](./2026-11-16-builtin-rules-extraction.md) | implemented |
| 2026-11-16 | [总览页清空历史控件 + 细粒度清理工具](./2026-11-16-clear-history-ui-and-scoped-tool.md) | implemented |
| 2026-11-16 | [zip 直入 + 输出目录新命名 + 特殊平台口径](./2026-11-16-zip-input-and-dir-naming.md) | implemented |
| 2026-11-16 | [第二批 7 平台对接](./2026-11-16-batch2-platforms.md) | implemented |
| 2026-11-14 | [方案改版：数据库只存店铺映射，结果落文件](./2026-11-14-file-based-results-rearchitecture.md) | implemented |

## 约定

- 重大口径变更（金额公式/分类链/对平门）**先与用户确认**再定稿；
- 被替代的 spec 不删除，status 改 superseded-by 保留决策轨迹；
- 每份 spec 的「验收」必须可执行（回归脚本 + ALL_OK 判据）。
