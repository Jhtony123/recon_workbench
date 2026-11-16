"""淘系（支付宝账单 + 微信支付账单）适配器。

对应原 bill_arr.zfb_complex_arr（约 558 行）+ wechatpay_arr（约 380 行），
以及 look_sku.bill_concate 把两者 concat 的逻辑。

⚠️ 2026-11-14 全量回归（30 家）后的重大修正：
支付宝与微信在原脚本里是**两条完全独立的分类链**，此前插件把两者合并成一条，
导致 TM001/TM003/TM006/TBQY001/TB002 五家出现「精确镜像」的重分类差异：

  · 两份 id_dict 不同：支付宝含 0000000/0010001-4/0160001/0020001-2/0020011/
    0640003/0030003，微信没有（微信靠「入帐类型」type_dict 兜）
  · 两条备注关键字链不同：
      - 「软件服务费」三分支在两条链里都排在「品牌新享」**之后**；插件曾提到链首
        → TM001 品牌新享 -839590 分 / 软件服务费 +839590 分（精确镜像）
      - 支付宝链没有「淘宝新客礼金技术服务费」「消费券价格管控补贴」等微信专有关键字
      - 支付宝链有「太保财险→财产保险」「天猫保证金履约险_退保保险费→天猫保证金」，
        微信链没有
      - 「投诉理赔」支付宝侧还要求备注含「客户服务中心投诉处理组」，微信侧不要求
  · 支付宝链独有后处理（插件此前全缺）：
      - bustype_split：支付服务费 → 取备注 '[' 前缀（基准「淘宝新客礼金技术服务费」
        72806/939163 分正是这么来的）
      - bustype_split：热浪引擎第三方服务商服务费 → 取备注 '(' 前缀
      - fright_type：运费险 → 取备注 '-' 与 '收' 之间的子串
  · 触发条件不同：支付宝按**业务编号**是否为空判定，微信按**业务类别**是否为空判定
  · get_type_from_other 仅支付宝链有

金额口径不变：支付宝收入金额（+元）为收款，支出金额（-元）取绝对值为退款；
微信收入金额(元)/支出金额(元) 同理。
"""
from __future__ import annotations

from typing import Callable, Sequence

import pandas as pd

from ..config import BIZ_GOODS
from ..errors import BillParseError
from .base import (
    STD_BIZ_CATEGORY,
    STD_FUND_DATE,
    STD_ORDER_NO,
    STD_RECEIPT,
    STD_REFUND,
    BillAdapter,
)

# 支付宝账单必需列（原 zfb_complex_arr 的 usecols）
ALIPAY_COLUMNS = (
    "商户订单号", "发生时间", "对方账号", "商品名称",
    "收入金额（+元）", "支出金额（-元）",
    "业务类型", "备注", "业务基础订单号", "业务描述",
)

# 微信支付账单必需列（原 wechatpay_arr 的 required_columns）
WECHAT_COLUMNS = (
    "入帐时间", "主订单id", "支付流水号", "入帐类型",
    "备注", "收入金额(元)", "支出金额(元)", "业务描述",
)

# 无分类占位（原脚本 '0000000|无分类'）
NO_CATEGORY = "无分类"
UNMATCHED = "未匹配"
OTHER = "其他"


def split_first_token(text) -> str:
    """按 | 或空格分割取第一段（原 split_by_space_or_pipe，两个平台共用）。"""
    if text is None:
        return ""
    try:
        if pd.isna(text):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(text).strip()
    if not s:
        return ""
    if "|" in s:
        return s.split("|")[0].strip()
    if " " in s:
        return s.split(" ")[0].strip()
    return s


# ---------------------------------------------------------------------------
# 业务编码字典（两份，原脚本各自独立；管理页规则表可整体覆盖）
# ---------------------------------------------------------------------------

