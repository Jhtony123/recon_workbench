/* 对账工作台 — 管理页逻辑（2026-11-14 方案改版）
 * 契约（sop-ui-design.md §6 / sop-ai-collab.md §1.1）：
 *   · 只打同源 Sidecar API：/agent/extensions/recon_workbench/*
 *   · 「问 AI」只发 extension:ask-ai 的 wraps，芯片与气泡由宿主绘制
 *   · 「在会话中引用」发 extension:use-in-chat；targetOrigin 必须 "*"
 *   · 元素点选由宿主 Design Mode 注入，本文件零选择器脚本
 *   · 主界面禁止暴露端口/CDP/内部 id；技术细节折叠进「高级」
 *
 * 改版要点：
 *   · 只有「账单整理」是真功能；SKU对账/成本匹配/数据库回传/氚云对接渲染占位卡
 *   · 账单结果、规则、作业留痕都是**文件态**，UI 文案不再说「入库」
 *   · 总览不再显示匹配率/未匹配差异（SKU对账暂缓，恒 0 会被误读成「全对上了」）
 */
"use strict";

const PLUGIN = "recon_workbench";
const BASE = `/agent/extensions/${PLUGIN}`;

const PAGES = ["home", "bill", "match", "cost", "writeback", "chuanyun", "sources", "jobs", "settings"];
const $ = (id) => document.getElementById(id);

/* ---------------- API ---------------- */

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let payload = null;
  try { payload = await res.json(); } catch { /* 非 JSON */ }
  if (!res.ok) {
    const detail = (payload && (payload.detail?.message || payload.detail || payload.message)) || `HTTP ${res.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** 调插件 action 工具（与 Agent 同一执行器）。 */
async function tool(name, params) {
  const out = await apiPost(`${BASE}/mcp-tools/execute`, { tool_name: name, params: params || {} });
  const r = (out && (out.result || out)) || {};
  if (r && r.ok === false) {
    const msg = (r.error && r.error.message) || r.summary || "执行失败";
    const err = new Error(msg);
    err.payload = r;
    throw err;
  }
  return r;
}

/** 取列表行：action 在 data.rows / data[]，read_only 在**顶层** rows。
 *
 * 2026-11-14 浏览器实测教训：read_only sql_template 工具由
 * extensions/readonly_sql.execute_readonly_query 返回 {ok,columns,rows,row_count}，
 * 行在顶层；旧 rowsOf 只看 res.data，于是恒为空且被 .catch() 静默吞掉，控制台零报错。
 * 改版后已无 read_only 工具，但这里仍两种都认，避免日后加回同类工具再踩一次。
 */
function rowsOf(res) {
  if (!res) return [];
  if (Array.isArray(res.rows)) return res.rows;
  const d = res.data || {};
  if (Array.isArray(d)) return d;
  if (Array.isArray(d.rows)) return d.rows;
  return [];
}

/* ---------------- UI 基础 ---------------- */

function toast(msg, kind) {
  const el = $("toast");
  if (!el) return;
  el.textContent = String(msg || "");
  el.className = `toast show ${kind || ""}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = "toast"; }, 4200);
}

function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const NUMERIC_HINT = /(_fen|_yuan|金额|成本|毛利|收款|退款|分摊|单价|吊牌|pct|rate|qty|数量|rows|n$)/i;

function isNum(v) { return typeof v === "number" && Number.isFinite(v); }

