# 内置规则剥离为默认可编辑规则

| Field | Value |
|-------|-------|
| date | 2026-11-16 |
| status | implemented |
| scope | base.builtin_rules / 各适配器 / rules_store / rules/*.json |

## 背景与问题

「平台解析规则」页显示空（rules/ 无 JSON），内置字典藏在代码里不可见不可编辑；
用户要求剥离为默认可编辑规则。

## 决策

1. 各适配器新增 `builtin_rules()` 类方法返回 {rule_kind: {key: value}}；
   种子脚本（工作区 scripts/seed_builtin_rules.py）导出到 rules/{platform}.json，
   note=builtin-default；**幂等：只补缺失键，不覆盖已改条目**；
2. **代码字典保留为兜底**：规则文件被删/整类停用时回落内置口径；
3. 副字典走 biz_code 命名空间前缀（不进主映射，prefixed_map 取）：
   淘系微信 `wx_id:`/`wx_type:`、唯品会品牌 `brand:`、爱库存店铺 `shop:`
   （值=名称|编码|品牌）；
4. **备注关键字链不进规则表**（顺序即口径，改序必回归，留代码）；
5. read_option 值支持 JSON（dtype 字典）；drop_tail 等清洗参数由
   _NON_PANDAS_READ_KEYS 过滤不进 pandas；
6. 使用注意：biz_code 整表接管——排除某编码应改值为「未匹配」而非停用。

## 实现要点

- base：biz_code_map 过滤带冒号键；prefixed_map；read_options JSON 还原；
- taobao/_wechat_id_dict、vip/_brand_dict（detail_frame 每次取一次 self._bd）、
  akc/_shop_by_id 接规则通道；
- 种子结果：10 平台 259 条（有赞「提现\t」键 strip 归并到「提现」，值相同无害；
  小红书无可剥离项）。

## 验收

规则接管后全量回归 ALL_OK（第一批 4+第二批 12+vip 三旬）；query_rules 实测
youzan 14 条可见；热重载+UI 探活零报错。

## 遗留与后续

- 修复缺陷见 bugfixlog/2026-11-16/drop-tail-non-pandas-read-key.md；
- 方法论 §3 已改写（不再需要「首次热修先全量导入」）。
