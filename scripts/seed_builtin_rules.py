# -*- coding: utf-8 -*-
"""内置规则剥离种子脚本（2026-11-16 用户需求）。

把各适配器 builtin_rules()（编码字典/读取参数等内置默认）导出到
rules/{platform}.json，作为**默认可编辑规则**（note=builtin-default）。
纪律：
  · 已存在的同 key 条目**不覆盖**（保护用户/AI 后续热修）；只补缺失键；
  · 代码字典保留为兜底：规则文件被删或条目停用时口径回落内置，不会算错；
  · 备注关键字链（NOTE_RULES 等顺序敏感逻辑）不进规则表，仍在代码。
用法: python scripts/seed_builtin_rules.py [--dry]
"""
import sys, os
sys.path.insert(0, r"D:\DesktopAgent\extensions\user\recon_workbench")
sys.path.insert(0, r"D:\DesktopAgent")

from core import rules_store
from core.platforms.registry import PLATFORMS

DRY = "--dry" in sys.argv

def main():
    total_new = 0
    for plat, cls in sorted(PLATFORMS.items()):
        builtin = cls.builtin_rules()
        if not builtin:
            print(f"== {plat}: 无可剥离内置规则（跳过）")
            continue
        existing = rules_store.load_rules_map(plat)
        added = 0
        for kind, kv in sorted(builtin.items()):
            for key, value in sorted(kv.items()):
                if key in (existing.get(kind) or {}):
                    continue  # 不覆盖已有条目
                added += 1
                if not DRY:
                    rules_store.upsert_rule(plat, kind, key, value,
                                            note="builtin-default（代码内置剥离，可编辑；停用后回落代码兜底）")
        total_new += added
        kinds = {k: len(v) for k, v in builtin.items()}
        print(f"== {plat}: builtin={kinds} 新增={added}{'（dry）' if DRY else ''}")
    print(f"TOTAL_NEW={total_new}")
    print("platforms_with_rules:", rules_store.list_rule_platforms())
    print("active_rules:", rules_store.count_rules())

if __name__ == "__main__":
    main()