function fmtNum(key, v) {
  if (!isNum(v)) return esc(v);
  if (/pct|rate|率/i.test(key)) return `${v.toFixed(2)}%`;
  if (/(_yuan|金额|成本|毛利|收款|退款|分摊|单价|吊牌)/.test(key)) {
    return v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (Number.isInteger(v)) return v.toLocaleString("zh-CN");
  return String(v);
}

/** 渲染表格；rows 为空时给业务化空态。 */
function renderTable(host, rows, opts) {
  const el = typeof host === "string" ? $(host) : host;
  if (!el) return;
  const o = opts || {};
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="empty">${esc(o.empty || "还没有数据")}</div>`;
    return;
  }
  let cols = o.columns || Object.keys(rows[0]);
  if (o.maxCols) cols = cols.slice(0, o.maxCols);
  const labels = o.labels || {};

  const head = cols.map((c) => {
    const numeric = NUMERIC_HINT.test(c) || rows.some((r) => isNum(r[c]));
    return `<th class="${numeric ? "num" : ""}">${esc(labels[c] || c)}</th>`;
  }).join("");

  const body = rows.slice(0, o.limit || 300).map((r) => `<tr>${cols.map((c) => {
    const v = r[c];
    const numeric = isNum(v);
    let cls = numeric ? "num" : "";
    if (numeric && v < 0) cls += " neg";
    else if (numeric && /毛利|gross/.test(c) && v > 0) cls += " pos";
    let text = fmtNum(c, v);
    if (o.render && o.render[c]) text = o.render[c](v, r);
    return `<td class="${cls}">${text}</td>`;
  }).join("")}</tr>`).join("");

  el.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderMetrics(host, items) {
  const el = typeof host === "string" ? $(host) : host;
  if (!el) return;
  if (!items || !items.length) { el.innerHTML = ""; return; }
  el.innerHTML = items.map((m) => `
    <div class="metric">
      <div class="m-label">${esc(m.label)}</div>
      <div class="m-value ${esc(m.tone || "")}">${esc(m.value)}</div>
      ${m.hint ? `<div class="m-hint">${esc(m.hint)}</div>` : ""}
    </div>`).join("");
}

function statusTag(status) {
  const map = {
    success: ["ok", "成功"], partial: ["warn", "部分成功"], skipped: ["warn", "已跳过"],
    deferred: ["warn", "暂缓开放"],
    failed: ["danger", "失败"], running: ["run", "运行中"], queued: ["run", "排队中"],
    cancelled: ["warn", "已取消"], timed_out: ["danger", "超时"],
  };
  const hit = map[String(status || "").toLowerCase()] || ["", String(status || "-")];
  return `<span class="tag ${hit[0]}">${esc(hit[1])}</span>`;
}

function humanize(text) {
  // 主界面降噪：过滤端口、堆栈、内部 id 等技术细节
  return String(text || "")
    .replace(/Traceback[\s\S]*$/m, "（详细堆栈见作业日志）")
    .replace(/127\.0\.0\.1:\d+/g, "本机服务")
    .replace(/timed out/g, "响应超时");
}

/* ---------------- 路由 ---------------- */

function showPage(id) {
  const page = PAGES.includes(id) ? id : "home";
  document.querySelectorAll(".page").forEach((s) => {
    s.classList.toggle("active", s.dataset.page === page);
  });
  document.querySelectorAll("#nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === page);
  });
  const loader = PAGE_LOADERS[page];
  if (loader) Promise.resolve(loader()).catch((e) => toast(humanize(e.message || e), "danger"));
}

function applyHash() {
  const hit = PAGES.find((p) => `#/${p}` === location.hash);
  if (!hit) { history.replaceState(null, "", "#/home"); showPage("home"); return; }
  showPage(hit);
}

window.addEventListener("hashchange", applyHash);

/* ---------------- AI 协同 ---------------- */

/** 「问 AI」：只发 wraps，芯片由宿主绘制，不自动发送。 */
function askAI(label, body) {
  window.parent.postMessage({
    type: "extension:ask-ai",
    plugin: PLUGIN,
    text: "",
    wraps: [{ label: String(label).slice(0, 40), body: String(body || "").slice(0, 20000) }],
  }, "*");
  toast("已把内容放进输入框，补充一句问题后发送即可", "ok");
}

function useInChat() {
  // targetOrigin 必须 "*"：iframe 源是 Sidecar，与外壳跨域，用 location.origin 会被静默丢弃
  window.parent.postMessage({ type: "extension:use-in-chat", plugin: PLUGIN }, "*");
}

/** 汇总当前页面的关键数据作为「问 AI」正文。 */
async function buildAskPayload(kind) {
  const ctx = collectForm();
  const head = `插件：对账工作台（recon_workbench）\n当前上下文：${JSON.stringify(ctx)}\n`;

  if (kind === "batches") {
    const res = await tool("recon_workbench__bill_batches", { limit: 20 });
    const rows = rowsOf(res);
    const body = `${head}\n账单整理批次（${rows.length} 个）：\n${JSON.stringify(rows, null, 2)}\n\n请判断这些批次的行数与货款合计是否合理，有没有明显异常（比如行数为 0、货款合计与账单透视对不上）。`;
    return ["账单批次核对", body];
  }
  if (kind === "clear") {
    const res = await tool("recon_workbench__bill_batches", { limit: 30 });
    const rows = rowsOf(res);
    const body = `${head}\n当前账单整理批次（${rows.length} 个）：\n${JSON.stringify(rows, null, 2)}\n\n我想清理其中一部分历史（按账期/平台/店铺）。请先列出可清理的条件组合让我选择，确认后调用 clear_history（confirm=yes，附 period/platform/shop_code 条件；只删命中批次，下载副本与作业留痕保留）。未经我确认不要执行。`;
    return ["按条件清理历史", body];
  }
  if (kind === "preview") {
    const el = $("bill-preview-out");
    const body = `${head}\n账单试跑结果：\n${(el && el.textContent || "").slice(0, 8000)}`;
    return ["账单映射校验", body];
  }
  if (kind === "rules") {
    const res = await tool("recon_workbench__query_rules", { platform: $("rule-platform")?.value || "", limit: 200 });
    const body = `${head}\n当前平台解析规则（rules/*.json）：\n${JSON.stringify(rowsOf(res), null, 2)}\n\n请帮我判断还缺哪些 column_map / biz_code 规则。`;
    return ["规则补全建议", body];
  }
  if (kind === "joblog") {
    const res = await tool("recon_workbench__query_job_log", { limit: 30 });
    const body = `${head}\n作业留痕（logs/jobs.jsonl）：\n${JSON.stringify(rowsOf(res), null, 2)}`;
    return ["作业失败排查", body];
  }
  if (kind === "capabilities") {
    const res = await tool("recon_workbench__capability_status", {});
    const body = `${head}\n能力开放状态：\n${JSON.stringify(res.data || {}, null, 2)}`;
    return ["能力开放状态", body];
  }
  const res = await tool("recon_workbench__overview", { days: 90 });
  return ["对账总览", `${head}\n总览数据：\n${JSON.stringify(res.data || {}, null, 2)}`];
}

/* ---------------- 表单 ---------------- */

function monthValue(v) { return String(v || "").slice(0, 7); }

function collectForm() {
  const page = document.querySelector(".page.active");
  const id = page ? page.dataset.page : "home";
  const g = (k) => ($(k) ? $(k).value.trim() : "");
  if (id === "bill") {
    return {
      platform: g("bill-platform"), shop_code: g("bill-shop"), period: monthValue(g("bill-period")),
      s_date: g("bill-sdate"), e_date: g("bill-edate"),
      file_paths: g("bill-files"), extra_files: g("bill-extra"),
    };
  }
  return {};
}

/** 从账期推导默认起止日（账期通常是自然月）。 */
function fillDatesFromPeriod(periodEl, sEl, eEl) {
  const p = monthValue(periodEl?.value);
  if (!p || !/^\d{4}-\d{2}$/.test(p)) return;
  const [y, m] = p.split("-").map(Number);
  const s = `${p}-01`;
  const last = new Date(y, m, 0).getDate();
  const e = `${p}-${String(last).padStart(2, "0")}`;
  if (sEl && !sEl.value) sEl.value = s;
  if (eEl && !eEl.value) eEl.value = e;
}

function validateParams(p, needFiles) {
  if (!p.platform) return "请选择平台";
  if (!p.period && !(p.s_date && p.e_date)) return "请填写账期，或同时填写开始与结束日期";
  if (needFiles && !p.file_paths) return "请填写账单文件或目录路径";
  if (!p.shop_code) return "请填写店铺编码";
  return "";
}

/* ---------------- 占位卡 ---------------- */

/** 四个暂缓能力共用一张占位卡：说清「暂缓原因 / 原本做什么 / 启用条件」，
 *  不留空白页（空白会被当成 bug），也不放假按钮（点了没反应更糟）。 */
const PLACEHOLDER_META = {
  match: {
    title: "SKU 对账",
    does: "把账单里的货款行与巨益出入库单（B2C配货单 / B2C退换货通知单 / 退款单）按订单号逐单匹配，"
        + "再把货款按结算金额分摊到每个 sku，标出「已匹配 / 未匹配」。",
    needs: "需要连巨益 ADB 取数（环境变量凭据），并决定匹配明细的落盘形态"
         + "（方案约束：数据库不存对账明细，优先落 results/ 下的 parquet）。",
  },
  cost: {
    title: "成本匹配",
    does: "给匹配结果补商品主数据（款号 / 品类 / 年份 / 季节 / 品牌 / 吊牌价）与成本单价，"
        + "按三分处理意见（结转成本 / 冲成本 / 冲发出商品 / 成本不做处理）结转，"
        + "输出店铺透视、款号透视与两种口径的毛利率。",
    needs: "依赖 SKU 对账的匹配明细；另需成本单价表（按 sku 或按款号取价）。",
  },
  writeback: {
    title: "数据库回传",
    does: "把已匹配的巨益单据回写生产库 mid_jedz_datas，标记「已对账」与对账周期，"
        + "并留成功/失败行数与失败清单。",
    needs: "会修改生产数据库，必须先决定结果来源（当前无匹配明细）与账期标签口径"
         + "（bill_period 为中文标签，如 2026年06月-2026年06月）。",
  },
  chuanyun: {
    title: "氚云对接",
    does: "把对账结果推送氚云，生成资金流水记录。",
    needs: "账单整理已导出「氚云导入模板」sheet（店铺名称 / 业务类别 / 资金方向 / 金额），"
         + "当前靠手工导入；自动推送需接氚云开放接口与表单字段映射。",
  },
};

function renderPlaceholder(key) {
  const host = $(`ph-${key}`);
  if (!host) return;
  const m = PLACEHOLDER_META[key] || { title: key, does: "", needs: "" };
  host.innerHTML = `
    <div class="statusbar warn"><span class="dot"></span><span>暂缓开放 · 当前版本不提供该能力</span></div>
    <h2>${esc(m.title)}</h2>
    <p class="hint"><b>这个环节原本做什么：</b>${esc(m.does)}</p>
    <p class="hint"><b>启用前需要决定：</b>${esc(m.needs)}</p>
    <details>
      <summary>现在能做什么</summary>
      <pre>· 账单整理：各平台账单 → 统一口径（订单编号/资金日期/业务类别/收款/退款）
            导出 Excel（含氚云导入模板 / 账单透视 / 账单明细）+ parquet
· 店铺映射：数据库唯一保留的数据，可在「数据源与规则」页导入与查询
· 平台解析规则：rules/{platform}.json 文件态，可在「数据源与规则」页增删改

数据落位（2026-11-14 方案改版）
· 数据库      只存店铺映射，不存账单结果，也不存 SKU 对账明细
· 账单结果    results/{账期}_{平台}_账单整理_{店铺}_{时间戳}/
· 作业留痕    logs/jobs.jsonl
· 解析规则    rules/{platform}.json</pre>
    </details>`;
}

/* ---------------- 作业 ---------------- */

let activeJobId = null;
let pollTimer = null;

async function startJob(params, onDone) {
  const out = await apiPost(`${BASE}/jobs`, { params });
  activeJobId = (out.job && (out.job.job_id || out.job.id)) || out.job_id || null;
  toast("作业已开始", "ok");
  startPolling(onDone);
  return activeJobId;
}

function startPolling(onDone) {
  stopPolling();
  pollTimer = setInterval(() => void pollJob(onDone), 1300);
  void pollJob(onDone);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

const TERMINAL = ["success", "failed", "skipped", "cancelled", "timed_out", "partial", "deferred"];

async function pollJob(onDone) {
  const host = $("job-progress");
  try {
    let job = null;
    if (activeJobId) {
      const out = await apiGet(`${BASE}/jobs/${activeJobId}`);
      job = out.job || out;
    } else {
      const out = await apiGet(`${BASE}/jobs?limit=1`);
      const list = out.jobs || out.items || out || [];
      job = Array.isArray(list) ? list[0] : null;
      if (job) activeJobId = job.job_id || job.id || null;
    }
    if (!job) {
      if (host) host.innerHTML = `<div class="empty">没有进行中的作业。</div>`;
      return;
    }
    const pct = Number(job.progress_pct || job.progress?.pct || 0);
    const msg = humanize(job.message || job.progress?.label || "");
    const st = String(job.status || "").toLowerCase();
    if (host) {
      host.innerHTML = `
        <div class="progress-row">
          <div class="progress-bar"><div class="progress-fill" style="width:${Math.min(100, Math.max(0, pct))}%"></div></div>
          <div class="progress-label">${statusTag(job.status)} ${esc(msg)} · ${pct}%</div>
        </div>
        ${job.batch_id || job.params?.batch_id ? `<div class="hint">批次 <code>${esc(job.batch_id || job.params?.batch_id)}</code></div>` : ""}
        <details><summary>运行详情</summary><pre>${esc((job.logs || []).slice(-120).map(humanize).join("\n") || "（暂无日志）")}</pre></details>`;
    }
    if (TERMINAL.includes(st)) {
      stopPolling();
      const kind = st === "success" ? "ok" : (st === "failed" || st === "timed_out" ? "danger" : "warn");
      // 只对本页发起的作业弹终态 toast（onDone 由 startJob 传入）；
      // 启动时 pollJob(null) 捡到的是历史旧作业，不该每次开页都弹一次旧失败
      // （2026-11-16 实测：开设置页弹出上一次 taobao 失败作业的报错）。
      if (onDone) toast(`${msg || st}`, kind);
      activeJobId = null;
      if (onDone) await onDone(job);
      void loadJobsPage();
    }
  } catch (e) {
    // 轮询失败不打扰用户，只在详情里体现
    if (host && !host.querySelector(".progress-row")) {
      host.innerHTML = `<div class="empty">读取作业状态失败：${esc(humanize(e.message || e))}</div>`;
    }
  }
}

/* ---------------- 各页加载 ---------------- */

async function loadHome() {
  const [ov, st] = await Promise.all([
    tool("recon_workbench__overview", { days: 90 }).catch((e) => ({ data: {}, error: e.message })),
    tool("recon_workbench__data_status", {}).catch(() => ({ data: {} })),
  ]);
  const d = ov.data || {};
  const t = d.totals || {};
  const counts = (st.data || {}).counts || {};

  renderMetrics($("home-metrics"), [
    { label: "账单批次", value: (t.bill_batches || 0).toLocaleString("zh-CN"), hint: `合计 ${counts.bill_rows || 0} 行` },
    { label: "账单行", value: (t.bill_rows || 0).toLocaleString("zh-CN"), tone: "acid", hint: "落 parquet，不入数据库" },
    { label: "货款行", value: (t.goods_rows || 0).toLocaleString("zh-CN"), hint: "SKU对账的输入（暂缓）" },
    { label: "店铺映射", value: (t.shop_map || 0).toLocaleString("zh-CN"), tone: "ok", hint: "数据库唯一保留的数据" },
    { label: "解析规则", value: (t.rules || 0).toLocaleString("zh-CN"), hint: "rules/*.json 文件态" },
    { label: "作业留痕", value: (counts.job_log || 0).toLocaleString("zh-CN"), hint: "logs/jobs.jsonl" },
  ]);

  const latest = d.latest_job;
  const bar = $("home-status");
  const txt = $("home-status-text");
  if (ov.error) {
    bar.className = "statusbar warn";
    txt.textContent = "还没跑过账单整理。在会话里把平台、店铺编码、账期与账单路径发给 AI 即可开始。";
  } else if (!latest) {
    bar.className = "statusbar warn";
    txt.textContent = "还没有账单整理记录。让 AI 整理一次后，这里会显示批次与货款合计。";
  } else {
    bar.className = `statusbar ${latest.status === "success" ? "ok" : (latest.status === "failed" ? "danger" : "warn")}`;
    txt.textContent = `最近批次 ${latest.platform || "-"}/${latest.shop_code || "-"}/${latest.period || "-"}：${humanize(latest.message || latest.status)}`;
  }

  renderTable($("home-capabilities"), (d.capabilities || []).map((c) => ({
    能力: c.name, 状态: c.status === "active" ? "可用" : "暂缓开放", 说明: c.note,
  })), {
    empty: "读取能力状态失败",
    render: { 状态: (v) => `<span class="tag ${v === "可用" ? "ok" : "warn"}">${esc(v)}</span>` },
  });

  renderTable($("home-periods"), (d.periods || []).map((p) => ({
    账期: p.period, 批次数: p.batches, 账单行数: p.detail_rows,
    货款收款元: p.goods_receipt_yuan, 货款退款元: p.goods_refund_yuan, 货款净额元: p.goods_net_yuan,
  })), { empty: "还没有整理过账单" });

  renderTable($("home-platforms"), (d.platforms || []).map((p) => ({
    平台: p.platform, 批次数: p.batches, 账单行数: p.detail_rows,
  })), { empty: "还没有平台数据" });

  renderTable($("home-jobs"), (d.recent_batches || []).map((b) => ({
    账期: b.period, 平台: b.platform, 店铺: b.shop_code, 状态: b.status,
    账单行: b.detail_rows, 货款行: b.goods_rows, 货款净额元: b.goods_net_yuan,
    时间: b.created_at, 批次: b.batch_id,
  })), {
    empty: "还没有账单整理批次",
    limit: 10,
    render: { 状态: (v) => statusTag(v) },
  });
}

async function loadPlatformsInto(selects) {
  const res = await tool("recon_workbench__platforms", {}).catch(() => ({ data: [] }));
  const rows = rowsOf(res);
  selects.forEach((sel) => {
    if (!sel) return;
    const keepAll = sel.querySelector('option[value=""]');
    sel.innerHTML = "";
    if (keepAll) sel.appendChild(keepAll);
    rows.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.platform;
      o.textContent = `${p.display_name}（${p.platform}）`;
      sel.appendChild(o);
    });
  });
}