#: 支付宝 id_dict（bill_arr.py 117-206）
ALIPAY_ID_DICT: dict[str, str] = {
    "0000000": "无分类",
    "0010001": "货款", "0010002": "货款", "0010003": "货款", "0010004": "货款",
    "0160001": "货款",
    "0170120T": "淘宝大促软件技术服务费",
    "0170039": "急速回款服务费",
    "0020001": "货款", "0020002": "货款", "0020011": "货款",
    "0030001": "代扣返点积分",
    "0030002": "软件服务费",
    "0030003": "天猫佣金",
    "0030006": "阿里健康天猫医药馆境内实时划扣软件服务费",
    "0030007": "聚划算佣金",
    "0030018": "代扣返点积分",
    "0030038": "支付服务费",
    "0030042": "品牌新享",
    "0030064": "软件服务费",
    "0030088": "百亿补贴软件服务费",
    "0030129": "淘宝内容推广服务费",
    "0030133": "直播软件服务费",
    "0030130": "基础软件服务费",
    "0400007": "代贴条码费",
    "0400014": "其他增值服务费",
    "0400025": "货物装卸费",
    "0400096": "防护包装费",
    "0400177": "组套费",
    "0400178": "拆套费",
    "0470003": "正向配送费",
    "0470009": "逆向配送费",
    "0470056": "配送运输费", "0470061": "配送运输费",
    "0470164": "配送运输费", "0470325": "配送运输费",
    "0480004": "退货入仓费",
    "0480009": "仓内操作服务费", "0480005": "仓内操作服务费",
    "0480200": "仓内操作服务费", "0480571": "仓内操作服务费",
    "0050005": "卖家版运费险",
    "0050006": "运费险",
    "0050026": "交易保障险",
    "0530026": "经济优惠正向配送费",
    "0540001": "包装物料费",
    "0560001": "仓储费", "0560011": "仓储费", "0560016": "仓储费",
    "0640003": "货款",
    "0060011": "淘宝客佣金",
    "0060032": "百亿补贴推广服务费", "0060053": "百亿补贴推广服务费",
    "0060064": "U先直播A78推广服务费",
    "0070001": "淘宝消费者保证金",
    "0070002": "天猫保证金",
    "0070023": "天猫佣金",
    "0110001": "公益捐赠",
    "0170038": "软件服务费",
    "0170040": "淘宝内容推广服务费",
    "008000200001": "天猫保证金", "008002800002": "天猫保证金",
    "008000200003": "天猫保证金", "008002800010": "天猫保证金",
    "008002800014": "天猫保证金", "008002800015": "天猫保证金",
    "008002800018": "天猫保证金", "008002800006": "天猫保证金",
    "008002800001": "天猫保证金", "008002800007": "天猫保证金",
    "0080084T00001T": "天猫保证金",
    "003003800001": "花呗支付服务费",
    "003000700002": "软件服务费",
    "记账本转账": "小额打款",
    "0240004T": "天猫佣金",
    "0530288T": "大服饰跨境服务增值费",
    "0170044T": "官方物流增值服务费",
    "0170122T": "大服饰全球包邮跨境服务基础费",
    "0170125T": "消费者体验提升计划服务费",
    "0530282T": "88VIP淘宝消费券技术服务费",
    "软件服务费-品牌直播大场营销软件服务费": "软件服务费",
    "0060083TI营销支出-百亿补贴推广服务费（日常直播&大牌补贴日）": "百亿补贴推广服务费",
    "0030178T": "淘宝服饰软件服务费",
    "0060092T": "消费券代付资金",
    "0530294T": "限时红包服务费",
    "0530299T": "跨境服务基础费",
    "0030162T": "软件服务费",
    "交易退款-售中退款": "货款",
}

#: 微信 id_dict（bill_arr.py 1953-2031）—— 注意**不含**货款类编码
WECHAT_ID_DICT: dict[str, str] = {
    "0170120T": "淘宝大促软件技术服务费",
    "0170039": "急速回款服务费",
    "0030001": "代扣返点积分",
    "0030002": "软件服务费",
    "0030006": "阿里健康天猫医药馆境内实时划扣软件服务费",
    "0030007": "聚划算佣金",
    "0030018": "代扣返点积分",
    "0030038": "支付服务费",
    "0030042": "品牌新享",
    "0030064": "软件服务费",
    "0030088": "百亿补贴软件服务费",
    "0030129": "淘宝内容推广服务费",
    "0030133": "直播软件服务费",
    "0030130": "基础软件服务费",
    "0400007": "代贴条码费",
    "0400014": "其他增值服务费",
    "0400025": "货物装卸费",
    "0400096": "防护包装费",
    "0400177": "组套费",
    "0400178": "拆套费",
    "0470003": "正向配送费",
    "0470009": "逆向配送费",
    "0470056": "配送运输费", "0470061": "配送运输费",
    "0470164": "配送运输费", "0470325": "配送运输费",
    "0480004": "退货入仓费",
    "0480009": "仓内操作服务费", "0480005": "仓内操作服务费",
    "0480200": "仓内操作服务费", "0480571": "仓内操作服务费",
    "0050005": "卖家版运费险",
    "0050006": "运费险",
    "0050026": "交易保障险",
    "0530026": "经济优惠正向配送费",
    "0540001": "包装物料费",
    "0560001": "仓储费", "0560011": "仓储费", "0560016": "仓储费",
    "0060011": "淘宝客佣金",
    "0060032": "百亿补贴推广服务费", "0060053": "百亿补贴推广服务费",
    "0060064": "U先直播A78推广服务费",
    "0070001": "淘宝消费者保证金",
    "0070002": "天猫保证金",
    "0070023": "天猫佣金",
    "0110001": "公益捐赠",
    "0170038": "软件服务费",
    "0170040": "淘宝内容推广服务费",
    "008000200001": "天猫保证金", "008002800002": "天猫保证金",
    "008000200003": "天猫保证金", "008002800010": "天猫保证金",
    "008002800014": "天猫保证金", "008002800015": "天猫保证金",
    "008002800018": "天猫保证金", "008002800006": "天猫保证金",
    "008002800001": "天猫保证金", "008002800007": "天猫保证金",
    "0080084T00001T": "天猫保证金",
    "003003800001": "花呗支付服务费",
    "003000700002": "软件服务费",
    "记账本转账": "小额打款",
    "0240004T": "天猫佣金",
    "0530288T": "大服饰跨境服务增值费",
    "0170044T": "官方物流增值服务费",
    "0170122T": "大服饰全球包邮跨境服务基础费",
    "0170125T": "消费者体验提升计划服务费",
    "0530282T": "88VIP淘宝消费券技术服务费",
    "软件服务费-品牌直播大场营销软件服务费": "软件服务费",
    "0060083TI营销支出-百亿补贴推广服务费（日常直播&大牌补贴日）": "百亿补贴推广服务费",
    "0030178T": "淘宝服饰软件服务费",
    "0060092T": "消费券代付资金",
    "0530294T": "限时红包服务费",
    "0530299T": "跨境服务基础费",
    "0030162T": "软件服务费",
    "交易退款-售中退款": "货款",
}

