/* RetailPulse dashboard — plain JS, no build step. Talks only to the app's own REST API.
   Security note: every piece of dynamic text (including LLM-generated SQL) is inserted with
   textContent / createTextNode, never innerHTML, so nothing returned by the API can inject markup. */
"use strict";

const fmtInt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmtDec = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const PALETTE = ["#2f5bea", "#7a5cff", "#0ea5a4", "#f59e0b", "#ef4444", "#10b981", "#ec4899", "#64748b"];

Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
Chart.defaults.color = "#5d6b82";
Chart.defaults.maintainAspectRatio = false;

/* ------------------------------------------------------------------ helpers */
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}
const $ = (id) => document.getElementById(id);
const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const key = sessionStorage.getItem("rp_api_key");
  if (key && options.method === "POST") headers["X-API-Key"] = key;
  let res;
  try {
    res = await fetch(path, { ...options, headers });
  } catch (e) {
    throw new ApiError(0, "Could not reach the RetailPulse API. Is the server running?");
  }
  let body = null;
  try { body = await res.json(); } catch (e) { /* non-JSON error body */ }
  if (!res.ok) throw new ApiError(res.status, (body && body.error && body.error.message) || `Request failed (${res.status})`);
  return body;
}

const charts = {};
function makeChart(canvasId, config) {
  if (charts[canvasId]) charts[canvasId].destroy();
  charts[canvasId] = new Chart($(canvasId), config);
}

function kpi(label, value, sub, tone) {
  return h("div", { class: "kpi" + (tone ? " " + tone : "") },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : "");
}

function notice(node, kind, text) {
  node.className = "notice " + kind;
  node.textContent = text;
  node.hidden = false;
}

/* ------------------------------------------------------------------ tabs */
const loaded = new Set();
const loaders = { overview: loadOverview, forecast: loadForecastTab, quality: loadQuality, ask: initAsk, security: initSecurity };

function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.id !== "tab-" + name; });
  if (!loaded.has(name)) { loaded.add(name); loaders[name](); }
  history.replaceState(null, "", "#" + name);
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ------------------------------------------------------------------ health */
async function loadHealth() {
  const setPill = (id, text, cls) => { const p = $(id); p.textContent = text; p.className = "pill " + cls; };
  try {
    const hl = await api("/health");
    setPill("pill-db", "database " + hl.database, hl.database === "ok" ? "ok" : "bad");
    setPill("pill-ro", "read-only role " + hl.readonly_database, hl.readonly_database === "ok" ? "ok" : "bad");
    setPill("pill-llm", hl.llm_configured ? "LLM configured" : "LLM not configured", hl.llm_configured ? "ok" : "off");
  } catch (e) {
    setPill("pill-db", "API unreachable", "bad");
  }
}

/* ------------------------------------------------------------------ overview */
let topMetric = "revenue";

async function loadOverview() {
  try {
    const [s, cats, months] = await Promise.all([
      api("/analytics/summary"), api("/analytics/revenue-by-category"), api("/analytics/monthly-revenue")]);
    const kp = $("kpis"); clear(kp);
    kp.append(
      kpi("Total revenue", fmtInt.format(s.total_revenue), `${s.first_date} → ${s.last_date}`),
      kpi("Units sold", fmtInt.format(s.total_units), "across all items"),
      kpi("Products", fmtInt.format(s.items), "in the catalogue"),
      kpi("Daily sales records", fmtInt.format(s.sales_rows), "after validation"));

    makeChart("chart-category", {
      type: "doughnut",
      data: { labels: cats.map((c) => c.category), datasets: [{ data: cats.map((c) => c.revenue), backgroundColor: PALETTE, borderWidth: 2 }] },
      options: { plugins: { legend: { position: "right" }, tooltip: { callbacks: {
        label: (ctx) => ` ${ctx.label}: ${fmtInt.format(ctx.parsed)} (${cats[ctx.dataIndex].revenue_share_pct}%)` } } } },
    });

    makeChart("chart-monthly", {
      type: "line",
      data: { labels: months.map((m) => m.month.slice(0, 7)), datasets: [{
        label: "Revenue", data: months.map((m) => m.revenue), borderColor: PALETTE[0], backgroundColor: "rgba(47,91,234,.12)",
        fill: true, tension: 0.25, pointRadius: 2 }] },
      options: { plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: (ctx) => ` Revenue: ${fmtInt.format(ctx.parsed.y)}`,
        afterLabel: (ctx) => { const g = months[ctx.dataIndex].mom_growth_pct;
          return g === null ? "" : ` vs previous month: ${g > 0 ? "+" : ""}${g}%`; } } } },
        scales: { y: { ticks: { callback: (v) => fmtInt.format(v) } } } },
    });
    await loadTopItems();
  } catch (e) {
    const box = h("div", { class: "notice error" }, e.message + " — load data first (see README) and refresh.");
    $("kpis").replaceChildren(box);
  }
}