async function loadShopsIntoDatalist() {
  const res = await tool("recon_workbench__shops", { limit: 500 }).catch(() => ({ data: [] }));
  const dl = $("shop-list");
  if (!dl) return;
  dl.innerHTML = rowsOf(res).map((s) => `<option value="${esc(s.shop_code)}">${esc(s.shop_name)}</option>`).join("");
}

async function loadBillPage() {
  await Promise.all([
    loadPlatformsInto([$("bill-platform")]),
    loadShopsIntoDatalist(),
  ]);
  await loadBillBatches();
  await loadBillRows();
}

/** 批次产物下载链接组：downloads 副本（宿主 /files/downloads/ 同源可下）优先，
 *  无副本时回落展示本地路径（可复制到资源管理器打开）。 */
function excelLinks(b) {
  const dls = Array.isArray(b.downloads) ? b.downloads : [];
  if (dls.length) {
    return dls.map((d) => {
      const label = String(d.name || "Excel").replace(/\.xlsx$/i, "");
      const short = label.length > 26 ? `${label.slice(0, 24)}…` : label;
      return `<a class="dl-link" href="${esc(d.url)}" download="${esc(d.name)}" title="${esc(d.name)}">${esc(short)}</a>`;
    }).join("");
  }
  const files = Array.isArray(b.excel_list) && b.excel_list.length ? b.excel_list : (b.excel ? [b.excel] : []);
  if (!files.length) return `<span class="hint">未导出</span>`;
  return files.map((p) => `<code class="path" title="${esc(p)}">${esc(String(p).split(/[\\/]/).pop())}</code>`).join("<br>");
}