#: 微信支付：入帐类型 → 业务类别（原 wechatpay_arr 的 type_dict）
WECHAT_TYPE_DICT: dict[str, str] = {
    "交易收款": "货款",
    "交易退款(售后)": "货款",
    "提现": "提现",
    "充值": "充值",
    "提现失败退款": "提现",
    "账户间转账": "资金调拨",
    "扣除交易手续费": "账扣",
    "分账": "佣金",
    "企业付款": "售后快赔",
}


# ---------------------------------------------------------------------------
# 备注关键字链（顺序敏感，两条链内容不同）
# ---------------------------------------------------------------------------

_KW = "kw"          # 备注含该子串
_KW_ALL = "kw_all"  # 备注同时含多个子串
_SFEE = "sfee"      # 「软件服务费」三分支（与百亿补贴/直播互斥）

_SOFTWARE_FEE = "软件服务费"
_LIVE_SOFTWARE_FEE = "直播软件服务费"
_BAIYI_SOFTWARE_FEE = "百亿补贴软件服务费"


def _apply_software_fee(note: str) -> str | None:
    """原脚本三条并列分支：软件服务费 / 直播软件服务费 / 百亿补贴软件服务费。"""
    if _SOFTWARE_FEE not in note:
        return None
    if "直播" in note:
        return _LIVE_SOFTWARE_FEE
    if "百亿补贴" in note:
        return _BAIYI_SOFTWARE_FEE
    return _SOFTWARE_FEE


#: 支付宝备注链（bill_arr.py 235-318）—— 注意「品牌新享」在「软件服务费」之前
ALIPAY_NOTE_RULES: tuple[tuple, ...] = (
    (_KW, "淘宝内容推广服务费", "淘宝内容推广服务费"),
    (_KW, "代扣款（扣款用途：基金代发任务", BIZ_GOODS),
    (_KW, "淘宝客", "淘宝客佣金"),
    (_KW, "美妆过敏险", "交易保障险"),
    (_KW, "淘宝联盟推广佣金", "淘宝客佣金"),
    (_KW, "品牌新享", "品牌新享"),
    (_KW, "淘宝联盟佣金代扣", "淘宝客佣金"),
    (_KW, "保证金退款", BIZ_GOODS),
    (_KW, "扣款用途：代发_服务赔付", "服务赔付"),
    (_KW, "扣款用途：代发_货值赔付", "货值赔付"),
    (_KW, "扣款用途：代扣_正向配送费", "正向配送费"),
    (_SFEE, None, None),
    (_KW, "商家权益红包", "商家权益红包"),
    (_KW, "技术年费", "技术年费"),
    (_KW, "保险承保", "基础消保保证金险"),
    (_KW, "保险理赔-", "保险赔款"),
    (_KW, "本地退赔款", "保险赔款"),
    (_KW, "代扣款（扣款用途：商家集运中转操作费", "中转操作费"),
    (_KW, "代扣款（扣款用途：商家集运物流服务费", "物流服务费"),
    (_KW, "佣金返还激励", "佣金激励"),
    (_KW, "佣金激励", "佣金激励"),
    (_KW, "代扣款（扣款用途：先用后付618返佣", "佣金激励"),
    (_KW, "交易还款-极速回款-售中退款", BIZ_GOODS),
    (_KW, "代扣款（扣款用途：88VIP淘宝消费券技术服务费", "88VIP淘宝消费券技术服务费"),
    (_KW, "KA激励项目，付款方：浙江天猫技术有限公司", "佣金激励"),
    (_KW, "_全店，付款方：浙江天猫技术有限公司", "佣金激励"),
    (_KW, "理赔款", "理赔款"),
    (_KW, "天猫APP专享折扣服务费", "折扣服务费"),
    (_KW, "先用后付技术服务费-调账", "先用后付技术服务费"),
    (_KW_ALL, ("代扣款（扣款用途：", "超过消费券封顶金额的平台出资合作费用"), "平台出资合作费用"),
    (_KW_ALL, ("代扣款（扣款用途：", "售后退款追缴消费券返还金额"), "消费券代付资金"),
    (_KW, "天猫保证金履约险追偿", "交易保障险"),
    (_KW_ALL, ("投诉理赔", "客户服务中心投诉处理组"), "理赔款"),
    (_KW, "太保财险", "财产保险"),
    (_KW, "天猫保证金履约险_退保保险费", "天猫保证金"),
    (_KW, "理赔追偿", "理赔款"),
    (_KW, "赔付追偿", "理赔款"),
)

