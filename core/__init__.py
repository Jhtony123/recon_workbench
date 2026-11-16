"""recon_workbench core 引擎包。

分层（对应原影刀脚本三层）：
  platforms/  ← bill_arr.py：各平台账单整理规则与数据源读取
  matching.py / allocation.py  ← look_sku.py：SKU 出入库匹配、货款分摊
  cost.py / pivots.py          ← cost_match.py：成本取数计算与透视
  writeback.py                 ← look_sku.update_sql：结果回传数据库标记

地基：
  config.py  凭据与路径（ADB 凭据走环境变量，不入代码/config.json）
  money.py   金额口径（Decimal ↔ 分 INTEGER）
  errors.py  业务异常（替代原脚本的 exit()）
  db.py      巨益 ADB 连接与查询
  repo.py    本机 SQLite 持久化
"""

__all__ = ["config", "money", "errors", "db", "repo"]