async function loadBillBatches() {
  const res = await tool("recon_workbench__bill_batches", { limit: 30 }).catch((e) => ({ data: {}, error: e.message }));
  renderTable($("bill-batches"), rowsOf(res).map((b) => ({
    账期: b.period, 平台: b.platform, 店铺编码: b.shop_code, 店铺名称: b.shop_name,
    状态: b.status, 账单行: b.detail_rows, 货款行: b.goods_rows,
    货款收款元: b.goods_receipt_yuan, 货款退款元: b.goods_refund_yuan, 货款净额元: b.goods_net_yuan,
    Excel文件: b,
    时间: b.created_at, 批次: b.batch_id,
  })), {
    empty: "还没有账单整理批次。填好参数点「开始整理账单」，或先「试跑看映射」。",
    limit: 30,
    render: {
      状态: (v) => statusTag(v),
      Excel文件: (b) => excelLinks(b || {}),
    },
  });
}

async function loadBillRows() {
  const batchId = $("bill-rows-batch")?.value.trim() || "";
  const res = await tool("recon_workbench__query_bill_rows", { batch_id: batchId, limit: 200 })
    .catch((e) => ({ data: {}, error: e.message }));
  const meta = (res.data || {}).batch || {};
  renderTable($("bill-rows"), rowsOf(res).map((r) => ({
    平台: r.platform, 店铺编码: r.shop_code, 账期: r.period,
    订单编号: r.order_no, 资金日期: r.fund_date, 业务类别: r.biz_category,
    收款元: r.receipt_yuan, 退款元: r.refund_yuan, 净额元: r.net_yuan,
    来源文件: r.source_file,
  })), {
    empty: batchId
      ? "该批次没有账单明细（可能未整理过，或批次ID不对）"
      : "还没有账单整理结果。结果只落文件，不进数据库。",
    limit: 200,
  });
  const host = $("bill-rows");
  if (host) {
    // 复用同一个提示节点：旧版每次查询 insertBefore 一个新 <p>，
    // 反复查询后提示堆成一摞（2026-11-16 页面优化）。
    let tip = document.getElementById("bill-rows-tip");
    if (!tip) {
      tip = document.createElement("p");
      tip.id = "bill-rows-tip";
      tip.className = "hint";
      host.parentNode.insertBefore(tip, host);
    }
    if (meta && meta.batch_id) {
      tip.textContent = `当前批次 ${meta.batch_id} · ${meta.detail_rows || 0} 行 · 货款净额 ${(meta.goods_net_yuan || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`;
      tip.style.display = "";
    } else {
      tip.style.display = "none";
    }
  }
}

