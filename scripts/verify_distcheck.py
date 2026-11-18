# -*- coding: utf-8 -*-
"""分发前安全自检验证（2026-11-18，凭据防泄露机制）。

对象：core/distcheck.py（scan_workspace / scan_git_history / dist_check）
     + actions.dist_check 归一出口 + config.json 工具声明。

纪律：只读扫描；注入测试用**临时副本目录**（不碰真实插件目录），
植入的是假秘密（FAKE_*），不读取/不回显本机真实凭据值。

用法：python scripts/verify_distcheck.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, r"D:\DesktopAgent")

from core import distcheck  # noqa: E402

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


# ---------------------------------------------------------------- 真实插件目录：应 safe/caution（绝无凭据命中）

ws = distcheck.scan_workspace()
cred = [f for f in ws["findings"] if f["kind"] in ("live_credential", "hardcoded_credential", "private_key")]
check("真实工作区 凭据/私钥 0 命中", not cred, str(cred)[:300])
check("真实工作区 扫描覆盖>50文件", ws["files_scanned"] > 50, str(ws["files_scanned"]))
rt = ws["runtime_files_in_plugin_dir"]
check("运行时文件仅 SSH 残留（已知无害）",
      all("ssh_known_hosts" in x for x in rt), str(rt))

git = distcheck.scan_git_history()
check("git 历史扫描 applicable", git.get("applicable") is True)
check("git 历史 凭据/私钥 0 命中", not (git.get("findings") or []), str(git.get("findings"))[:300])

full = distcheck.dist_check()
check("dist_check verdict 非 blocked", full["verdict"] in ("safe", "caution"), full["verdict"])
check("packaging_excludes 含关键项",
      all(x in full["packaging_excludes"] for x in
          (".git/", "settings.json", "chuanyun_master.json", "*.db", "*ssh_known_hosts*")))
check("credential_architecture 说明完整",
      "环境变量" in full["credential_architecture"]["storage"]
      and "RECON_H3_ENGINE_SECRET" in full["credential_architecture"]["h3yun"])

# ---------------------------------------------------------------- 注入测试：临时副本必须 blocked

tmp = Path(tempfile.mkdtemp(prefix="distcheck_verify_"))
try:
    fake = tmp / "plugin"
    fake.mkdir()
    (fake / "core").mkdir()
    (fake / "config.json").write_text('{"name":"x"}', encoding="utf-8")
    (fake / "core" / "ok.py").write_text(
        'ENV_ADB_PASSWORD = "RECON_ADB_PASSWORD"\n'   # 环境变量名常量：不算泄露
        'headers = {"EngineSecret": "TEST"}\n',        # 测试占位：不算泄露
        encoding="utf-8")

    r = distcheck.scan_workspace(fake)
    check("干净副本 0 命中", not r["findings"], str(r["findings"]))

    # 1) 硬编码口令样赋值 → blocked
    # 假秘密在运行时拼接构造：本验证脚本自身不含完整字面量，
    # 否则真实工作区扫描会把测试夹具当成泄露（自触发）
    fake_pwd = "Super" + "Secret123!"
    key_name = "pass" + "word"  # 键名也拼接：避免本脚本出现完整赋值字面量
    (fake / "core" / "bad.py").write_text(
        key_name + ' = "' + fake_pwd + '"\n', encoding="utf-8")
    r = distcheck.scan_workspace(fake)
    hit = [f for f in r["findings"] if f["kind"] == "hardcoded_credential"]
    check("硬编码口令 → 命中", len(hit) == 1 and hit[0]["file"] == "core/bad.py", str(r["findings"]))
    check("命中值打码不回显原文", hit and fake_pwd not in json.dumps(r, ensure_ascii=False))
    (fake / "core" / "bad.py").unlink()

    # 2) PEM 私钥 → blocked（同样运行时拼接，避免自触发）
    pem_head = "-----BEGIN " + "RSA PRIVATE KEY-----"
    pem_tail = "-----END " + "RSA PRIVATE KEY-----"
    (fake / "core" / "key.py").write_text(
        f'KEY = """{pem_head}\nMIIfake\n{pem_tail}"""\n', encoding="utf-8")
    r = distcheck.scan_workspace(fake)
    check("PEM 私钥 → 命中", any(f["kind"] == "private_key" for f in r["findings"]))
    (fake / "core" / "key.py").unlink()

    # 3) 本机真实凭据值出现在文件里 → live_credential（用假环境变量值模拟，monkeypatch env）
    # 假秘密同样运行时拼接：否则本脚本自身含该字面量，真实目录扫描会自触发 blocked
    fake_live = "FAKE_LIVE_" + "SECRET_9x7"
    _orig_env = distcheck.env
    distcheck.env = lambda name: fake_live if name == "RECON_ADB_PASSWORD" else ""
    try:
        (fake / "core" / "leak.py").write_text(
            '# 调试时不小心写死\nconn_pwd = "' + fake_live + '"\n', encoding="utf-8")
        r = distcheck.scan_workspace(fake)
        live = [f for f in r["findings"] if f["kind"] == "live_credential"]
        check("本机凭据值入文件 → live_credential 命中", len(live) >= 1, str(r["findings"]))
        check("live 命中注明变量名", live and "RECON_ADB_PASSWORD" in live[0]["detail"])
        check("live 命中不泄漏原值", fake_live not in json.dumps(r, ensure_ascii=False))
        # dist_check 整体 verdict（副本无 .git → git 段 not_applicable）
        full2 = distcheck.dist_check()  # 注意：dist_check 扫真实目录，这里直接验 scan 逻辑即可
        check("真实目录 dist_check 仍非 blocked（注入只在副本）", full2["verdict"] != "blocked")
    finally:
        distcheck.env = _orig_env
        (fake / "core" / "leak.py").unlink()

    # 4) 运行时文件混入 → caution 判据
    (fake / "settings.json").write_text('{"results_dir":"C:/x"}', encoding="utf-8")
    (fake / "plugin.db").write_bytes(b"fake")
    (fake / "results").mkdir()
    (fake / "results" / "a.xlsx").write_bytes(b"x")
    r = distcheck.scan_workspace(fake)
    check("运行时文件检出（settings/db/results）",
          {"settings.json", "plugin.db", "results/a.xlsx"} <= set(r["runtime_files_in_plugin_dir"]),
          str(r["runtime_files_in_plugin_dir"]))
    check("运行时文件不算凭据命中", not r["findings"])

    # 5) 无 .git 目录 → git 段 not_applicable 不报错
    g = distcheck.scan_git_history(fake)
    check("无 .git → not_applicable", g["applicable"] is False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- actions / config 接线

import actions  # noqa: E402
r = actions.dist_check()
check("actions.dist_check 归一 ok", r["ok"] and r["data"]["verdict"] in ("safe", "caution"), r["summary"])
check("actions summary 可读", len(r["summary"]) > 10, r["summary"])
r = actions.dist_check(include_git="0")
check("include_git=0 跳过历史", r["ok"] and r["data"]["git_history"]["applicable"] is False)

cfg = json.loads((PLUGIN / "config.json").read_text(encoding="utf-8"))
tools = {t["name"]: t for t in cfg["mcp_tools"]}
check("config.json 声明 dist_check", "recon_workbench__dist_check" in tools
      and tools["recon_workbench__dist_check"]["action"] == "dist_check")
check("mcp_tools 总数 28", len(cfg["mcp_tools"]) == 28, str(len(cfg["mcp_tools"])))

print(f"\n==== verify_distcheck: PASS={PASS} FAIL={FAIL} ====")
sys.exit(1 if FAIL else 0)