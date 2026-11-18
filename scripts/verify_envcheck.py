# -*- coding: utf-8 -*-
"""环境自检模块验证（2026-11-18）。

对象：core/envcheck.py（env_status / feature_readiness / DEPENDENCIES）
     + skills/recon_assist/environment_checks.yaml（宿主 env_check 清单）
     + requirements.txt（人工安装参考）

纪律：只读探测，不安装任何东西、不连库、不发 HTTP。
用法：python scripts/verify_envcheck.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, r"D:\DesktopAgent")

from core import envcheck  # noqa: E402

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


# ---------------------------------------------------------------- 三处清单一致性（SSOT=DEPENDENCIES）

specs = {s.pypi: s for s in envcheck.DEPENDENCIES}
check("DEPENDENCIES 8 包三层齐全",
      len(specs) == 8
      and sum(s.tier == "required" for s in specs.values()) == 3
      and sum(s.tier == "conditional" for s in specs.values()) == 2
      and sum(s.tier == "optional" for s in specs.values()) == 3)

# environment_checks.yaml（不依赖 PyYAML：轻量行解析，与宿主 yaml 语义等价校验）
yaml_text = (PLUGIN / "skills" / "recon_assist" / "environment_checks.yaml").read_text(encoding="utf-8")
yaml_targets = re.findall(r"^\s*target:\s*(\S+)\s*$", yaml_text, re.M)
yaml_ids = re.findall(r"^\s*-\s*id:\s*(\S+)\s*$", yaml_text, re.M)
yaml_kinds = re.findall(r"^\s*kind:\s*(\S+)\s*$", yaml_text, re.M)
check("yaml 清单与 DEPENDENCIES 包集合一致", set(yaml_targets) == set(specs),
      f"yaml={sorted(yaml_targets)}")
check("yaml 全部 python_package", set(yaml_kinds) == {"python_package"})
check("yaml id 合法（小写连字符）且唯一",
      len(yaml_ids) == len(set(yaml_ids))
      and all(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", i) for i in yaml_ids))
try:
    import yaml as _yaml
    parsed = _yaml.safe_load(yaml_text)
    check("yaml 可被宿主 PyYAML 解析", isinstance(parsed.get("checks"), list)
          and len(parsed["checks"]) == 8)
except ImportError:
    print("[SKIP] PyYAML 不在运行时（宿主侧有），行解析校验已覆盖")

# requirements.txt
req_text = (PLUGIN / "requirements.txt").read_text(encoding="utf-8")
req_pkgs = {re.split(r"[><=]", ln.strip())[0].strip()
            for ln in req_text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")}
check("requirements.txt 与 DEPENDENCIES 包集合一致", req_pkgs == set(specs),
      f"req={sorted(req_pkgs)}")

# ---------------------------------------------------------------- feature_readiness 纯函数

ALL = {p: True for p in specs}
r = envcheck.feature_readiness(ALL)
check("全装 → 六能力全 ready", all(v["status"] == "ready" for v in r.values()) and len(r) == 6)

no_core = dict(ALL, pandas=False)
r = envcheck.feature_readiness(no_core)
check("缺 pandas → 全部 not_ready", all(v["status"] == "not_ready" for v in r.values()))

no_mysql = dict(ALL)
no_mysql["mysql-connector-python"] = False
r = envcheck.feature_readiness(no_mysql)
check("缺 mysql 驱动 → match/writeback degraded，其余 ready",
      r["match"]["status"] == "degraded" and r["writeback"]["status"] == "degraded"
      and r["bill"]["status"] == "ready" and r["cost"]["status"] == "ready"
      and r["chuanyun"]["status"] == "ready" and r["summaries"]["status"] == "ready")
check("degraded 注明 dry-run 仍可用", "dry-run" in r["match"]["note"])

no_req = dict(ALL, requests=False)
r = envcheck.feature_readiness(no_req)
check("缺 requests → 仅 chuanyun degraded",
      r["chuanyun"]["status"] == "degraded"
      and all(r[k]["status"] == "ready" for k in ("bill", "match", "cost", "writeback", "summaries")))

no_opt = dict(ALL, xlsxwriter=False, pyarrow=False)
no_opt["python-calamine"] = False
r = envcheck.feature_readiness(no_opt)
check("可选包全缺 → 六能力仍 ready（自动回落）", all(v["status"] == "ready" for v in r.values()))

# ---------------------------------------------------------------- 真实 env_status（本机只读探测）

data = envcheck.env_status()
check("env_status overall 合法", data["overall"] in ("ok", "warning", "error"))
check("packages 8 项含版本/层级/回落说明",
      len(data["packages"]) == 8
      and all({"pypi", "tier", "installed", "purpose", "fallback"} <= set(p) for p in data["packages"]))
by_name = {p["pypi"]: p for p in data["packages"]}
check("本机核心三包已装", all(by_name[p]["installed"] for p in ("pandas", "numpy", "openpyxl")))
check("已装包带版本号", by_name["pandas"]["version"] is not None)
check("mysql.connector 子模块探测", by_name["mysql-connector-python"]["installed"] is True)
check("optional 缺失进 missing.optional 而非阻塞",
      all(p in data["missing"]["optional"] for p in ("xlsxwriter", "pyarrow")
          if not by_name[p]["installed"]))
check("install_guidance 指向 venv_install", data["install_guidance"]["tool"] == "venv_install")
check("credentials 段含 adb/h3yun 且不暴露口令",
      {"adb", "h3yun"} <= set(data["credentials"])
      and "password" not in str(data["credentials"]).lower().replace("recon_adb_password", "")
      .replace("missing_env", ""))
check("features 六能力矩阵", len(data["features"]) == 6)

# actions 归一出口
import actions  # noqa: E402
r = actions.env_status()
check("actions.env_status 归一 ok", r["ok"] and r["data"]["overall"] in ("ok", "warning", "error"),
      r["summary"])
check("actions summary 可读（含缺失/就绪说明）", len(r["summary"]) > 5, r["summary"])

# config.json 声明
import json  # noqa: E402
cfg = json.loads((PLUGIN / "config.json").read_text(encoding="utf-8"))
tools = {t["name"]: t for t in cfg["mcp_tools"]}
check("config.json 声明 env_status 工具",
      "recon_workbench__env_status" in tools
      and tools["recon_workbench__env_status"]["action"] == "env_status")
check("mcp_tools 总数 28", len(cfg["mcp_tools"]) == 28, str(len(cfg["mcp_tools"])))

print(f"\n==== verify_envcheck: PASS={PASS} FAIL={FAIL} ====")
sys.exit(1 if FAIL else 0)