async function loadSourcesPage() {
  await loadPlatformsInto([$("rule-platform")]);
  await loadDbPanel();
  await loadShops();
  await loadRules();
}

async function loadDbPanel() {
  const host = $("db-detail");
  const res = await tool("recon_workbench__db_status", {}).catch((e) => ({ ok: false, data: {}, summary: e.message, error: { message: e.message } }));
  const d = res.data || {};
  const conn = $("db-conn");
  // res.ok 恒为 true（引导态工具）；真实连通性看 data.ok
  const live = !!d.ok;
  if (live) {
    if (conn) { conn.className = "conn ok"; conn.textContent = "数据库已连接"; }
  } else {
    const missing = (d.missing_env || []).length;
    if (conn) { conn.className = `conn ${missing ? "warn" : "danger"}`; conn.textContent = missing ? "数据库未配置" : "数据库未连通"; }
  }
  host.innerHTML = `
    <div class="statusbar ${live ? "ok" : ((d.missing_env || []).length ? "warn" : "danger")}">
      <span class="dot"></span><span>${esc(humanize(res.summary || (live ? "已连接" : "未就绪")))}</span>
    </div>
    <p class="hint">账单整理<b>不需要</b>数据库；巨益 ADB 只在 SKU对账与数据库回传启用后才需要（当前暂缓）。</p>
    <p class="hint">数据库账号口令一律读系统环境变量，插件不保存、页面不显示。</p>
    <details>
      <summary>需要配置的环境变量</summary>
      <pre>RECON_ADB_HOST      数据库地址（必填）
RECON_ADB_USER      用户名（必填）
RECON_ADB_PASSWORD  口令（必填）
RECON_ADB_PORT      端口（选填，默认 3306）
RECON_ADB_DATABASE  库名（选填，默认 xianxia）

已检测到：${esc((d.present_env || []).join(", ") || "（无）")}
缺失：${esc((d.missing_env || []).join(", ") || "（无）")}</pre>
    </details>`;
}

