"""分发前安全自检（2026-11-18，凭据防泄露机制）。

回答的问题：把插件目录分享给其他 Desktop Agent 用户，会不会带出凭据/隐私？

凭据存储架构（审计结论，详见 docs/SECURITY.md）：
  · ADB 口令 / 氚云 EngineCode+EngineSecret 只存**本机环境变量**
    （进程环境 → HKCU\\Environment 注册表兜底），插件目录内零凭据文件；
  · settings.json / plugin.db / results / downloads / logs / 主数据快照
    全部在运行时目录 data/extensions/recon_workbench/（插件目录**外**），
    正常打包插件目录不会带出；
  · 本模块是「分发前最后一道闸」：扫描插件目录（含 git 历史）是否混入
    凭据样文本/运行时文件/私钥，给出打包排除清单与 go/no-go 结论。

只读扫描：不修改任何文件、不连库、不发 HTTP、不读取环境变量值
（只检查「值是否出现在插件文件里」这一泄露方向，且命中只报位置不回显原文）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .config import (
    ENV_ADB_HOST, ENV_ADB_PASSWORD, ENV_ADB_USER,
    ENV_H3_ENGINE_CODE, ENV_H3_ENGINE_SECRET, env, plugin_dir,
)

#: 扫描的文本文件类型（二进制不扫；.git 单独走历史扫描）
_TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".sql", ".yaml", ".yml",
                  ".html", ".js", ".css", ".csv", ".ini", ".cfg", ".toml"}

#: 不应出现在插件目录里的运行时文件/目录（应在 data/extensions/recon_workbench/）
_RUNTIME_NAMES = {"settings.json", "plugin.db", "plugin.db-journal",
                  "chuanyun_master.json", "jobs.jsonl"}
_RUNTIME_DIRS = {"results", "downloads", "logs", "extracted"}

#: 打包排除清单（分享 zip 时剔除；.git 含开发历史，建议一并剔除）
PACKAGING_EXCLUDES = [
    ".git/", "__pycache__/", "*.pyc",
    "results/", "downloads/", "logs/", "extracted/",
    "*.db", "*.db-journal", "*.parquet",
    "settings.json", "chuanyun_master.json",
    "*ssh_known_hosts*", ".venv/", ".vscode/", ".idea/",
]

#: 硬编码凭据样赋值（password/secret/token/api_key = "非占位值"）
_ASSIGN_RE = re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|engine_?secret|api_?key|access_?key|token)\b"""
    r"""\s*[=:]\s*["']([^"'\n]{6,})["']""")
#: 占位/环境变量名/测试值白名单（命中不算泄露）
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(RECON_[A-Z_]+|TEST|test.*|\*+|x+|your[_-].*|<.*>|\{\{.*\}\}|placeholder.*|"
    r"changeme|None|null|env\(.*|os\.environ.*)$")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _mask(s: str) -> str:
    """命中值只回显首尾各 2 字符，中间打码（报告本身不成为二次泄露源）。"""
    s = str(s)
    return s[:2] + "***" + s[-2:] if len(s) > 6 else "***"


def scan_workspace(root: Path | str | None = None) -> dict[str, Any]:
    """扫插件目录工作区（不含 .git）：凭据样文本 / 本机凭据值 / 私钥 / 运行时文件。

    本机凭据值检查：把当前环境变量里的真实口令/密钥作为「已知秘密」在文件里
    找子串——这是最直接的泄露判据（值出现在文件里=打包必带出）。
    只报文件与行号，不回显秘密原文。
    """
    root = Path(root) if root else plugin_dir()
    findings: list[dict] = []
    runtime_files: list[str] = []
    scanned = 0

    # 已知秘密（本机真实凭据值；空值跳过）
    known_secrets: list[tuple[str, str]] = []
    for name in (ENV_ADB_PASSWORD, ENV_ADB_USER, ENV_ADB_HOST,
                 ENV_H3_ENGINE_CODE, ENV_H3_ENGINE_SECRET):
        v = env(name).strip()
        if v and len(v) >= 6:
            known_secrets.append((name, v))

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()  # 统一正斜杠（跨平台报告可比对）
        parts = set(p.relative_to(root).parts)
        if ".git" in parts or "__pycache__" in parts:
            continue
        if p.name in _RUNTIME_NAMES or (parts & _RUNTIME_DIRS):
            runtime_files.append(rel)
        if "ssh_known_hosts" in p.name:
            runtime_files.append(rel)
        if p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            for name, val in known_secrets:
                if val in line:
                    findings.append({"file": rel, "line": i, "kind": "live_credential",
                                     "detail": f"本机环境变量 {name} 的值出现在文件中",
                                     "masked": _mask(val)})
            if _PRIVATE_KEY_RE.search(line):
                findings.append({"file": rel, "line": i, "kind": "private_key",
                                 "detail": "PEM 私钥块"})
            for m in _ASSIGN_RE.finditer(line):
                val = m.group(2)
                if _PLACEHOLDER_RE.match(val.strip()):
                    continue
                # 环境变量名本身（如 ENV_ADB_PASSWORD = "RECON_ADB_PASSWORD"）不算
                if val.strip().startswith("RECON_"):
                    continue
                findings.append({"file": rel, "line": i, "kind": "hardcoded_credential",
                                 "detail": f"{m.group(1)} 硬编码赋值", "masked": _mask(val)})
    return {"root": str(root), "files_scanned": scanned,
            "findings": findings, "runtime_files_in_plugin_dir": runtime_files}