#: 微信备注链（bill_arr.py 2055-2144）—— 含支付宝链没有的多个关键字
WECHAT_NOTE_RULES: tuple[tuple, ...] = (
    (_KW, "淘宝内容推广服务费", "淘宝内容推广服务费"),
    (_KW, "代扣款（扣款用途：基金代发任务", BIZ_GOODS),
    (_KW, "淘宝客", "淘宝客佣金"),
    (_KW, "美妆过敏险", "交易保障险"),
    (_KW, "淘宝联盟推广佣金", "淘宝客佣金"),
    (_KW, "品牌新享", "品牌新享"),
    (_KW, "淘宝联盟佣金代扣", "淘宝客佣金"),
    (_KW, "保证金退款", BIZ_GOODS),
    (_KW, "扣款用途：代发_服务赔付", "服务赔付"),
    (_KW, "扣款用途：代发_货值赔付", "货值赔付"),
    (_KW, "扣款用途：代扣_正向配送费", "正向配送费"),
    (_SFEE, None, None),
    (_KW, "商家权益红包", "商家权益红包"),
    (_KW, "技术年费", "技术年费"),
    (_KW, "保险承保", "基础消保保证金险"),
    (_KW, "保险理赔-", "保险赔款"),
    (_KW, "本地退赔款", "保险赔款"),
    (_KW, "保证金管控资金使用", BIZ_GOODS),
    (_KW, "淘宝新客礼金技术服务费", "技术服务费"),
    (_KW, "代扣款（扣款用途：商家集运中转操作费", "中转操作费"),
    (_KW, "代扣款（扣款用途：商家集运物流服务费", "物流服务费"),
    (_KW, "佣金返还激励", "佣金激励"),
    (_KW, "佣金激励", "佣金激励"),
    (_KW, "代扣款（扣款用途：先用后付618返佣", "佣金激励"),
    (_KW, "交易还款-极速回款-售中退款", BIZ_GOODS),
    (_KW, "百亿补贴定向营销费用", "百亿补贴定向营销费用"),
    (_KW, "代扣款（扣款用途：88VIP淘宝消费券技术服务费", "88VIP淘宝消费券技术服务费"),
    (_KW, "KA激励项目，付款方：浙江天猫技术有限公司", "佣金激励"),
    (_KW, "_全店，付款方：浙江天猫技术有限公司", "佣金激励"),
    (_KW, "理赔款", "理赔款"),
    (_KW, "天猫APP专享折扣服务费", "折扣服务费"),
    (_KW, "先用后付技术服务费-调账", "先用后付技术服务费"),
    (_KW_ALL, ("代扣款（扣款用途：", "超过消费券封顶金额的平台出资合作费用"), "平台出资合作费用"),
    (_KW_ALL, ("代扣款（扣款用途：", "售后退款追缴消费券返还金额"), "消费券代付资金"),
    (_KW, "天猫保证金履约险追偿", "交易保障险"),
    (_KW, "投诉理赔", "理赔款"),
    (_KW, "淘宝新品礼金技术服务费", "淘宝新品礼金技术服务费"),
    (_KW, "理赔追偿", "理赔款"),
    (_KW, "赔付追偿", "理赔款"),
    (_KW, "消费券价格管控补贴", "消费券价格管控补贴"),
    (_KW, "万相台无界版聚合充值", "万相台充值"),
)

NOTE_FALLBACK = OTHER

# 业务编号视为「空」的取值（原支付宝链 get_type_from_note 的判定）
_BLANK_IDS = ("", "nan", NO_CATEGORY, "None")


def _run_note_chain(note: str, rules: Sequence[tuple]) -> str:
    """按顺序跑备注关键字链，全不命中则返回「其他」。"""
    s = str(note or "")
    for kind, key, value in rules:
        if kind == _SFEE:
            hit = _apply_software_fee(s)
            if hit:
                return hit
        elif kind == _KW:
            if str(key) in s:
                return str(value)
        elif kind == _KW_ALL:
            if all(str(k) in s for k in key):
                return str(value)
    return NOTE_FALLBACK