async function loadTopItems() {
  const rows = await api(`/analytics/top-items?limit=10&metric=${topMetric}`);
  const key = topMetric === "revenue" ? "revenue" : "units_sold";
  makeChart("chart-top", {
    type: "bar",
    data: { labels: rows.map((r) => r.name), datasets: [{ data: rows.map((r) => r[key]), backgroundColor: PALETTE[0], borderRadius: 5 }] },
    options: { indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: {
      afterLabel: (ctx) => ` ${rows[ctx.dataIndex].category}` } } },
      scales: { x: { ticks: { callback: (v) => fmtInt.format(v) } } } },
  });
}
$("metric-toggle").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button"); if (!btn) return;
  topMetric = btn.dataset.metric;
  $("metric-toggle").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
  loadTopItems();
});

/* ------------------------------------------------------------------ forecast */
async function loadForecastTab() {
  try {
    const items = await api("/items");
    const sel = $("item-select"); clear(sel);
    items.forEach((it) => sel.append(h("option", { value: it.id }, `${it.name} (${it.category})`)));
    sel.addEventListener("change", () => loadForecast(sel.value));
    if (items.length) loadForecast(items[0].id);
  } catch (e) { notice($("forecast-msg"), "error", e.message); }
}

async function loadForecast(itemId) {
  const msg = $("forecast-msg"); msg.hidden = true;
  try {
    const [fc, hist] = await Promise.all([api(`/forecast/${itemId}`), api(`/analytics/item-sales/${itemId}?days=90`)]);
    const labels = [...hist.map((p) => p.sale_date), ...fc.forecast.map((p) => p.date)];
    const actual = [...hist.map((p) => p.units_sold), ...fc.forecast.map(() => null)];
    const pred = [...hist.map(() => null), ...fc.forecast.map((p) => Math.round(p.predicted_units * 100) / 100)];
    makeChart("chart-forecast", {
      type: "line",
      data: { labels, datasets: [
        { label: "Actual units sold (last 90 records)", data: actual, borderColor: PALETTE[0], pointRadius: 0, borderWidth: 2, tension: 0.15 },
        { label: `Forecast (next ${fc.horizon_days} days)`, data: pred, borderColor: "#ef4444", borderDash: [6, 4], pointRadius: 0, borderWidth: 2.5, tension: 0.2 }] },
      options: { interaction: { mode: "index", intersect: false }, scales: { x: { ticks: { maxTicksLimit: 10 } } } },
    });

    const m = fc.model;
    const kp = $("model-kpis"); clear(kp);
    if (m) {
      kp.append(
        kpi("Model MAE", fmtDec.format(m.holdout_mae_units_per_day), "units / day", m.beats_seasonal_naive ? "good" : "bad"),
        kpi("WAPE", fmtDec.format(m.holdout_wape_pct) + "%", "error vs demand"),
        kpi("Seasonal-naive MAE", fmtDec.format(m.baseline_seasonal_naive_mae), "same weekday last week"),
        kpi("Recent-mean MAE", fmtDec.format(m.baseline_mean_mae), "average of recent days"));
      $("model-periods").textContent = `Trained on ${m.train_period}; evaluated on ${m.holdout_period}. ` +
        (m.beats_seasonal_naive ? "The model beats the seasonal-naive baseline for this item." : "The model does NOT beat the seasonal-naive baseline for this item.");
    } else { $("model-periods").textContent = "No evaluation record stored for this item."; }
    $("forecast-caveat").textContent = fc.caveat;
    $("forecast-details").hidden = false;
  } catch (e) {
    $("forecast-details").hidden = true;
    if (charts["chart-forecast"]) { charts["chart-forecast"].destroy(); delete charts["chart-forecast"]; }
    notice(msg, e.status === 404 ? "warn" : "error",
      e.status === 404 ? `No forecast for this item: ${e.message}. (Items with no recent sales are skipped rather than forecast from no data.)` : e.message);
  }
}