def scan_git_history(root: Path | str | None = None, *,
                     max_commits: int = 500) -> dict[str, Any]:
    """扫 git 全历史：已知秘密值 + 私钥块是否曾提交过（分享 .git 时的泄露面）。

    无 .git / 无 git 命令 → 返回 not_applicable（不算失败）。
    """
    root = Path(root) if root else plugin_dir()
    if not (root / ".git").exists():
        return {"applicable": False, "reason": "插件目录无 .git（直接拷贝分享不涉及历史）"}
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-list", "--all"],
                           capture_output=True, text=True, timeout=60)
        revs = r.stdout.split()[:max_commits]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"applicable": False, "reason": f"git 不可用：{type(exc).__name__}"}
    if not revs:
        return {"applicable": True, "commits": 0, "findings": []}

    findings: list[dict] = []
    # PEM 头必须带 ----- 前后缀（真实私钥块格式），且**运行时拼接**：
    # 若在本文件写完整字面量，git grep 会在历史里的 distcheck.py 自身命中（自触发
    # blocked，2026-11-18 实测）；裸 "BEGIN RSA PRIVATE KEY" 也会命中本文件的模式
    # 定义行。拼接 + 全 PEM 头双保险，与 verify_distcheck 夹具同一纪律。
    patterns: list[tuple[str, str]] = [
        ("private_key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
        ("private_key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
        ("private_key", "-----BEGIN " + "EC PRIVATE KEY-----"),
        ("private_key", "-----BEGIN " + "PRIVATE KEY-----"),
    ]
    for name in (ENV_ADB_PASSWORD, ENV_H3_ENGINE_SECRET, ENV_H3_ENGINE_CODE):
        v = env(name).strip()
        if v and len(v) >= 6:
            patterns.append((f"live_credential({name})", v))
    for kind, pat in patterns:
        try:
            r = subprocess.run(["git", "-C", str(root), "grep", "-l", pat, *revs],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in r.stdout.splitlines():
            if line.strip():
                # 输出形如 <rev>:<path>；不回显秘密本身
                findings.append({"kind": kind, "hit": line.split(":", 1)[-1],
                                 "commit": line.split(":", 1)[0][:12]})
    return {"applicable": True, "commits": len(revs), "findings": findings}


def dist_check(*, include_git: bool = True) -> dict[str, Any]:
    """分发前安全自检主入口（actions.dist_check 调用）。

    结论三档：
      safe     工作区与历史均无凭据命中、插件目录无运行时文件 → 可分享
      caution  仅运行时文件/SSH残留等非凭据问题 → 按排除清单打包后可分享
      blocked  发现凭据/私钥命中 → 先清理再分享（findings 给文件+行号）
    """
    ws = scan_workspace()
    git = scan_git_history() if include_git else {"applicable": False, "reason": "跳过"}
    cred_hits = [f for f in ws["findings"]
                 if f["kind"] in ("live_credential", "hardcoded_credential", "private_key")]
    git_hits = git.get("findings") or []
    if cred_hits or git_hits:
        verdict, note = "blocked", "发现凭据/私钥命中，清理后再分享（见 findings）"
    elif ws["runtime_files_in_plugin_dir"]:
        verdict, note = "caution", "插件目录混入运行时文件/残留，按 packaging_excludes 打包剔除后可分享"
    else:
        verdict, note = "safe", "未发现凭据泄露面，可分享"
    return {
        "verdict": verdict,
        "note": note,
        "workspace": ws,
        "git_history": git,
        "packaging_excludes": PACKAGING_EXCLUDES,
        "credential_architecture": {
            "storage": "本机环境变量（进程环境 → HKCU\\Environment 注册表兜底），插件目录零凭据文件",
            "adb": [ENV_ADB_HOST, ENV_ADB_USER, ENV_ADB_PASSWORD, "RECON_ADB_PORT", "RECON_ADB_DATABASE"],
            "h3yun": [ENV_H3_ENGINE_CODE, ENV_H3_ENGINE_SECRET],
            "runtime_data_location": "data/extensions/recon_workbench/（插件目录外：settings/plugin.db/results/downloads/logs/主数据快照）",
            "receiver_side": "接收方凭据为空 → env_status 报缺失 → 各自配环境变量；dry-run 与本地功能不需要凭据",
        },
        "docs": "docs/SECURITY.md",
    }