def alipay_note_category(note: str) -> str:
    return _run_note_chain(note, ALIPAY_NOTE_RULES)


def wechat_note_category(note: str) -> str:
    return _run_note_chain(note, WECHAT_NOTE_RULES)


def deposit_split(category: str, note: str) -> str:
    """天猫保证金按备注首个「-」前缀细分（原 deposit_split，两源共用）。"""
    cat = str(category or "")
    if cat != "天猫保证金":
        return cat
    s = str(note or "")
    if "保证金退款" in s:
        return BIZ_GOODS
    idx = s.find("-")
    if idx != -1:
        return s[:idx].strip()
    # 原脚本：无「-」时，备注 ≥31 字符返回原类型，否则返回备注本身（空则「未知」）
    cleaned = s.strip()
    if len(cleaned) >= 31:
        return cat
    return cleaned if cleaned else "未知"


def pay_service_split(category: str, note: str) -> str:
    """支付服务费 → 取备注 '[' 前缀（原支付宝链 bustype_split）。

    2026-11-14 回归实证：TBQY001 基准「淘宝新客礼金技术服务费」72806/939163 分
    正是由本步产生；插件此前缺失该步，金额被错误归入「技术服务费」（精确镜像差异）。
    """
    if str(category or "") != "支付服务费":
        return category
    s = str(note or "")
    idx = s.find("[")
    if idx == -1:
        # 原脚本此处会抛 UnboundLocalError（result 未定义）；插件保守返回原类别
        return category
    return s[:idx]


def hotwave_split(category: str, note: str) -> str:
    """热浪引擎第三方服务商服务费 → 取备注 '(' 前缀（原支付宝链第二个 bustype_split）。"""
    if str(category or "") != "热浪引擎第三方服务商服务费":
        return category
    s = str(note or "")
    idx = s.find("(")
    return s[:idx] if idx != -1 else s


def fright_type(category: str, note: str) -> str:
    """运费险 → 取备注 '-' 与 '收' 之间子串（原支付宝链 fright_type）。"""
    if str(category or "") != "运费险":
        return category
    s = str(note or "")
    i_start = s.find("-")
    i_end = s.find("收")
    if i_start == -1 or i_end == -1:
        # 原脚本此处同样会 UnboundLocalError；插件保守返回原类别
        return category
    return s[i_start + 1:i_end - 2]


def type_from_other(category: str, biz_type: str, note: str, trade_account: str) -> str:
    """「其他」类目按业务类型/备注/对方账号二次判定（原 get_type_from_other，仅支付宝链）。"""
    cat = str(category or "")
    if cat != OTHER:
        return cat
    bt = str(biz_type or "")
    s = str(note or "")
    acc = str(trade_account or "")

    if ("红包" in s) and ("提现" not in bt):
        return "企业红包"
    if ("红包" not in s) and ("提现" in bt):
        return "提现"
    if "结息" in bt:
        return "结息"
    for marker in (")扣款", ")退款"):
        if marker in s:
            idx = s.find("(")
            if idx != -1:
                return s[:idx]
            return cat
    if "*" in acc:
        return "第三方转账"
    if "充值" in bt:
        return "充值"
    return cat


def _map_chain(values: list, notes: list[str], fn: Callable[[str, str], str]) -> list:
    return [fn(str(v), str(n)) for v, n in zip(values, notes)]


