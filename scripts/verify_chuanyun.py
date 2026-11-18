# -*- coding: utf-8 -*-
"""氚云对接模块验证（2026-11-18）。

纪律：本脚本**绝不向氚云发送任何请求**——
  · dry-run 路径：monkeypatch requests.post / fetch_master_data 为「调用即失败」，证明零 HTTP；
  · 真实路径：FakeResp 捕获 LoadBizObjects / CreateBizObject，逐字段核对 payload
    与影刀 api_chuanyun.file_arrange 口径一致（主表 F 字段 + 子表 26 字段 + IsSubmit）。
  · run_chuanyun 端到端：临时 results 根 + 伪造账单批次 Excel（含氚云导入模板 sheet），
    验证 dry-run 预览落盘不移动 / 真实推送成功移入 氚云导入成功/ + manifest 更新 / 失败留守。

用法：python scripts/verify_chuanyun.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, r"D:\DesktopAgent")

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from core import artifacts, chuanyun as cy, joblog, pipeline  # noqa: E402
from core.config import H3_SCHEMA_BILL, H3_SCHEMA_BILL_DETAIL  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[OK] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


# ---------------------------------------------------------------- 主数据 fixture（与影刀字段口径一致）

def fixture_master():
    return {
        "shop": pd.DataFrame([{
            "ObjectId": "shop-obj-1", "平台": "抖音", "店铺名称": "测试店",
            "注册公司": "测试公司", "决算部门": "抖音_测试店", "品牌": "QS", "状态": "启用"}]),
        "expense": pd.DataFrame([{
            "ObjectId": "exp-1", "账单科目名称": "货款", "关联科目": "mg-1",
            "金蝶科目编号": "1405", "核算类型": "存货", "税率": 0.13}]),
        "manage_exp": pd.DataFrame([{
            "ObjectId": "mg-1", "分类科目名称": "主营业务成本", "预算科目名称": "货款预算",
            "科目名称": "库存商品", "科目性质": "资产", "科目预控": "是",
            "是否入决算": "是", "状态": "启用"}]),
        "money_acct": pd.DataFrame([
            {"ObjectId": "acct-1", "账户性质": "第三方资金平台", "注册公司": "测试公司",
             "开户机构全称": "抖音_测试店支付宝", "账号": "123456", "状态": "启用",
             "提现账户obj": "wd-1"},
            {"ObjectId": "acct-2", "账户性质": "第三方资金平台", "注册公司": "测试公司",
             "开户机构全称": "抖音_测试店微信", "账号": "wx-9", "状态": "启用",
             "提现账户obj": "wd-2"},
            {"ObjectId": "acct-3", "账户性质": "第三方资金平台", "注册公司": "测试公司",
             "开户机构全称": "爱库存结算户", "账号": "akc-1", "状态": "启用",
             "提现账户obj": "wd-3"}]),
        "oms_dept": pd.DataFrame([{
            "ObjectId": "oms-1", "决算部门": "抖音_测试店", "部门预算管控": "是",
            "巨益编号": "DY001", "品牌": "QS", "状态": "启用"}]),
    }


TPL = pd.DataFrame([
    {"店铺名称": "抖音_测试店", "业务类别": "货款", "资金方向": "收款", "金额": 1000.0},
    {"店铺名称": "抖音_测试店", "业务类别": "货款", "资金方向": "付款", "金额": 130.0},
])

# ---------------------------------------------------------------- 单元：日期/模板/payload

check("月末日期 YYYY-MM", cy.get_month_end_date("2026-10") == "2026-10-31")
check("月末日期 YYYY-MM-DD", cy.get_month_end_date("2026-02-05") == "2026-02-28")
check("月末日期 闰年2月", cy.get_month_end_date("2024-02") == "2024-02-29")

params, warns, stats = cy.build_payload(TPL, fixture_master(), period="2026-10",
                                        file_name="抖音_测试店_DY001_账单整理.xlsx",
                                        account_balance=500.0)
check("payload 生成成功", params is not None and not warns, str(warns))
check("ActionName/SchemaCode/IsSubmit",
      params["ActionName"] == "CreateBizObject" and params["SchemaCode"] == H3_SCHEMA_BILL
      and params["IsSubmit"] == "true")
biz = json.loads(params["BizObject"])
check("主表 数据来源/预算模式/金蝶状态",
      biz["F0000001"] == "BIL-店铺资金账单" and biz["F0000034"] == "预算管控模式"
      and biz["F0000035"] == "年度_月度_科目_部门" and biz["F0000062"] == "未生成")
check("主表 平台/公司/账期/结算日",
      biz["F0000078"] == "抖音" and biz["F0000002"] == "测试公司"
      and biz["F0000003"] == "2026-10" and biz["F0000076"] == "2026-10-31")
check("主表 资金账户四件套",
      biz["F0000021"] == "acct-1" and biz["F0000024"] == "抖音_测试店支付宝"
      and biz["F0000025"] == "123456" and biz["F0000066"] == "wd-1")
check("主表 金额合计（收1000/付130/净870/余1370）",
      biz["F0000016"] == 1000.0 and biz["F0000017"] == 130.0
      and biz["F0000020"] == 870.0 and biz["F0000057"] == 870.0
      and biz["F0000022"] == 500.0 and biz["F0000023"] == 1370.0, str(stats))
check("主表 录入查重=YYYY-MM+账户obj", biz["F0000026"] == "2026-10acct-1")
check("主表 摘要口径", "2026年10月" in biz["F0000030"] and "收款：1000.0元" in biz["F0000030"]
      and "净现金流：870.0元" in biz["F0000030"], biz["F0000030"])
rows = biz[H3_SCHEMA_BILL_DETAIL]
check("子表 2 行 26 字段", len(rows) == 2 and all(len(r) == 26 for r in rows))
r0, r1 = rows
check("子表 店铺/部门/品牌/巨益编号映射",
      r0["F0000068"] == "shop-obj-1" and r0["F0000069"] == "抖音_测试店"
      and r0["F0000077"] == "测试店" and r0["F0000070"] == "QS" and r0["F0000071"] == "DY001")
check("子表 科目链映射",
      r0["F0000058"] == "exp-1" and r0["F0000010"] == "mg-1"
      and r0["F0000011"] == "主营业务成本" and r0["F0000048"] == "货款预算"
      and r0["F0000012"] == "库存商品" and r0["F0000059"] == "存货"
      and r0["F0000084"] == "货款")
check("子表 收款行 税额拆分（1000/1.13=884.96 税115.04）",
      r0["F0000015"] == 1000.0 and r0["F0000051"] == 884.96
      and r0["F0000053"] == 115.04 and r0["F0000052"] == 0.13)
check("子表 收款=释放/负占用",
      r0["F0000049"] == "释放" and r0["F0000045"] == -1000.0 and r0["F0000067"] == -884.96)
check("子表 付款=占用/正金额（130/1.13=115.04 税14.96）",
      r1["F0000049"] == "占用" and r1["F0000045"] == 130.0
      and r1["F0000051"] == 115.04 and r1["F0000053"] == 14.96 and r1["F0000067"] == 115.04)
check("子表 预算标签=年_月_科目_部门",
      r0["F0000039"] == "2026_10_货款预算_抖音_测试店", r0["F0000039"])
check("子表 预控双列", r0["F0000036"] == "是" and r0["F0000037"] == "是")

# 微信分支：文件名含微信 → 选微信账户且账号置空（原脚本口径）
p_wx, w_wx, _ = cy.build_payload(TPL, fixture_master(), period="2026-10",
                                 file_name="抖音_测试店_微信账单整理.xlsx")
b_wx = json.loads(p_wx["BizObject"])
check("微信分支 账户选择", b_wx["F0000021"] == "acct-2" and b_wx["F0000025"] == "", str(w_wx))
# 爱库存分支
tpl_akc = TPL.copy()
tpl_akc["店铺名称"] = "爱库存_某店"
m_akc = fixture_master()
m_akc["shop"] = pd.concat([m_akc["shop"], pd.DataFrame([{
    "ObjectId": "shop-akc", "平台": "爱库存", "店铺名称": "某店", "注册公司": "测试公司",
    "决算部门": "爱库存_某店", "品牌": "QS", "状态": "启用"}])], ignore_index=True)
m_akc["oms_dept"] = pd.concat([m_akc["oms_dept"], pd.DataFrame([{
    "ObjectId": "oms-akc", "决算部门": "爱库存_某店", "部门预算管控": "否",
    "巨益编号": "AKC01", "品牌": "QS", "状态": "启用"}])], ignore_index=True)
p_akc, w_akc, _ = cy.build_payload(tpl_akc, m_akc, period="2026-10", file_name="爱库存账单整理.xlsx")
check("爱库存分支 账户选择", p_akc is not None
      and json.loads(p_akc["BizObject"])["F0000021"] == "acct-3", str(w_akc))

# 校验门：店铺/业务类别/预算科目/资金账户 未匹配 → None + 原因
m_bad = fixture_master()
m_bad["shop"] = m_bad["shop"].iloc[0:0]
p_bad, w_bad, _ = cy.build_payload(TPL, m_bad, period="2026-10", file_name="x.xlsx")
check("校验门 店铺未匹配跳过", p_bad is None and "店铺名称" in w_bad[0], str(w_bad))
m_bad2 = fixture_master()
m_bad2["expense"] = m_bad2["expense"].iloc[0:0]
p_bad2, w_bad2, _ = cy.build_payload(TPL, m_bad2, period="2026-10", file_name="x.xlsx")
check("校验门 业务类别未匹配跳过", p_bad2 is None and "业务类别" in w_bad2[0])
m_bad3 = fixture_master()
m_bad3["manage_exp"] = m_bad3["manage_exp"].iloc[0:0]
p_bad3, w_bad3, _ = cy.build_payload(TPL, m_bad3, period="2026-10", file_name="x.xlsx")
check("校验门 预算科目空跳过", p_bad3 is None and "预算科目" in w_bad3[0], str(w_bad3))
m_bad4 = fixture_master()
m_bad4["money_acct"] = m_bad4["money_acct"].iloc[0:0]
p_bad4, w_bad4, _ = cy.build_payload(TPL, m_bad4, period="2026-10", file_name="x.xlsx")
check("校验门 资金账户未匹配跳过", p_bad4 is None and "资金账户" in w_bad4[0])

# 无快照骨架（dry-run）：ObjectId 留空、warnings 明示、无 'nan' 字符串
p_skel, w_skel, s_skel = cy.build_payload(TPL, None, period="2026-10", file_name="x.xlsx")
check("无快照骨架生成", p_skel is not None and "无主数据快照" in w_skel[0])
b_skel = json.loads(p_skel["BizObject"])
check("骨架 平台/公司空串非nan", b_skel["F0000078"] == "" and b_skel["F0000002"] == "")
check("骨架 金额合计照算", b_skel["F0000016"] == 1000.0 and b_skel["F0000057"] == 870.0)
check("骨架 税率空→不含税=含税", b_skel[H3_SCHEMA_BILL_DETAIL][0]["F0000051"] == 1000.0)
check("骨架 预算标签无nan", "nan" not in b_skel[H3_SCHEMA_BILL_DETAIL][0]["F0000039"])

# ---------------------------------------------------------------- HTTP 层：FakeResp 捕获（不发真实请求）

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


http_calls = []
_orig_post = requests.post


def fake_post(url, headers=None, json=None, data=None, timeout=None, **kw):
    body = json if json is not None else (
        __import__("json").loads(data) if isinstance(data, (str, bytes)) else {})
    http_calls.append({"url": url, "headers": dict(headers or {}), "body": body})
    action = body.get("ActionName")
    if action == "LoadBizObjects":
        schema = body.get("SchemaCode")
        arr = []
        if schema == cy.H3_SCHEMA_SHOP:
            arr = [{"ObjectId": "shop-obj-1", "F0000010": "抖音", "F0000008": "测试店",
                    "F0000011": "测试公司", "F0000015": "抖音_测试店", "F0000016": "QS",
                    "F0000005": "启用"},
                   {"ObjectId": "shop-off", "F0000015": "停用店", "F0000005": "停用"}]
        elif schema == cy.H3_SCHEMA_EXPENSE:
            arr = [{"ObjectId": "exp-1", "F0000001": "货款", "F0000002": "mg-1",
                    "F0000014": "1405", "F0000013": "存货", "F0000012": 0.13}]
        elif schema == cy.H3_SCHEMA_MANAGE_EXP:
            arr = [{"ObjectId": "mg-1", "F0000010": "主营业务成本", "F0000025": "货款预算",
                    "F0000008": "库存商品", "F0000011": "资产", "F0000014": "是",
                    "F0000028": "是", "F0000005": "启用"}]
        elif schema == cy.H3_SCHEMA_MONEY_ACCT:
            arr = [{"ObjectId": "acct-1", "F0000008": "第三方资金平台", "F0000019": "测试公司",
                    "F0000006": "抖音_测试店支付宝", "F0000007": "123456",
                    "F0000005": "启用", "F0000023": "wd-1"},
                   {"ObjectId": "acct-bank", "F0000008": "银行", "F0000006": "某银行",
                    "F0000005": "启用", "F0000023": ""}]
        elif schema == cy.H3_SCHEMA_OMS_DEPT:
            arr = [{"ObjectId": "oms-1", "F0000001": "抖音_测试店", "F0000006": "是",
                    "F0000014": "DY001", "F0000016": "QS", "F0000005": "启用"}]
        elif schema == cy.H3_SCHEMA_MONEY_VALUE:
            arr = [{"ObjectId": "mv-1", "F0000004": "acct-1", "F0000013": 300.0},
                   {"ObjectId": "mv-2", "F0000004": "acct-1", "F0000013": 200.0},
                   {"ObjectId": "mv-3", "F0000004": "other", "F0000013": 999.0}]
        return FakeResp({"ReturnData": {"BizObjectArray": arr}})
    if action == "CreateBizObject":
        return FakeResp({"Successful": True, "ReturnData": {"BizObjectId": "new-obj-1"}})
    return FakeResp({}, status=400)


requests.post = fake_post
try:
    headers = {"Content-Type": "application/json", "EngineCode": "TEST", "EngineSecret": "TEST"}
    master = cy.fetch_master_data(headers=headers)
    check("fetch_master 五表齐全", set(master) == {"shop", "expense", "manage_exp", "money_acct", "oms_dept"})
    check("fetch_master 状态过滤（停用店剔除）",
          len(master["shop"]) == 1 and master["shop"].iloc[0]["决算部门"] == "抖音_测试店")
    check("fetch_master 账户性质过滤（银行剔除）", len(master["money_acct"]) == 1)
    check("fetch_master 列名中文化", "账单科目名称" in master["expense"].columns
          and "开户机构全称" in master["money_acct"].columns)
    bal = cy.fetch_account_balance("acct-1", headers=headers)
    check("余额台账 按账户obj合计（300+200=500）", bal == 500.0, str(bal))
    check("余额台账 空obj=0不查", cy.fetch_account_balance("", headers=headers) == 0.0)
    n_before = len(http_calls)
    resp = cy.push_biz_object({"ActionName": "CreateBizObject", "SchemaCode": H3_SCHEMA_BILL,
                               "BizObject": "{}", "IsSubmit": "true"}, headers=headers)
    check("push_biz_object 返回响应", resp.get("Successful") is True)
    check("push 请求体 BizObject 为JSON字符串",
          isinstance(http_calls[-1]["body"]["BizObject"], str))
    check("push URL=氚云OpenApi", http_calls[-1]["url"] == "https://www.h3yun.com/OpenApi/Invoke")
finally:
    requests.post = _orig_post

# ---------------------------------------------------------------- run_chuanyun 端到端（临时 results 根）

tmp = Path(tempfile.mkdtemp(prefix="cy_verify_"))
_orig_output_dir = artifacts.output_dir
artifacts.output_dir = lambda: tmp
_orig_runtime_dir = cy.runtime_dir
cy.runtime_dir = lambda: tmp / "runtime"
_orig_joblog = joblog.save_job_log
logs = []
joblog.save_job_log = lambda *a, **k: logs.append((a, k)) or {}
pipeline.joblog.save_job_log = joblog.save_job_log
_orig_interval = cy.PUSH_INTERVAL_SEC
cy.PUSH_INTERVAL_SEC = 0


def make_bill_batch(name, tpl_rows, *, with_sheet=True):
    root = artifacts.bill_root("2026-10")
    d = root / name
    (d / "底稿").mkdir(parents=True, exist_ok=True)
    fname = f"{name}_账单整理.xlsx"
    fp = d / fname
    with pd.ExcelWriter(fp, engine="openpyxl") as w:
        if with_sheet:
            pd.DataFrame(tpl_rows, columns=list(cy.TEMPLATE_COLUMNS)).to_excel(
                w, sheet_name="氚云导入模板", index=False)
        pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="账单透视", index=False)
    man = {"batch_id": f"bill-{name}", "platform": "douyin", "shop_code": "DY001",
           "shop_name": "抖音_测试店", "period": "2026-10", "status": "success",
           "files": {"excel": str(fp), "excel_list": json.dumps([str(fp)], ensure_ascii=False)}}
    (d / "底稿" / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    return d, fp


def _http_boom(*a, **k):
    raise AssertionError("dry-run 不应发送任何 HTTP 请求！")


def _fetch_boom(*a, **k):
    raise AssertionError("dry-run 不应真实取主数据！")


try:
    tpl_rows = [["抖音_测试店", "货款", "收款", 1000.0], ["抖音_测试店", "货款", "付款", 130.0]]
    d1, fp1 = make_bill_batch("2026-10-抖音-测试店-DY001", tpl_rows)
    d2, fp2 = make_bill_batch("2026-10-抖音-无模板店-DY002", [], with_sheet=False)

    # dry-run：零 HTTP（requests.post 与 fetch_master_data 都是 boom 检测器）
    requests.post = _http_boom
    _orig_fetch = cy.fetch_master_data
    cy.fetch_master_data = _fetch_boom
    try:
        r = pipeline.run_chuanyun(period="2026-10", dry_run=True)
    finally:
        requests.post = _orig_post
        cy.fetch_master_data = _orig_fetch
    check("run_chuanyun dry-run success", r.status == "success", r.message)
    check("dry-run 统计 1 成功 1 跳过", r.data["ok"] == 1 and r.data["skipped"] == 1,
          str(r.data["file_reports"]))
    check("dry-run pushed_to_h3=False", r.data["pushed_to_h3"] is False)
    check("dry-run 文件不移动", fp1.is_file() and not (d1 / cy.SUCCESS_DIRNAME).exists())
    pv = d1 / "底稿" / "chuanyun_preview" / (fp1.stem + ".json")
    check("dry-run payload预览落盘", pv.is_file())
    pv_data = json.loads(pv.read_text(encoding="utf-8"))
    check("预览含完整params与warnings", pv_data["params"] is not None
          and "无主数据快照" in pv_data["warnings"][0])
    check("dry-run joblog 留痕", any(k.get("stage") == "chuanyun" for _, k in logs))

    # dry-run + 本地快照 → ObjectId 映射完整
    snap_dir = tmp / "runtime"
    snap_dir.mkdir(parents=True, exist_ok=True)
    cy.save_master_snapshot(fixture_master())
    requests.post = _http_boom
    cy.fetch_master_data = _fetch_boom
    try:
        r = pipeline.run_chuanyun(period="2026-10", dry_run=True)
    finally:
        requests.post = _orig_post
        cy.fetch_master_data = _orig_fetch
    check("dry-run 快照加载", r.data["master_loaded"] is True and r.status == "success")
    pv_data = json.loads(pv.read_text(encoding="utf-8"))
    check("快照预览 ObjectId 完整",
          json.loads(pv_data["params"]["BizObject"])["F0000021"] == "acct-1"
          and not pv_data["warnings"])

    # 真实路径（fake HTTP）：取数→推送→成功移动+manifest更新
    # 凭据头也打桩（不读真实环境变量；HTTP 已被 fake_post 接管，零真实请求）
    _orig_h3_headers = cy.load_h3_headers
    cy.load_h3_headers = lambda: {"Content-Type": "application/json",
                                  "EngineCode": "TEST", "EngineSecret": "TEST"}
    http_calls.clear()
    requests.post = fake_post
    try:
        r = pipeline.run_chuanyun(period="2026-10", dry_run=False)
    finally:
        requests.post = _orig_post
    check("真实推送 success", r.status == "success" and r.data["ok"] == 1, r.message)
    check("真实推送 pushed_to_h3=True", r.data["pushed_to_h3"] is True)
    creates = [c for c in http_calls if c["body"].get("ActionName") == "CreateBizObject"]
    loads = [c for c in http_calls if c["body"].get("ActionName") == "LoadBizObjects"]
    check("HTTP 序列：5主数据+1余额+1推送", len(loads) == 6 and len(creates) == 1,
          f"loads={len(loads)} creates={len(creates)}")
    moved = d1 / cy.SUCCESS_DIRNAME / fp1.name
    check("成功文件移入 氚云导入成功/", moved.is_file() and not fp1.exists())
    man_after = json.loads((d1 / "底稿" / "manifest.json").read_text(encoding="utf-8"))
    check("manifest excel 指向新位置", man_after["files"]["excel"] == str(moved))
    biz_pushed = json.loads(creates[0]["body"]["BizObject"])
    check("推送余额=台账500（时序修复：先匹配账户再查余额）",
          biz_pushed["F0000022"] == 500.0 and biz_pushed["F0000023"] == 1370.0,
          str(biz_pushed["F0000022"]))
    check("真实取数后自动留快照", cy.master_snapshot_path().is_file())

    # 推送失败 → 文件留守
    d3, fp3 = make_bill_batch("2026-10-抖音-失败店-DY003", tpl_rows)

    def fake_post_fail(url, headers=None, json=None, data=None, timeout=None, **kw):
        # push_biz_object 用 data=JSON字符串 发请求，须与 fake_post 同口径解析
        body = json if json is not None else (
            __import__("json").loads(data) if isinstance(data, (str, bytes)) else {})
        if body.get("ActionName") == "CreateBizObject":
            return FakeResp({"Successful": False}, status=500)
        return fake_post(url, headers=headers, json=json, data=data, timeout=timeout)

    requests.post = fake_post_fail
    try:
        r = pipeline.run_chuanyun(period="2026-10", bill_batch_id=d3.name, dry_run=False)
    finally:
        requests.post = _orig_post
        cy.load_h3_headers = _orig_h3_headers
    check("推送失败终态 failed", r.status == "failed", r.message)
    check("失败文件留守", fp3.is_file() and not (d3 / cy.SUCCESS_DIRNAME).exists())

    # period 必填
    try:
        pipeline.run_chuanyun(period="")
        check("period必填抛错", False)
    except Exception as exc:
        check("period必填抛错", "period" in str(exc))
finally:
    requests.post = _orig_post
    artifacts.output_dir = _orig_output_dir
    cy.runtime_dir = _orig_runtime_dir
    cy.PUSH_INTERVAL_SEC = _orig_interval
    joblog.save_job_log = _orig_joblog
    pipeline.joblog.save_job_log = _orig_joblog
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 入口接线静态检查

check("ACTIVE_STAGES 含 chuanyun", "chuanyun" in pipeline.ACTIVE_STAGES)
caps = {c["key"]: c["status"] for c in pipeline.capability_status()}
check("capability_status 五能力全 active",
      all(caps.get(k) == "active" for k in ("bill", "match", "cost", "writeback", "chuanyun")),
      str(caps))

import actions  # noqa: E402
import inspect  # noqa: E402
sig = inspect.signature(actions.chuanyun_sync)
check("actions.chuanyun_sync 参数齐全",
      all(p in sig.parameters for p in ("period", "bill_batch_id", "dry_run")))
src = inspect.getsource(actions.start_recon)
check("start_recon chuanyun 缺省 dry_run=1", 'elif st == "chuanyun":' in src)

print(f"\n==== verify_chuanyun: PASS={PASS} FAIL={FAIL} ====")
sys.exit(1 if FAIL else 0)