"""业务异常。

原影刀脚本在 3 处用 `exit()` 直接终止解释器（look_sku.py 254/1209/1222），
在插件 Job 里 exit 会绕过异常处理，表现为「跑完没结果也没报错」。
这里统一改为抛业务异常，由 run.py / actions.py 捕获并写入 job_log + 管理页状态。
"""
from __future__ import annotations


class ReconError(Exception):
    """对账引擎基类异常。"""

    code: str = "recon_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ConfigError(ReconError):
    """凭据 / 数据源配置缺失。"""

    code = "config_error"


class DataSourceEmpty(ReconError):
    """数据源无数据。

    注意：新店首月、账期无发货都属于**正常业务场景**，不应当作系统失败。
    调用方应把 status 标为 skipped 并给出可读原因。
    """

    code = "data_source_empty"


class BillParseError(ReconError):
    """平台账单解析失败（列缺失、格式不符、编码错误）。"""

    code = "bill_parse_error"


class AllocationMismatch(ReconError):
    """货款分摊后合计与账单合计不一致。

    原脚本在此处 `exit()`；财务口径上这是必须暴露的硬差异。
    """

    code = "allocation_mismatch"


class ReconAbort(ReconError):
    """对账流程主动中止（带可读原因）。"""

    code = "recon_abort"


class WritebackError(ReconError):
    """回传数据库失败。

    单行失败不抛，聚合后由 writeback 汇总；整体不可用（连不上库）才抛。
    """

    code = "writeback_error"