/* ------------------------------------------------------------------ ask (NL -> SQL) */
const SAMPLE_QUESTIONS = [
  "Which 5 items had the highest revenue in 2023?",
  "What are the total units sold per category?",
  "What percentage of total revenue does the Bakery category represent?",
  "What was the total revenue in January 2024?",
  "Which items have never had a single sale recorded?",
];

function initAsk() {
  const keyBox = $("api-key");
  keyBox.value = sessionStorage.getItem("rp_api_key") || "";
  keyBox.addEventListener("input", () => sessionStorage.setItem("rp_api_key", keyBox.value.trim()));
  const chips = $("q-chips");
  SAMPLE_QUESTIONS.forEach((q) => chips.append(h("button", { class: "chip", type: "button", onclick: () => { $("q-input").value = q; runQuery(); } }, q)));
  $("q-run").addEventListener("click", runQuery);
  $("q-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); runQuery(); } });
}

function renderTable(table, columns, rows) {
  clear(table);
  const numeric = columns.map((_, i) => rows.length > 0 && rows.every((r) => r[i] === null || /^-?\d+(\.\d+)?$/.test(String(r[i]))));
  table.append(h("thead", {}, h("tr", {}, columns.map((c, i) => h("th", { class: numeric[i] ? "num" : "" }, c)))));
  table.append(h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v, i) => h("td", { class: numeric[i] ? "num" : "" }, v === null ? "—" : v))))));
}