async function loadShops() {
  const kw = $("shop-keyword")?.value.trim() || "";
  const res = await tool("recon_workbench__shops", { keyword: kw, limit: 100 }).catch(() => ({ data: [] }));
  renderTable($("shop-preview"), rowsOf(res).map((s) => ({
    店铺编码: s.shop_code, 店铺ID: s.shop_id, 店铺名称: s.shop_name,
    平台: s.platform, 氚云名称: s.chuanyun_name, 启用: s.active ? "是" : "否",
    版本: s.version, 导入时间: s.imported_at,
  })), { empty: "还没有导入店铺映射。店铺映射是数据库里唯一保留的数据。", limit: 100 });
  await loadShopsIntoDatalist();
}

async function loadRules() {
  const platform = $("rule-platform")?.value || "";
  const kind = $("rule-kind")?.value || "";
  const res = await tool("recon_workbench__query_rules", { platform, limit: 500 }).catch(() => ({ data: [] }));
  let rows = rowsOf(res);
  if (kind) rows = rows.filter((r) => r.rule_kind === kind);
  renderTable($("rule-table"), rows.map((r) => ({
    平台: r.platform, 类型: r.rule_kind, 键: r.rule_key, 值: r.rule_value,
    启用: r.active ? "是" : "否", 备注: r.note, 更新时间: r.updated_at,
  })), {
    empty: "还没有自定义规则（rules/ 目录下暂无 JSON）。未配规则时使用内置兜底映射；新增平台可只靠规则文件接管，不必写代码。",
    limit: 500,
  });
}

