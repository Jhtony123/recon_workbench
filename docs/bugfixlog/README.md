# Bug Fix Log — recon_workbench

> 机器索引：[`index.json`](./index.json) · Code Map：`task_lookup.bugfixlog`
> 参照 AI_RPA `docs/bugfixlog` 规范。

每次**已落地的 bug 修复**在此登记一条记录，便于回归定位与后续审计。

## 目录约定

按**修复日期**分文件夹：

```
docs/bugfixlog/
  README.md
  index.json
  YYYY-MM-DD/
    <kebab-short-name>.md      # 文件名不含日期前缀；id 带日期保证全局唯一
```

## 条目模板（必填字段）

```markdown
# <一句话标题>

| Field | Value |
|-------|-------|
| id | `YYYY-MM-DD-<kebab-short-name>` |
| date | YYYY-MM-DD |
| status | fixed |
| severity | high / medium / low |
| area | `<task_lookup key>` |

## Symptom      现象（含实证数据/报错原文）
## Root cause   根因
## Fix          修法
## Files        改动文件列表
## Tests        验证方式（基准回归/实测命令）
## Regression guard  回归护栏（什么不许再发生）
```

## 何时写入

- 完成一次**可验证**的 bug 修复后（含小尾巴）当天登记
- 不为未合入的试探、纯重构、文档笔误建条目
- 同一根因的多文件修复 → 一条记录，文件列全
- 登记后同步 `index.json` entries

## 本项目高频坑区（排查先查这里）

| 区域 | 典型坑 |
|------|--------|
| 平台 csv 读取 | 行尾多空字段→索引错位（有赞）；StringDtype≠object（好衣库）；NaN .map(str.strip) TypeError（快手） |
| 金额口径 | 舍入顺序（唯品会先 round 再除税）；链序挪动→镜像错分类（淘系/京东） |
| 规则通道 | biz_code 整表接管；drop_tail 混入 pandas 参数 TypeError |
| zip 入口 | 内层目录名非法字符（冒号）WinError 123；zip+解压副本双份计账 |
| 回归 | 比对列漏项→假绿（毛利率列教训，见 2026-11-14 前史） |
