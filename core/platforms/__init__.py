"""平台账单适配层（承接 bill_arr.py 4366 行 / 19 个 *_arr 函数）。

设计：每个平台一个薄适配器，只写差异；共性（读文件、清洗、归一、日期过滤、透视）在基类。
列名映射、业务编码字典、金额符号规则一律从 SQLite 规则表读取（可在管理页维护），
不再硬编码在函数体里。
"""
from __future__ import annotations

from .base import BillAdapter, BillRow, ParseContext
from .registry import PLATFORMS, get_adapter, list_platforms

__all__ = [
    "BillAdapter",
    "BillRow",
    "ParseContext",
    "PLATFORMS",
    "get_adapter",
    "list_platforms",
]