class TaobaoAdapter(BillAdapter):
    """淘系店铺：支付宝账单为主，微信支付账单为可选附加源。"""

    platform = "taobao"
    display_name = "淘系店铺"
    bill_type = "淘系店铺"
    file_patterns = (".csv",)
    default_read_options = {
        "encoding": "gb18030",
        "skiprows": 4,
        "dtype": {
            "账务流水号": "str",
            "业务描述": "str",
            "业务基础订单号": "str",
            "收入金额（+元）": "str",
            "支出金额（-元）": "str",
        },
    }
    needs_extra_files = ("wechat",)

    # ---------------- 支付宝

    def _read_alipay(self, ctx) -> pd.DataFrame:
        files = self.collect_files(ctx)
        if not files:
            raise BillParseError(
                "未找到支付宝账单 csv",
                detail={"searched": list(ctx.file_paths or [])},
            )
        opts = self.read_options()
        frames = []
        for f in files:
            df = pd.read_csv(f, index_col=None, **opts)
            self.require_columns(df, list(ALIPAY_COLUMNS), self.display_name)
            # 原脚本无条件去掉尾 4 行汇总
            drop_tail = int(self.rule("read_option", "drop_tail", "4") or 4)
            if drop_tail > 0 and len(df) > drop_tail:
                df = df.drop(df.tail(drop_tail).index)
            df = df.copy()
            df["__source_file__"] = f
            df["__source_row__"] = range(len(df))
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _alipay_id_dict(self) -> dict[str, str]:
        """规则表 biz_code 优先（管理页可维护），否则用原脚本支付宝字典。"""
        return self.biz_code_map() or ALIPAY_ID_DICT

    def _wechat_id_dict(self) -> dict[str, str]:
        """微信副字典走 biz_code 命名空间 wx_id:（与支付宝主字典隔离）。"""
        return self.prefixed_map("wx_id:") or WECHAT_ID_DICT

    def _wechat_type_dict(self) -> dict[str, str]:
        return self.prefixed_map("wx_type:") or WECHAT_TYPE_DICT

    @classmethod
    def builtin_rules(cls) -> dict[str, dict[str, str]]:
        out = super().builtin_rules()
        biz = dict(ALIPAY_ID_DICT)
        biz.update({f"wx_id:{k}": v for k, v in WECHAT_ID_DICT.items()})
        biz.update({f"wx_type:{k}": v for k, v in WECHAT_TYPE_DICT.items()})
        out["biz_code"] = biz
        out.setdefault("read_option", {})["drop_tail"] = "4"
        return out

    def _clean_alipay(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        for col in ("对方账号", "商户订单号", "业务基础订单号",
                    "收入金额（+元）", "支出金额（-元）", "发生时间"):
            out[col] = out[col].astype("string").fillna("").str.strip()

        out["备注"] = out["备注"].astype("string").fillna("无备注").str.strip()
        out["业务描述"] = out["业务描述"].astype("string").fillna("0000000|无分类").str.strip()
        out["商品名称"] = out["商品名称"].astype("string").fillna("")

        out["收入金额（+元）"] = pd.to_numeric(out["收入金额（+元）"], errors="coerce").fillna(0.0)
        out["支出金额（-元）"] = pd.to_numeric(out["支出金额（-元）"], errors="coerce").fillna(0.0)
        out["资金日期"] = pd.to_datetime(out["发生时间"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")

        out["业务编号"] = out["业务描述"].map(split_first_token)
        out["业务类型"] = out["业务类型"].astype("string").fillna("").str.strip()
        notes = out["备注"].astype("string").fillna("").tolist()

        # ① 业务编号 → id_dict
        mapping = self._alipay_id_dict()
        category = out["业务编号"].map(lambda x: mapping.get(str(x).strip()))

        # ② 0020011 且业务类型含「退票」→ 提现（原 return_special）
        mask = (out["业务编号"] == "0020011") & out["业务类型"].str.contains("退票", na=False)
        category = category.mask(mask, "提现")

        # ③ 货款 + 商品名称含「充值」→ 取「值」字前缀（原 saleReclass）
        goods = category == BIZ_GOODS
        recharge = out["商品名称"].str.contains("充值", na=False)
        target = goods & recharge
        if target.any():
            def _reclass(name: str) -> str:
                idx = str(name).find("值")
                return str(name)[:idx] if idx != -1 else str(name)
            category = category.mask(target, out.loc[target, "商品名称"].map(_reclass))

        # ④ 备注关键字链 —— 触发条件是**业务编号为空**（不是类别为空）
        cat_list = category.astype("object").tolist()
        ids = out["业务编号"].astype("string").fillna("").tolist()
        for i, bus_id in enumerate(ids):
            if bus_id in _BLANK_IDS:
                cat_list[i] = alipay_note_category(notes[i])

        # ⑤ 天猫保证金按备注首个「-」前缀细分
        cat_list = _map_chain(cat_list, notes, deposit_split)
        # ⑥ 支付服务费按 '[' 前缀细分（基准「淘宝新客礼金技术服务费」来源）
        cat_list = _map_chain(cat_list, notes, pay_service_split)
        # ⑦ 热浪引擎第三方服务商服务费按 '(' 前缀细分
        cat_list = _map_chain(cat_list, notes, hotwave_split)
        # ⑧「其他」二次判定（企业红包/提现/结息/…）
        biz_types = out["业务类型"].tolist()
        accounts = out["对方账号"].astype("string").fillna("").tolist()
        cat_list = [
            type_from_other(str(c), str(bt), n, str(acc))
            for c, bt, n, acc in zip(cat_list, biz_types, notes, accounts)
        ]

        category = pd.Series(cat_list, index=out.index, dtype="object")

        # ⑨ T62- 开头且有金额 → 货款（原 Txx）
        receipt = out["收入金额（+元）"]
        refund = out["支出金额（-元）"]
        t62 = pd.Series(notes, index=out.index).str.contains("T62-", regex=False) & (
            (receipt != 0) | (refund != 0)
        )
        category = category.mask(t62, BIZ_GOODS)

        # ⑩ 运费险按备注 '-' 与 '收' 之间取子串（原 fright_type，在最后一步）
        category = pd.Series(
            _map_chain(category.astype("object").tolist(), notes, fright_type),
            index=out.index, dtype="object",
        )

        out[STD_BIZ_CATEGORY] = category.fillna(UNMATCHED).replace("", UNMATCHED)
        out[STD_ORDER_NO] = out["业务基础订单号"]
        out[STD_RECEIPT] = out["收入金额（+元）"]
        # 原脚本：退款 = -支出金额（支出列为负数），这里统一取绝对值
        out[STD_REFUND] = out["支出金额（-元）"].abs()
        return out

    # ---------------- 微信支付（附加源，可缺省）

    def _read_wechat(self, ctx) -> pd.DataFrame:
        # 微信账单是 .xlsx（file_patterns 只有 .csv，collect_files 会漏收）
        import glob as _glob
        import os as _os
        files: list[str] = []
        # 附加源支持原始下载 zip（2026-11-16 用户需求）
        for p in self.expand_source_paths(ctx.extra_files.get("wechat") or []):
            p = str(p).strip().strip('"')
            if _os.path.isdir(p):
                files.extend(_glob.glob(_os.path.join(p, "*.xlsx")))
                files.extend(_glob.glob(_os.path.join(p, "*.xls")))
                files.extend(_glob.glob(_os.path.join(p, "**", "*.xlsx"), recursive=True))
            elif _os.path.isfile(p):
                files.append(p)
        files = [f for f in sorted(set(files)) if not _os.path.basename(f).startswith("~$")]
        if not files:
            return pd.DataFrame()
        frames = []
        for f in files:
            df = pd.read_excel(
                f,
                index_col=None,
                dtype={"支付流水号": "str", "主订单id": "str", "备注": "str", "子订单id": "str"},
            )
            self.require_columns(df, list(WECHAT_COLUMNS), "淘系微信支付")
            df = df.copy()
            df["__source_file__"] = f
            df["__source_row__"] = range(len(df))
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _clean_wechat(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.rename(columns={"主订单id": "淘宝订单编号"}).copy()
        out["入账时间"] = out["入帐时间"].astype("string").fillna("").str.strip().str.replace("`", "", regex=False)
        out["资金日期"] = pd.to_datetime(out["入账时间"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")

        for col in ("淘宝订单编号", "支付流水号", "备注"):
            if col in out.columns:
                out[col] = out[col].astype("string").fillna("").str.strip().str.replace("`", "", regex=False)
        if "备注" not in out.columns:
            out["备注"] = ""
        if "入帐类型" not in out.columns:
            out["入帐类型"] = ""

        for col in ("收入金额(元)", "支出金额(元)"):
            s = out[col].astype("string").fillna("").str.replace(r"[^0-9.\-]", "", regex=True)
            out[col] = pd.to_numeric(s.replace("", "0"), errors="coerce").fillna(0.0)

        # 微信分类三层链（原 wechatpay_arr）：
        #   ① 业务编号 → 微信 id_dict（不含货款类编码）
        #   ② ①未命中 → 入帐类型 type_dict（交易收款/交易退款(售后) → 货款）
        #   ③ ①②合并后类别为空 → 备注关键字链（触发条件是**类别为空**，与支付宝不同）
        out["业务描述"] = out["业务描述"].astype("string").fillna("")
        out["业务编号"] = out["业务描述"].map(split_first_token)
        # 原脚本：业务编号非空才查字典（规则表 wx_id:/wx_type: 可覆盖）
        mapping = self._wechat_id_dict()
        type_mapping = self._wechat_type_dict()
        cat1 = out["业务编号"].map(lambda x: mapping.get(str(x).strip()) if str(x).strip() else None)
        cat2 = out["入帐类型"].astype("string").fillna("").str.strip().map(
            lambda x: type_mapping.get(str(x).strip())
        )
        category = cat1.where(cat1.notna(), cat2)

        notes = out["备注"].astype("string").fillna("").tolist()
        cat_list = category.astype("object").tolist()
        for i, v in enumerate(cat_list):
            sv = "" if v is None else str(v)
            if sv in ("", NO_CATEGORY, "None", "nan") or pd.isna(v):
                cat_list[i] = wechat_note_category(notes[i])

        # 天猫保证金按备注首个「-」前缀细分（微信链也有 deposit_split）
        cat_list = _map_chain(cat_list, notes, deposit_split)

        out[STD_BIZ_CATEGORY] = pd.Series(cat_list, index=out.index, dtype="object").fillna(UNMATCHED)
        out[STD_ORDER_NO] = out["淘宝订单编号"]
        out[STD_RECEIPT] = out["收入金额(元)"]
        out[STD_REFUND] = out["支出金额(元)"].abs()
        return out

    # ---------------- 合并

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError("淘系走 parse()，不使用基类 normalize")

    def parse(self, ctx) -> list:
        std_cols = [STD_ORDER_NO, STD_FUND_DATE, STD_BIZ_CATEGORY, STD_RECEIPT, STD_REFUND,
                    "__source_file__", "__source_row__"]

        ali = self._read_alipay(ctx)
        parts = []
        if not ali.empty:
            parts.append(self._clean_alipay(ali)[std_cols])

        wc = self._read_wechat(ctx)
        if not wc.empty:
            parts.append(self._clean_wechat(wc)[std_cols])

        if not parts:
            return []

        merged = pd.concat(parts, ignore_index=True)
        merged[STD_FUND_DATE] = merged["资金日期"]
        merged = self.filter_by_period(merged, ctx)
        return self.bill_rows(ctx, merged)

    # ---------------- 基准格式出口（原 zfb_complex_arr / wechatpay_arr）

    def _alipay_detail(self, ctx) -> pd.DataFrame:
        """支付宝清洗后全量 df（含 实际业务类型 / 收款 / 退款，元），账期过滤后。"""
        ali = self._read_alipay(ctx)
        if ali.empty:
            return pd.DataFrame()
        out = self._clean_alipay(ali)
        out = out.rename(columns={STD_BIZ_CATEGORY: "实际业务类型"})
        out["发生时间"] = pd.to_datetime(out["资金日期"], errors="coerce")
        if ctx.s_date and ctx.e_date:
            d = out["发生时间"]
            out = out[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        return out.reset_index(drop=True)

    def _wechat_detail(self, ctx) -> pd.DataFrame:
        """微信基准 7 列明细（淘宝订单编号/资金发生日期/业务类别/收款/退款/业务描述/备注）。"""
        wc = self._read_wechat(ctx)
        if wc.empty:
            return pd.DataFrame()
        out = self._clean_wechat(wc)
        if ctx.s_date and ctx.e_date:
            d = pd.to_datetime(out["资金日期"], errors="coerce")
            out = out[(d >= pd.Timestamp(ctx.s_date)) & (d <= pd.Timestamp(ctx.e_date))]
        cols = ["淘宝订单编号", "资金日期", STD_BIZ_CATEGORY, STD_RECEIPT, STD_REFUND, "业务描述", "备注"]
        for c in cols:
            if c not in out.columns:
                out[c] = ""
        detail = out.loc[:, cols].rename(columns={"资金日期": "资金发生日期"}).copy()
        return detail.reset_index(drop=True)

    def export_books(self, ctx) -> list[dict]:
        """淘系两簿（与原脚本一致）：
        · _支付宝_…_汇总结果.xlsx：氚云导入模板 + 汇总结果（8 列透视）
        · _支付宝_…_账单分类明细.xlsx：每个实际业务类型一个 sheet（全列）
        · _微信支付_…_淘系微信账单整理.xlsx（有微信源时）：模板 + 透视 + 7 列明细
        文件名前缀（bill_type=淘宝/天猫）由 pipeline 按店铺映射拼。
        """
        books: list[dict] = []

        ali = self._alipay_detail(ctx)
        if not ali.empty:
            # 原脚本收支合计校验：透视合计必须与原始合计一致，不一致即终止
            re_raw = round(float(ali["收入金额（+元）"].sum()), 2)
            pay_raw = round(float(ali["支出金额（-元）"].sum()), 2)
            pivot = self.pivot_from_detail(
                ali, ctx, "支付宝账单", category_col="实际业务类型")
            if (round(float(pivot["收款"].sum()), 2) != re_raw
                    or round(float(pivot["退款"].sum()), 2) != -pay_raw):
                raise BillParseError(
                    "支付宝账单收支合计校验不一致",
                    detail={"原始收款": re_raw, "原始付款": pay_raw,
                            "透视收款": round(float(pivot["收款"].sum()), 2),
                            "透视退款": round(float(pivot["退款"].sum()), 2),
                            "hint": "原脚本此处 exit()；请检查分类链是否漏判金额"},
                )
            books.append({
                "infix": "_支付宝", "tail": "_汇总结果",
                "sheets": [
                    {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot), "kind": "yuan"},
                    {"name": "汇总结果", "df": pivot, "kind": "yuan"},
                ],
            })
            # 账单分类明细：每个业务类型一个 sheet（原 clean_sheet_name 去重）
            sheets = []
            used: set[str] = set()
            for cat in pivot["业务类别"].tolist():
                name = str(cat)[:31]
                while name in used:
                    name = name[:-1] + "~"
                used.add(name)
                sheets.append({"name": name, "df": ali[ali["实际业务类型"] == cat], "kind": "yuan"})
            if sheets:
                books.append({"infix": "_支付宝", "tail": "_账单分类明细", "sheets": sheets})

        wc = self._wechat_detail(ctx)
        if not wc.empty:
            pivot_w = self.pivot_from_detail(wc, ctx, "微信支付")
            books.append({
                "infix": "_微信支付", "tail": "_淘系微信账单整理",
                "sheets": [
                    {"name": "氚云导入模板", "df": self.chuanyun_from_pivot(pivot_w), "kind": "yuan"},
                    {"name": "微信支付账单透视", "df": pivot_w, "kind": "yuan"},
                    {"name": "微信支付账单", "df": wc, "kind": "yuan"},
                ],
            })
        return books