async function runQuery() {
  const question = $("q-input").value.trim();
  const msg = $("q-msg"), result = $("q-result"), btn = $("q-run");
  msg.hidden = true; result.hidden = true;
  if (question.length < 3) { notice(msg, "warn", "Type a question first (at least 3 characters)."); return; }
  btn.disabled = true; btn.textContent = "Thinking…";
  try {
    const r = await api("/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
    $("q-sql").textContent = r.sql;
    $("q-timings").textContent = `LLM ${r.timings.llm_ms} ms · validate ${r.timings.validate_ms} ms · database ${r.timings.db_ms} ms`;
    const notes = $("q-notes"); clear(notes);
    r.notes.forEach((n) => notes.append(h("li", {}, n)));
    renderTable($("q-table"), r.columns, r.rows);
    $("q-count").textContent = `${r.row_count} row${r.row_count === 1 ? "" : "s"} shown` + (r.truncated ? ` — more rows matched than the limit of ${r.limit_applied}` : "");
    $("q-limit-hint").textContent = `Row limit: ${r.limit_applied}. The LIMIT in the SQL above is deliberately one higher, so the API can tell whether more rows exist without a second query.`;
    result.hidden = false;
  } catch (e) {
    if (e.status === 503) notice(msg, "info", "Natural-language querying isn't enabled on this server yet: no LLM_API_KEY is configured. Add one to .env and restart. You can still try the validator in the Security Lab tab.");
    else if (e.status === 422 && e.message.startsWith("query rejected")) notice(msg, "warn", "Blocked by the safety validator — nothing was run. " + e.message);
    else if (e.status === 401) notice(msg, "error", "This server requires an API key. Enter it in the X-API-Key box.");
    else if (e.status === 429) notice(msg, "warn", e.message);
    else notice(msg, "error", e.message);
  } finally { btn.disabled = false; btn.textContent = "Run query"; }
}

/* ------------------------------------------------------------------ security lab */
const ATTACKS = [
  { label: "DROP TABLE", sql: "DROP TABLE sales", attack: true },
  { label: "Stacked DELETE", sql: "SELECT 1; DELETE FROM sales", attack: true },
  { label: "Hidden in a comment", sql: "SELECT name FROM items -- ; DROP TABLE items", attack: true },
  { label: "Read password hashes", sql: "SELECT * FROM pg_shadow", attack: true },
  { label: "Read the audit table", sql: "SELECT question FROM nl_query_log", attack: true },
  { label: "Hang the server", sql: "SELECT pg_sleep(30)", attack: true },
  { label: "Read a server file", sql: "SELECT pg_read_file('/etc/passwd')", attack: true },
  { label: "Legit: revenue by category", sql: "SELECT i.category, SUM(s.revenue) AS revenue FROM items i JOIN sales s ON s.item_id = i.id GROUP BY i.category ORDER BY revenue DESC" },
  { label: "Legit: no LIMIT given", sql: "SELECT name, category FROM items" },
];

function initSecurity() {
  const chips = $("v-chips");
  ATTACKS.forEach((a) => chips.append(h("button", { class: "chip" + (a.attack ? " attack" : ""), type: "button",
    onclick: () => { $("v-input").value = a.sql; runValidate(); } }, a.label)));
  $("v-run").addEventListener("click", runValidate);
}

async function runValidate() {
  const sql = $("v-input").value.trim();
  const out = $("v-result");
  if (!sql) { out.hidden = true; return; }
  try {
    const r = await api("/query/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql }) });
    out.className = "verdict " + (r.ok ? "accepted" : "rejected");
    clear(out);
    if (r.ok) {
      out.append(h("span", { class: "badge" }, "ACCEPTED"),
        h("p", {}, `Safe to run. Row cap enforced at ${r.limit_applied}. This is exactly what would execute:`),
        h("pre", { class: "code" }, r.rewritten_sql));
      r.notes.forEach((n) => out.append(h("p", { class: "fine" }, "• " + n)));
    } else {
      out.append(h("span", { class: "badge" }, "REJECTED"), h("span", { class: "reason" }, r.reject_reason),
        h("p", {}, r.detail), h("p", { class: "fine" }, "Nothing was executed — this check never touches the database."));
    }
    out.hidden = false;
  } catch (e) {
    out.className = "verdict rejected"; out.textContent = e.message; out.hidden = false;
  }
}

/* ------------------------------------------------------------------ data quality */
function barChart(canvasId, summary, color) {
  const entries = Object.entries(summary || {}).sort((a, b) => b[1] - a[1]);
  makeChart(canvasId, {
    type: "bar",
    data: { labels: entries.map((e) => e[0].replace(/_/g, " ")), datasets: [{ data: entries.map((e) => e[1]), backgroundColor: color, borderRadius: 5 }] },
    options: { indexAxis: "y", plugins: { legend: { display: false } } },
  });
}

async function loadQuality() {
  try {
    const d = await api("/data-quality/latest");
    const kp = $("dq-kpis"); clear(kp);
    kp.append(
      kpi("Rows read", fmtInt.format(d.rows_read), "raw file"),
      kpi("Rows loaded", fmtInt.format(d.rows_loaded), "clean, in PostgreSQL", "good"),
      kpi("Rows rejected", fmtInt.format(d.rows_rejected), "each with one recorded reason", d.rows_rejected ? "bad" : ""),
      kpi("Rejection rate", d.rejection_rate_pct + "%", "read = loaded + rejected"));
    barChart("chart-rejects", d.rejection_summary, "#ef4444");
    barChart("chart-repairs", d.repair_summary, "#0ea5a4");
    $("dq-source").textContent = `Run #${d.run_id} · ${d.source_file} · status: ${d.status}`;
  } catch (e) {
    $("dq-kpis").replaceChildren(h("div", { class: "notice warn" }, e.status === 404 ? "No ETL run recorded yet — run the pipeline first (see README)." : e.message));
  }
}

/* ------------------------------------------------------------------ boot */
loadHealth();
const start = location.hash.replace("#", "");
showTab(loaders[start] ? start : "overview");