async function loadJobsPage() {
  await pollJob(null);
  const res = await tool("recon_workbench__query_job_log", { limit: 60 }).catch(() => ({ data: [] }));
  renderTable($("job-log"), rowsOf(res).map((j) => ({
    批次: j.batch_id, 阶段: j.stage, 平台: j.platform, 店铺: j.shop_code, 账期: j.period,
    状态: j.status, 账单行: j.bill_rows, 耗时秒: j.duration_sec, 时间: j.created_at,
    说明: humanize(j.message),
  })), {
    empty: "还没有作业记录",
    render: { 状态: (v) => statusTag(v) },
    limit: 60,
  });
}

/* ---------------- 设置页（2026-11-16：自定义存储路径 + 清空历史） ---------------- */

async function loadSettingsPage() {
  const res = await tool("recon_workbench__get_settings", {});
  const d = res.data || {};
  const input = $("set-results-dir");
  if (input) input.value = d.results_dir || "";
  const cur = $("set-results-current");
  if (cur) {
    cur.textContent = d.is_custom
      ? `当前生效：${d.effective_dir}（自定义）`
      : `当前生效：${d.effective_dir}（默认）`;
  }
  const out = $("set-save-result");
  if (out) { out.style.display = "none"; out.textContent = ""; }
}

/** 保存存储路径（2026-11-16 bug 修复：AI 原生化改版误删了编辑入口，补回）。
 *  restore=true 时传空串 = 恢复默认目录；后端校验绝对路径 + 可创建可写。 */
async function saveResultsDir(restore) {
  const btn = $(restore ? "btn-reset-settings" : "btn-save-settings");
  const input = $("set-results-dir");
  const out = $("set-save-result");
  const dir = restore ? "" : String((input && input.value) || "").trim();
  if (btn) btn.disabled = true;
  try {
    const res = await tool("recon_workbench__save_settings", { results_dir: dir });
    const d = res.data || {};
    toast(res.summary || "已保存", "ok");
    if (input) input.value = d.results_dir || "";
    const cur = $("set-results-current");
    if (cur) {
      cur.textContent = d.is_custom
        ? `当前生效：${d.effective_dir}（自定义）`
        : `当前生效：${d.effective_dir}（默认）`;
    }
    if (out) {
      out.style.display = "";
      out.textContent = res.summary || "已保存";
    }
  } catch (err) {
    toast(err.message || "保存失败", "err");
    if (out) {
      out.style.display = "";
      out.textContent = `保存失败：${err.message || err}`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 清空历史：两步确认（WebView iframe 里 window.confirm 可能被拦，改用按钮态）。
 *  第一次点击变红字「确认清空？再点一次执行」，5 秒内再点才真正调用。 */
let clearArmed = null;
async function clearHistoryFlow(btn) {
  if (!clearArmed || clearArmed.btn !== btn) {
    if (clearArmed) { clearTimeout(clearArmed.timer); resetClearBtn(clearArmed.btn); }
    const original = btn.textContent;
    btn.textContent = "确认清空？再点一次执行";
    btn.classList.add("armed");
    clearArmed = {
      btn,
      timer: setTimeout(() => { resetClearBtn(btn); clearArmed = null; }, 5000),
    };
    clearArmed.original = original;
    toast("将删除所有批次结果与作业留痕（保留店铺映射/规则/设置），5 秒内再点一次确认", "warn");
    return;
  }
  clearTimeout(clearArmed.timer);
  resetClearBtn(btn);
  clearArmed = null;
  btn.disabled = true;
  try {
    const res = await tool("recon_workbench__clear_history", { confirm: "yes" });
    toast(res.summary || "已清空历史", "ok");
    const out = $("clear-history-result");
    if (out) {
      const d = res.data || {};
      out.style.display = "";
      out.className = "";
      out.innerHTML = `<p class="hint">${esc(res.summary || "")}</p>` +
        ((d.errors || []).length ? `<p class="hint">删除失败项：${esc(d.errors.join("；"))}</p>` : "");
    }
    // 刷新受影响页面数据（总览页按钮就地刷新本页；批次表/明细同步重取）
    await loadHome().catch(() => {});
    await loadBillBatches().catch(() => {});
    await loadBillRows().catch(() => {});
  } finally {
    btn.disabled = false;
  }
}
function resetClearBtn(btn) {
  btn.textContent = "清空历史";
  btn.classList.remove("armed");
}

/** 占位页：只渲染占位卡，不发任何取数请求（没有对应数据）。 */
function placeholderLoader(key) {
  return () => { renderPlaceholder(key); };
}

const PAGE_LOADERS = {
  home: loadHome,
  bill: loadBillPage,
  match: placeholderLoader("match"),
  cost: placeholderLoader("cost"),
  writeback: placeholderLoader("writeback"),
  chuanyun: placeholderLoader("chuanyun"),
  sources: loadSourcesPage,
  jobs: loadJobsPage,
  settings: loadSettingsPage,
};

/* ---------------- 事件绑定 ---------------- */

function bind(id, handler) {
  const el = $(id);
  if (el) el.addEventListener("click", () => Promise.resolve(handler()).catch((e) => toast(humanize(e.message || e), "danger")));
}

function bindAll() {
  $("btn-use-in-chat")?.addEventListener("click", useInChat);

  document.querySelectorAll(".ask-ai").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const [label, body] = await buildAskPayload(btn.dataset.ask);
        askAI(label, body);
      } catch (e) {
        toast(humanize(e.message || e), "danger");
      } finally {
        btn.disabled = false;
      }
    });
  });

  // 占位卡启动即渲染，避免用户先看到空白再被填上
  ["match", "cost", "writeback", "chuanyun"].forEach(renderPlaceholder);

  // 【2026-11-16 AI 原生化】人工操作入口（整理表单/试跑/导入店铺表/规则编辑/
  // 设置保存）已移除；对应能力由 AI 通过 MCP 工具执行：
  //   start_recon / preview_bill / load_shop_file / upsert_rule / delete_rule /
  //   save_settings / clear_history(confirm=yes，AI 须先向用户确认)
  // 例外（2026-11-16 用户需求）：对账总览页保留「清空历史」人工按钮，
  // 两步确认（clearHistoryFlow）后调 clear_history(confirm=yes)。
  const clearBtn = $("btn-clear-history");
  if (clearBtn) clearBtn.addEventListener("click", () => clearHistoryFlow(clearBtn));
  // 设置页存储路径编辑（2026-11-16 bug 修复：改版误删入口后补回）
  bind("btn-save-settings", () => saveResultsDir(false));
  bind("btn-reset-settings", () => saveResultsDir(true));
  bind("btn-batch-refresh", () => loadBillBatches());
  bind("btn-shop-refresh", () => loadShops());
  bind("btn-rule-refresh", () => loadRules());
  bind("btn-job-refresh", () => loadJobsPage());
}

/* ---------------- 启动 ---------------- */

(async function boot() {
  bindAll();
  applyHash();
  try {
    const res = await tool("recon_workbench__db_status", {});
    // db_status 是引导态工具：action 恒 ok，真实连通性在 data.ok
    const conn = $("db-conn");
    if (conn) {
      const live = !!(res.data && res.data.ok);
      // 改版后账单整理不依赖数据库，未连通只作提示不作警告
      conn.className = `conn ${live ? "ok" : "warn"}`;
      conn.textContent = live ? "数据库已连接" : "数据库未配置（账单整理不受影响）";
    }
  } catch {
    const conn = $("db-conn");
    if (conn) { conn.className = "conn warn"; conn.textContent = "数据库未配置（账单整理不受影响）"; }
  }
  void pollJob(null);
})();
