/* Quantara - dashboard client (no build step, no external libraries) */
const $ = (s, r = document) => r.querySelector(s);
const PAGE = document.body.dataset.page;
let STATE = {};
const FEED = [];

// ------------------------------------------------------------------ formatting
const n = (v, d = 2) => (v === null || v === undefined || Number.isNaN(+v)) ? "–" : (+v).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (v, d = 2) => (v === null || v === undefined) ? "–" : n(100 * v, d) + "%";
const signed = (v, d = 2) => (v === null || v === undefined) ? "–" : ((+v > 0 ? "+" : "") + n(v, d));
const cls = v => (v === null || v === undefined) ? "" : (+v > 0 ? "ok" : (+v < 0 ? "bad" : ""));
// Display time zone (config display.timezone, default Asia/Karachi). Internals and the database stay UTC.
let TZ = "Asia/Karachi";
const fmtParts = d => { const p = {}; new Intl.DateTimeFormat("en-GB", {timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit"}).formatToParts(d).forEach(x => p[x.type] = x.value); return p; };
const tzLabel = () => TZ === "Asia/Karachi" ? "PKT" : TZ === "UTC" ? "UTC" : TZ;
const tm = v => { if (!v) return "–"; const d = new Date(typeof v === "number" ? v * 1000 : v); if (isNaN(d)) return String(v);
  const p = fmtParts(d); return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`; };
const hhmmss = v => { const p = fmtParts(new Date(v * 1000)); return `${p.hour}:${p.minute}:${p.second}`; };
const duration = v => { let sec = Math.max(0, Math.floor(Number(v) || 0)); const h = Math.floor(sec / 3600); sec %= 3600;
  const m = Math.floor(sec / 60); sec %= 60; return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const STATUS_CLASS = s => (typeof term === "function" ? ({good: "good", bad: "bad", wait: "wait", info: "mut"}[term(s).tone] || "mut")
  : "mut");
// the raw code, shown small and greyed, so a technical reader can still see exactly what fired
const withCode = (text, code) => `${esc(text)} <span class="code" title="internal code">${esc(code)}</span>`;

function toast(msg, ms = 4000) {
  let t = $("#toast"); if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms);
}

// ------------------------------------------------------------------ header
function header() {
  const links = [["/", "Overview"], ["/realtime", "Live"], ["/logs", "History"], ["/analysis", "Analysis"], ["/config", "Settings"]];
  $("#hdr").innerHTML = `
  <div class="bar">
    <div class="brand"><img src="/static/quantara-logo.png" alt="" class="mark">QUANTARA<small>control center</small></div>
    <nav>${links.map(([h, l]) => `<a href="${h}" class="${location.pathname === h ? "on" : ""}">${l}</a>`).join("")}</nav>
    <span id="env" class="env">…</span><span id="sym" class="env"></span>
    <div class="spacer"></div>
    <button id="killbtn" class="kill" title="Stops new signals and new orders. Existing broker stops stay in place.">EMERGENCY · HALT NEW TRADES</button>
  </div>
  <div class="row2"><div class="pills" id="pills"></div>
    <div class="helpbtns">
      ${PAGE === "learn" ? "" : `<button id="learnbtn" class="hbtn primary" title="A five-minute animated walkthrough: what the bot does, how to read this screen, how to tell whether it works"><span class="ic" aria-hidden="true">&#9654;</span>Start here</button>
      <button id="tourbtn" class="hbtn" title="A spotlight walks you over each part of this page">Explain this page</button>`}
      <button id="modebtn" class="hbtn toggle" aria-pressed="true" title="Show or hide the explanatory line under each heading"><i class="knob" aria-hidden="true"></i>Explanations</button>
    </div></div>`;
  $("#killbtn").onclick = onKill;
  // the help controls only ever explain; none of them can change the bot
  if ($("#learnbtn")) $("#learnbtn").onclick = () => LEARN.open();     // picks up where it was left, if unfinished
  if ($("#tourbtn")) $("#tourbtn").onclick = () => HELP.tour();
  $("#modebtn").onclick = () => HELP.toggleMode();
  HELP.applyMode();
}

// Header pills, in plain words. The technical status still sits in the tooltip for anyone who wants it.
const PILL_LABEL = {MT5: "MetaTrader", DATA: "Prices", DATABASE: "Database", EXECUTION: "Order sending",
  RECONCILIATION: "Broker match", STRATEGY: "Rules", RISK: "Risk"};

function renderHeader(s) {
  const env = s.environment || "…";
  $("#env").textContent = env === "DEMO" ? "Practice account" : env === "REAL" ? "REAL MONEY" : env;
  $("#env").className = "env " + env;
  $("#env").title = `environment: ${env}`;
  $("#sym").textContent = s.symbol || "";
  const c = s.components || {};
  const acct = s.account || {};
  const pills = [["MT5", c.MT5], ["DATA", c.DATA], ["DATABASE", c.DATABASE], ["EXECUTION", c.EXECUTION],
    ["RECONCILIATION", c.RECONCILIATION], ["STRATEGY", strategySummary(s)], ["RISK", s.risk ? s.risk.state : null]];
  $("#pills").innerHTML = pills.map(([k, v]) => {
    const raw = String(v || "UNKNOWN"); const t = term(raw); const cl = STATUS_CLASS(raw);
    const live = cl === "good" && (k === "MT5" || k === "DATA") ? " live" : "";
    return `<div class="pill" title="${esc(k)}: ${esc(raw)}"><span class="dot ${cl}${live}"></span>${PILL_LABEL[k] || k}
      <b class="${cl}">${esc(t.say)}</b></div>`; }).join("");
  const k = s.kill_switch || {};
  const engaged = k.engaged || (k.volatile && k.volatile.length);
  const b = $("#killbtn"); b.classList.toggle("engaged", !!engaged);
  b.textContent = engaged ? "Resume trading" : "Stop all new trades";
  const ban = $("#banner");
  if (ban) {
    if (engaged) {
      ban.className = "banner halt show";
      ban.textContent = `New trades are stopped: ${explain(k.reason || (k.volatile || []).join(", "))}`
        + ` (stopped by ${k.by || "the system"}). Positions already open keep their stop and target at the broker.`;
    } else ban.className = "banner";
  }
}

// ------------------------------------------------------------------ the hero sentence
// The dashboard's one job on opening: say whether it is working, in a sentence, before any number.
function renderHero(s) {
  const el = $("#hero"); if (!el) return;
  const a = s.account || {}, r = s.risk || {}, k = s.kill_switch || {};
  const monitor = s.trading_status || {};
  const reasons = s.status_reasons || [];
  const positions = (s.positions || []).length;
  const symbols = Object.values(s.symbol_trading || {}).filter(x => x.enabled).length;
  const total = Object.keys(s.symbol_trading || {}).length;

  let headline, sub, tone = "good";
  const blocking = reasons.find(x => x.severity === "CRITICAL") || reasons.find(x => x.severity === "WARNING");
  if (k.engaged || (k.volatile || []).length) {
    tone = "bad"; headline = "Stopped — you pressed the stop button";
    sub = "No new trades will open. Anything already open still has its stop and target sitting at the broker. "
        + "Press “Resume trading” above when you want it going again.";
  } else if (!monitor.is_trading_allowed && blocking) {
    const t = term(blocking.code);
    tone = blocking.severity === "CRITICAL" ? "bad" : "wait";
    headline = "Not trading right now";
    sub = t.mean ? `${t.say}. ${t.mean}` : `${t.say}. ${blocking.message || ""}`;
  } else if (!symbols && total) {
    tone = "wait"; headline = "Every symbol is stopped";
    sub = "Nothing can open until you start at least one symbol below.";
  } else {
    headline = positions ? `Trading — ${positions} position${positions === 1 ? "" : "s"} open`
                         : "Watching for a setup";
    sub = positions
      ? `Across ${symbols} of ${total} symbols. Each open trade already has a stop and a target at the broker.`
      : `Watching ${symbols} of ${total} symbols. No setup matches the rules at the moment, which is the normal state most of the day.`;
  }
  if (r.state && r.state !== "NORMAL") {
    const rt = term(r.state);
    sub += ` ${rt.say}${rt.mean ? " — " + rt.mean.toLowerCase() : ""}`;
    if (tone === "good") tone = "wait";
  }

  // The words are rebuilt only when they change, so a hovered explanation is not wiped every 1.5 s; the
  // numbers are updated in place and roll to their new value.
  const day = todaySplit(a);
  const key = [tone, headline, sub, day.traded == null, day.other != null].join("|");
  el.className = "hero" + (tone === "good" ? "" : tone === "wait" ? " is-wait" : " is-bad");
  if (el.dataset.key !== key) {
    el.dataset.key = key;
    el.innerHTML = `<div class="hero-l"><span class="orb ${tone}" aria-hidden="true"></span>
        <div><h1>${esc(headline)}</h1><div class="sub">${esc(sub)}</div></div></div>
      ${day.traded == null ? "" : `<div class="money">
        <div class="big" id="hBig"></div>
        <div class="cap">from trading today · banked <span id="hBank"></span> · ${defn("FLOATING", "open")} <span id="hOpen"></span></div>
        ${day.other != null ? `<div class="cap wait" id="hOtherRow"><span id="hOther"></span> not from trading — a ${defn("DEPOSIT", "deposit or top-up")}, not profit</div>` : ""}
        <div class="cap">${defn("BALANCE", "balance")} <span id="hBal"></span> · ${defn("EQUITY", "worth")} <span id="hEq"></span> with open trades</div>
      </div>`}`;
  }
  if (day.traded == null) return;
  const big = $("#hBig");
  big.className = "big " + cls(day.traded);
  VZ.countUp(big, day.traded, v => signed(v));
  $("#hBank").textContent = signed(a.today_realized);
  $("#hOpen").textContent = signed(a.floating_pnl);
  if ($("#hOther")) $("#hOther").textContent = signed(day.other);
  $("#hBal").textContent = n(a.balance);
  $("#hEq").textContent = n(a.equity);
}

// What trading did today, kept apart from money that arrived any other way. The engine's today_pnl is "worth now
// minus worth at the start of the day", so a deposit or a demo top-up during the day reads as profit there - on
// 20 Oct 2026 the $500 -> $5,000 top-up showed as "+4,899 today". Banked results plus the open trades are what
// trading did; whatever is left over is shown separately, with the same tolerance as tools/daily_review.py's
// unexplained_balance (the larger of 1 and 0.2% of the balance), so swap rounding never looks like a deposit.
function todaySplit(a) {
  if (a.today_realized == null && a.floating_pnl == null) return {traded: a.today_pnl ?? null, other: null};
  const traded = (+a.today_realized || 0) + (+a.floating_pnl || 0);
  const gap = a.today_pnl == null ? 0 : a.today_pnl - traded;
  return {traded, other: Math.abs(gap) >= Math.max(1, 0.002 * Math.abs(+a.balance || 0)) ? gap : null};
}

function strategySummary(s) {
  const st = s.strategies || {}; const vals = Object.values(st).filter(x => x.enabled);
  if (!vals.length) return "…";
  const order = ["DISABLED", "RESTRICTED", "DEGRADED", "WATCH", "HEALTHY"];
  for (const o of order) if (vals.some(v => v.health === o)) return o;
  return "HEALTHY";
}

// ------------------------------------------------------------------ token + controls
async function token() {
  let t = localStorage.getItem("ata_token");
  if (!t) { t = prompt("Dashboard token (DASHBOARD_TOKEN from your .env file):"); if (!t) return null; }
  const r = await fetch("/api/verify-token", {method: "POST", headers: {"X-Dashboard-Token": t}});
  const j = await r.json();
  if (!j.ok) { localStorage.removeItem("ata_token"); toast("Wrong token"); return null; }
  localStorage.setItem("ata_token", t); return t;
}

async function control(action, body = {}) {
  const t = await token(); if (!t) return null;
  const r = await fetch("/api/control/" + action, {method: "POST", headers: {"Content-Type": "application/json", "X-Dashboard-Token": t}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({ok: false, error: "bad response"}));
  if (r.status === 403) localStorage.removeItem("ata_token");
  const blockers = (j.blockers || []).map(b => `${b.code}: ${b.message}`).join("; ");
  toast(j.ok ? `${action}: done` : `${action} failed: ${j.error || JSON.stringify(j)}${blockers ? " · " + blockers : ""}`, 9000);
  refresh(); return j;
}

async function onKill() {
  const k = STATE.kill_switch || {};
  if (k.engaged || (k.volatile && k.volatile.length)) {
    const reason = prompt("Resume trading - reason (required):"); if (!reason) return;
    await control("resume", {reason});
  } else {
    const reason = prompt("HALT NEW TRADES - reason (optional):", "manual halt"); if (reason === null) return;
    await control("halt", {reason});
  }
}

// ------------------------------------------------------------------ tables
function table(el, rows, cols, empty = "Nothing yet") {
  if (!el) return;
  if (!rows || !rows.length) { el.innerHTML = `<div class="empty">${empty}</div>`; return; }
  el.innerHTML = `<table><thead><tr>${cols.map(c => `<th>${c[1]}</th>`).join("")}</tr></thead><tbody>${
    rows.map(r => `<tr>${cols.map(([k, , f, cf]) => { const v = r[k]; const txt = f ? f(v, r) : (v === null || v === undefined ? "–" : (typeof v === "object" ? JSON.stringify(v) : v));
      return `<td class="${cf ? cf(v, r) : ""}" title="${esc(typeof v === "object" ? JSON.stringify(v) : v)}">${esc(txt)}</td>`; }).join("")}</tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ chart
// Timestamps arrive as ISO strings with microseconds; trim to milliseconds so every browser parses them.
const epoch = v => typeof v === "number" ? v : Date.parse(String(v || "").replace(/(\.\d{3})\d+/, "$1")) / 1000;
const isClosed = t => t && (t.state === "CLOSED" || t.state === "BROKER_CLOSED") && t.closed_at;

// The account-value chart: the line, every finished trade as a dot you can click for its story, and a flag at
// every balance change no finished trade explains - a deposit, a demo top-up or an account switch - so money that
// was added never reads as money that was made.
let EQ = null, EQ_SIG = "";
async function loadEquity() {
  const canvas = $("#equity"); if (!canvas) return;
  const hours = $("#eqh") ? $("#eqh").value : 168, key = $("#eqkey") ? $("#eqkey").value : "equity";
  let pts = [], trades = [];
  try { pts = await (await fetch("/api/equity?hours=" + hours)).json(); } catch (_) { return; }
  try { trades = await (await fetch("/api/logs/trades?limit=500")).json(); } catch (_) {}
  if (!Array.isArray(pts)) pts = [];
  const since = pts.length ? pts[0].t : Infinity;
  const closed = (Array.isArray(trades) ? trades : []).filter(isClosed)
    .map(t => ({...t, ct: epoch(t.closed_at)})).filter(t => t.ct >= since - 300);
  const markers = closed.map(t => ({t: t.ct, tone: +t.net_pnl > 0 ? "good" : +t.net_pnl < 0 ? "bad" : "mut", data: t,
    label: `<b class="${cls(t.net_pnl)}">${+t.net_pnl >= 0 ? "Won" : "Lost"} ${n(Math.abs(+t.net_pnl || 0))}</b> on ${esc(t.symbol)}`
      + `<br><span class="mut">${esc(t.strategy_id || "")}${t.r_multiple == null ? "" : " · " + signed(t.r_multiple) + "R"} · ${esc(tm(t.ct).slice(0, 16))}</span>`
      + `<br><span class="mut">click for the story of this trade</span>`}));
  const flags = [];
  for (let i = 1; i < pts.length; i++) {
    const d = pts[i].balance - pts[i - 1].balance;
    if (!d) continue;
    // trades closing a little either side of the snapshot pair still explain the step; snapshots lag the broker
    const explained = closed.filter(t => t.ct > pts[i - 1].t - 180 && t.ct <= pts[i].t + 180).reduce((s, t) => s + (+t.net_pnl || 0), 0);
    const gap = d - explained;
    if (Math.abs(gap) >= Math.max(1, 0.002 * Math.abs(pts[i].balance))) flags.push({t: pts[i].t, amount: gap, short: `${signed(gap, 0)} not from trading`,
      label: `<b class="wait">${signed(gap)}</b> balance change that no finished trade explains<br><span class="mut">a deposit, a demo top-up or an account switch — not profit</span>`});
  }
  if (!EQ) EQ = VZ.lineChart(canvas, {formatX: v => tm(v).slice(5, 16), formatY: v => n(v), onMarker: t => VZ.tradeStory(t),
    empty: "The line appears after a few minutes of running"});
  const sig = hours + "|" + key;
  if (sig !== EQ_SIG) { EQ_SIG = sig; EQ.set({pts, key, markers, flags}); } else EQ.update({pts, markers, flags});
}

// Rows of a table that are finished trades open their story when clicked (or Enter on the keyboard).
function storyRows(el, rows, ok = isClosed) {
  if (!el || !rows) return;
  el.querySelectorAll("tbody tr").forEach((tr, i) => {
    if (rows[i] && ok(rows[i])) { tr.classList.add("click"); tr.tabIndex = 0; }
  });
  const pick = tr => tr && tr.classList.contains("click") && VZ.tradeStory(rows[[...tr.parentNode.children].indexOf(tr)]);
  el.onclick = e => pick(e.target.closest("tbody tr"));
  el.onkeydown = e => { if (e.key === "Enter") pick(e.target.closest("tbody tr")); };
}

// ------------------------------------------------------------------ live feed
// The live feed reads like a running commentary, not a log dump. Each line answers "what just happened"
// first; the technical codes stay available in the row's tooltip.
const FEED_LABEL = {
  execution_event: e => ["Order", [say(e.event_type), e.retcode_text && e.retcode_text !== "DONE" ? say(e.retcode_text) : "",
      e.filled_volume ? `${n(e.filled_volume, 2)} lots` : "", e.price ? `at ${n(e.price)}` : "", e.error || ""]
      .filter(Boolean).join(" · ")],
  trade_update: e => [`${e.symbol || "Trade"}`, [say(e.state), String(e.direction || "").toLowerCase(),
      e.net_pnl != null ? `${signed(e.net_pnl)}` : "", e.r != null ? `${signed(e.r)}R` : "", e.event ? say(e.event) : ""]
      .filter(Boolean).join(" · ")],
  signal_event: e => ["Signal", [e.strategy || "", String(e.direction || "").toLowerCase(),
      e.entry ? `in ${n(e.entry)}, stop ${n(e.sl)}, target ${n(e.tp)}` : "", e.reason ? say(e.reason) : ""]
      .filter(Boolean).join(" · ")],
  rejection_event: e => e.counterfactual_resolved
      ? ["Skipped trade settled", `${say(e.gate)} · on paper it would have been ${signed(e.hypothetical_r)}R`]
      : ["Did not trade", `${e.strategy || ""} ${String(e.direction || "").toLowerCase()} · ${say(e.reason || e.gate)}`],
  risk_state: e => ["Risk", [say(e.state), e.reason ? say(e.reason) : ""].filter(Boolean).join(" · ")],
  health_event: e => ["System", e.message || say(e.startup_state || "")],
  straddle_event: e => ["News orders", [e.symbol || "", say(e.status || ""), e.event || "",
      e.leg ? String(e.leg).toLowerCase() : "", e.reason || e.outcome ? say(e.reason || e.outcome) : ""]
      .filter(Boolean).join(" · ")],
  strategy_state: e => ["Market shape", e.regime ? `${say(e.regime.label)} · ${say(e.regime.vol)}` : ""]};

// The colour of a feed line follows the same rule as everywhere else: it describes the situation. A refused
// signal is the bot working, so it reads calm; money lost on a closed trade reads coral.
const FEED_TONE = {
  execution_event: e => e.error ? "bad" : /FILL/.test(String(e.event_type || "")) ? "good" : "info",
  trade_update: e => +e.net_pnl > 0 ? "good" : +e.net_pnl < 0 ? "bad" : "info",
  rejection_event: e => e.counterfactual_resolved ? (+e.hypothetical_r > 0 ? "wait" : "good") : term(e.reason || e.gate).tone,
  risk_state: e => term(e.state).tone, signal_event: () => "info", health_event: () => "info",
  straddle_event: () => "info", strategy_state: () => "mut"};

function pushFeed(e, fresh = false) {
  const f = FEED_LABEL[e.event]; if (!f) return;
  const [a, b] = f(e);
  FEED.unshift({t: e.ts, a, b, raw: e.event, tone: (FEED_TONE[e.event] || (() => "info"))(e)}); if (FEED.length > 300) FEED.pop();
  const el = $("#feed");
  // only the line that just arrived slides in; the rest stay still
  if (el) el.innerHTML = FEED.map((x, i) => `<div class="fi ${x.tone}${fresh && i === 0 ? " new" : ""}" title="${esc(x.raw)}"><span class="t">${hhmmss(x.t)}</span>
    <span class="a">${esc(x.a)}</span><span>${esc(x.b)}</span></div>`).join("");
}

// One ping timer per connection, cleared when it closes: every reconnect used to add a timer that never stopped.
// The server closes a connection that stays silent (api.WS_IDLE_SEC), so the ping is what keeps a live page fed.
function connectWS() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  const ping = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 20000);
  ws.onmessage = m => { try { JSON.parse(m.data).forEach(e => { if (e.event === "state") { STATE = e; } else pushFeed(e, true); }); } catch (_) {} };
  ws.onclose = () => { clearInterval(ping); setTimeout(connectWS, 2000); };
}

// ------------------------------------------------------------------ pages
async function refresh() {
  try { STATE = await (await fetch("/api/state")).json(); } catch (_) { return; }
  if (STATE.display_timezone) TZ = STATE.display_timezone;
  renderHeader(STATE);
  if (PAGE === "overview") renderOverview(STATE);
  if (PAGE === "realtime") renderRealtime(STATE);
  if (PAGE === "config") renderConfigState(STATE);
}

function kpis(el, items) {
  el.innerHTML = items.map(([l, v, c]) => `<div class="card kpi"><div class="l">${l}</div><div class="v ${c || ""}">${v}</div></div>`).join("");
}

// Enough decimals for the instrument: FX needs 5, indices 3, metals 2. Without this a 5-digit FX quote
// renders entry, stop and target as the same "1.09" and a trade looks like it is sitting on its target.
const autoPx = v => { const a = Math.abs(+v); return !isFinite(a) ? n(v) : n(v, a < 20 ? 5 : a < 500 ? 3 : 2); };

// Prices use the SYMBOL's digits, not a global 2. At 2 dp a 5-digit FX quote renders 1.11234 and 1.12480 as
// "1.11" and "1.12", so entry, current, stop and target collapse together and a position looks like it is
// sitting on its target when it is hundreds of pips away.
const px = (v, row) => n(v, row && row.digits != null ? row.digits : 2);
const POS_COLS = [["ticket", "Ticket"], ["symbol", "Symbol"], ["side", "Side"], ["volume", "Volume"], ["entry", "Entry", px],
  ["current", "Current", px], ["floating_pnl", "Floating P/L", v => signed(v), cls], ["sl", "SL", px], ["tp", "TP", px],
  ["strategy", "Strategy"], ["state", "State"], ["managed", "Managed", v => v ? "yes" : "NO (manual)"]];

function renderOverview(s) {
  const a = s.account || {};
  renderHero(s);
  renderOpenPositions(s);
  const day = todaySplit(a);
  kpis($("#acct"), [[defn("BALANCE", "Balance"), n(a.balance)], [defn("EQUITY", "Worth now"), n(a.equity)], ["Free margin", n(a.free_margin)],
    [defn("MARGIN", "Margin in use"), n(a.margin)], ["Margin level", a.margin_level ? n(a.margin_level, 0) + "%" : "–"],
    ["Today from trading", signed(day.traded), cls(day.traded)],
    day.other != null ? [defn("DEPOSIT", "Today, not from trading"), signed(day.other), "wait"]
                      : ["Today %", pct(day.traded != null && a.balance ? day.traded / a.balance : null), cls(day.traded)]]);
  const st = s.strategies || {};
  table($("#strats"), Object.entries(st).map(([id, v]) => ({id, ...v})),
    [["id", "Rule"], ["status", "Cleared for", v => v === "DEMO" ? "practice account" : String(v || "").toLowerCase()],
     ["enabled", "Switched on", v => v ? "yes" : "no", v => v ? "good" : "mut"],
     ["health", "How it is doing", v => say(v), v => STATUS_CLASS(v)],
     ["trades", "Trades taken"], ["expectancy_r", "Average result", v => v == null ? "–" : signed(v, 3) + "R", cls],
     ["diagnostics", "Last look at the chart", v => (v || []).map(say).join("; ") || "–"]],
    "The rules appear once the bot has started");
  renderSymbolSwitch(s);
  table($("#micro"), Object.entries(s.microstructure || {}).map(([sym, m]) => ({symbol: sym, cvd: m.cvd, delta: m.delta_window, agg: m.aggression_buy_pct, burst: m.burst,
    vw: m.vwap_dev_sigma, poc: m.poc, va: m.val != null ? `${n(m.val, 3)} – ${n(m.vah, 3)}` : "–", shift: m.poc_shift_points, vpin: m.vpin != null ? `${m.vpin} ${m.vpin_label}` : (m.vpin_label || m.status),
    book: m.book ? (m.book.status === "OK" ? `imb ${m.book.imbalance}` : "no depth") : "–"})),
    [["symbol", "Symbol"], ["cvd", "CVD"], ["delta", "Δ window"], ["agg", "Buy %"], ["burst", "Burst"], ["vw", "VWAP dev σ"], ["poc", "POC", v => n(v, 3)],
     ["va", "Value area"], ["shift", "POC shift (pts)"], ["vpin", "VPIN"], ["book", "Book"]], "Waiting for ticks")
  table($("#adaptive"), Object.entries(s.adaptive || {}).map(([sym, v]) => ({symbol: sym, on: v.enabled ? "on" : "off", scale: v.vol_scale, spread: v.max_spread_points, ratio: (v.metrics || {}).atr_ratio, at: v.updated_at, why: v.last_reason})),
    [["symbol", "Symbol"], ["on", "Adaptive"], ["scale", "Vol scale", v => n(v, 3)], ["ratio", "ATR short/long", v => n(v, 2)], ["spread", "Spread cap (pts)", v => v == null ? "global" : n(v, 0)], ["at", "Updated", v => v ? tm(v) : "–"], ["why", "Last result"]],
    "No adaptive data yet")
  const marketStatuses = s.symbol_market_status || {};
  table($("#marketHours"), Object.entries(marketStatuses).map(([symbol, market]) => ({symbol, ...market})),
    [["symbol", "Symbol"], ["status", "Market"], ["opens_at_utc", `Estimated reopen (${tzLabel()})`, v => v ? tm(v) : "–"],
      ["opens_at_utc", "Time to open", (v, row) => v ? duration((Date.parse(v) - Date.now()) / 1000) : "–"]],
    "Market hours will appear when symbol scanning starts");
  table($("#exposure"), s.positions || [], POS_COLS, "No open positions");
  const r = s.risk || {};
  renderStatusReasons(s, $("#statusReasons"));
  const rt = term(r.state || "UNKNOWN");
  const mult = r.multiplier == null ? null : Math.round(r.multiplier * 100);
  renderRiskViz(r);
  $("#risk").innerHTML = `<div class="kv">
    <span>Right now</span><span class="${STATUS_CLASS(r.state)}">${esc(rt.say)}</span>
    <span>Size per trade</span><span>${mult == null ? "–" : mult + "% of normal"}</span>
    <span>${defn("DRAWDOWN", "Down from its best")}</span><span class="${r.drawdown > 0.05 ? "wait" : ""}">${pct(r.drawdown)}</span>
    <span>Best the account has been</span><span>${n(r.peak_equity)}</span>
    <span>Today from trading</span><span class="${cls(day.traded)}">${signed(day.traded)}</span>
    <span>Can still lose today</span><span>${n(r.daily_loss_remaining)}</span></div>
    ${rt.mean ? `<div class="note">${esc(rt.mean)}</div>` : ""}
    ${r.reason && r.reason !== "ok" ? `<div class="note">${esc(explain(r.reason))}</div>` : ""}`;

  const c = s.components || {};
  const val = {HEALTHY: 1, MARKET_CLOSED: .7, WATCH: .7, DEGRADED: .4, DOWN: .05, UNKNOWN: .2};
  const COL = {good: "var(--seafoam)", wait: "var(--sand)", bad: "var(--coral)", mut: "var(--faint)"};
  const bar = (label, raw, pctWidth) => {
    const t = term(raw), cl = STATUS_CLASS(raw);
    return `<div class="hb" title="${esc(label)}: ${esc(raw)}"><span>${esc(label)}</span>
      <div class="track"><div class="fill" style="width:${pctWidth}%;background:${COL[cl] || COL.mut}"></div></div>
      <b class="${cl}">${esc(t.say)}</b></div>`;
  };
  const summary = strategySummary(s);
  $("#health").innerHTML =
    [["MetaTrader link", c.MT5], ["Database", c.DATABASE], ["Price feed", c.DATA],
     ["Order sending", c.EXECUTION], ["Matches the broker", c.RECONCILIATION]]
      .map(([label, v]) => bar(label, v || "UNKNOWN", 100 * (val[v] ?? .2))).join("")
    + bar("Trading rules", summary, summary === "HEALTHY" ? 100 : 60)
    + bar("Risk", r.state || "UNKNOWN", r.state === "NORMAL" ? 100 : 50);
}

// Each open trade as a card: the money first, then where price sits between the stop and the target.
function renderOpenPositions(s) {
  const el = $("#openPositions") || $("#positions"); if (!el) return;
  const rows = s.positions || [];
  if (!rows.length) {
    el.innerHTML = `<div class="empty"><b>Nothing open</b>When the bot takes a trade it will appear here with
      its live profit or loss, and how close it is to its stop and target.</div>`;
    return;
  }
  const empty = el.querySelector(".empty"); if (empty) empty.remove();
  // Cards are kept and updated in place, keyed by ticket, so the "now" marker glides to each new price
  // instead of the whole card being redrawn every 1.5 seconds.
  const seen = new Set();
  rows.forEach(p => {
    const id = String(p.ticket); seen.add(id);
    let card = [...el.children].find(c => c.dataset.t === id);
    if (!card) {
      card = document.createElement("div"); card.className = "pos"; card.dataset.t = id;
      card.innerHTML = `<div class="r1"><span class="who"></span><span class="amt"></span></div><div class="r2"></div>
        <div class="ptrack" tabindex="0" aria-label="Where the price sits between the stop and the target">
          <i class="pz bad"></i><i class="pz good"></i><i class="pmove"></i><i class="pentry"></i><i class="pnow"></i></div>
        <div class="legend"><span class="l-stop"></span><span class="l-now"></span><span class="l-tgt"></span></div>`;
      const track = card.querySelector(".ptrack");
      track.addEventListener("pointermove", e => { const r = track.getBoundingClientRect(); priceTip(card._g, (e.clientX - r.left) / r.width, e.clientX, e.clientY); });
      track.addEventListener("pointerleave", () => VZ.untip());
      track.addEventListener("focus", () => { const r = track.getBoundingClientRect(); priceTip(card._g, card._g.fNow, r.left + r.width * card._g.fNow, r.bottom); });
      track.addEventListener("blur", () => VZ.untip());
      el.appendChild(card);
    }
    fillPosition(card, p);
  });
  [...el.children].forEach(c => { if (c.dataset.t && !seen.has(c.dataset.t)) c.remove(); });
}

// The track runs stop -> target, so the left edge is always "the losing end" whichever way the trade faces.
// (v - stop) / (target - stop) maps a BUY and a SELL onto the same left-is-bad axis without flipping anything.
function fillPosition(card, p) {
  const d = p.digits != null ? p.digits : 2;
  const buy = String(p.side).toUpperCase() === "BUY";
  const entry = +p.entry, now = +(p.current ?? entry);
  const away = Math.abs(now - entry) || Math.abs(entry) * 0.001 || 1;
  // MT5 reports "no stop" / "no target" as 0.0, not null: read as a price it printed "stop 0.00" and told a SELL
  // with no stop at all that it "can no longer lose" (0 is below any entry)
  const sl = p.sl != null && +p.sl !== 0 ? +p.sl : null, tp = p.tp != null && +p.tp !== 0 ? +p.tp : null;
  let stop = sl, target = tp;
  if (stop == null && target == null) { stop = entry - (buy ? 2 : -2) * away; target = entry + (buy ? 2 : -2) * away; }
  else if (stop == null) stop = entry - (target - entry);
  else if (target == null) target = entry + (entry - stop);
  const F = v => VZ.clamp((v - stop) / ((target - stop) || 1), 0, 1);
  const fE = F(entry), fN = F(now);
  const protectedNow = sl != null && (buy ? sl >= entry : sl <= entry);
  const q = s => card.querySelector(s);
  q(".who").textContent = `${p.symbol} ${buy ? "buy" : "sell"} ${n(p.volume, 2)}`;
  const amt = q(".amt"); amt.className = "amt " + cls(p.floating_pnl); VZ.countUp(amt, p.floating_pnl, v => signed(v), 500);
  const r2 = `${esc(p.strategy || "opened by hand")} · ${protectedNow
    ? '<span class="good">can no longer lose &mdash; the stop has been moved past the entry price</span>'
    : `the ${defn("STOP", "stop")} is still on the losing side of the entry price`}${p.managed ? "" : ' · <span class="wait">opened outside the bot</span>'}`;
  if (card._r2 !== r2) { card._r2 = r2; q(".r2").innerHTML = r2; }
  const pz = card.querySelectorAll(".pz");
  pz[0].style.width = fE * 100 + "%";
  pz[1].style.left = fE * 100 + "%"; pz[1].style.width = (1 - fE) * 100 + "%";
  const mv = q(".pmove");
  mv.style.left = Math.min(fE, fN) * 100 + "%"; mv.style.width = Math.max(0.6, Math.abs(fN - fE) * 100) + "%";
  mv.className = "pmove " + ((p.floating_pnl ?? 0) >= 0 ? "good" : "bad");
  q(".pentry").style.left = fE * 100 + "%";
  q(".pnow").style.left = fN * 100 + "%";
  q(".l-stop").textContent = sl == null ? "no stop" : `stop ${n(sl, d)}`; q(".l-now").textContent = `now ${n(now, d)}`;
  q(".l-tgt").textContent = tp == null ? "no target" : `target ${n(tp, d)}`;
  // what any price on the track would mean in money: a straight line through (entry, 0) and (now, open P/L)
  const perPrice = Math.abs(now - entry) > 0 && p.floating_pnl != null ? +p.floating_pnl / (now - entry) : null;
  const risk = sl != null && !protectedNow ? Math.abs(entry - sl) : null;
  card._g = {stop, target, entry, d, perPrice, risk, dir: buy ? 1 : -1, fNow: fN};
}

function priceTip(g, frac, x, y) {
  if (!g) return;
  const price = g.stop + VZ.clamp(frac, 0, 1) * (g.target - g.stop);
  const money = g.perPrice == null ? null : g.perPrice * (price - g.entry);
  const r = g.risk ? (price - g.entry) * g.dir / g.risk : null;
  VZ.tip(`If price reaches <b>${n(price, g.d)}</b>:<br>`
    + (money == null ? `<span class="mut">about ${r == null ? "–" : signed(r) + "R"} (money appears once price has moved)</span>`
      : `<b class="${cls(money)}">${signed(money)}</b>${r == null ? "" : ` <span class="mut">· ${signed(r)}R</span>`}`)
    + `<br><span class="mut">left end = stop, right end = target</span>`, x, y);
}

const TILE_LINE = {};
function renderSymbolSwitch(s) {
  const sw = s.symbol_trading || {}, mk = s.symbol_market_status || {}, pos = s.positions || [], st = s.news_straddle || {};
  const el = $("#symbolTiles");
  if (el) {
    const syms = Object.keys(sw);
    if (!syms.length) el.innerHTML = `<div class="empty"><b>Starting up</b>Symbols appear once the bot has
      connected to MetaTrader and loaded their price history.</div>`;
    else el.innerHTML = syms.map(sym => {
      const on = sw[sym].enabled;
      const market = (mk[sym] || {}).status;
      const mine = pos.filter(p => p.symbol === sym);
      const pnl = mine.reduce((t, p) => t + (p.floating_pnl || 0), 0);
      const straddle = st[sym] || {};
      let line;
      if (!on) line = "You stopped this one";
      else if (market && market !== "OPEN") line = say(market);
      else if (mine.length) line = `${mine.length} trade${mine.length === 1 ? "" : "s"} open`;
      else if (straddle.state && straddle.state !== "IDLE") line = say(straddle.state);
      else line = "Watching";
      // a tile whose sentence changed since the last refresh flashes once, so a change catches the eye
      const changed = TILE_LINE[sym] !== undefined && TILE_LINE[sym] !== line;
      TILE_LINE[sym] = line;
      const tipText = `${sym}\n${line}${market ? `\nmarket: ${String(market).toLowerCase().replace(/_/g, " ")}` : ""}`
        + `${mine.length ? `\nopen profit or loss: ${signed(pnl)}` : ""}\n\nClick to ${on ? "STOP" : "START"} new trades here (it asks first).`
        + `${on ? "\nOpen trades are never touched." : ""}`;
      return `<button class="tile ${on ? "" : "off"}${changed ? " flash" : ""}" data-sym="${esc(sym)}" data-on="${on ? 1 : 0}"
          data-tip="${esc(tipText)}" aria-label="${esc(sym)} — ${esc(line)}. Click to ${on ? "stop" : "start"} new trades">
        <div class="top"><span class="sym">${esc(sym)}</span>
          <span class="dot ${!on ? "" : mine.length ? "good live" : "good"}"></span></div>
        <div class="st">${esc(line)}</div>
        ${mine.length ? `<div class="pl ${cls(pnl)}">${signed(pnl)}</div>` : '<div class="pl mut">–</div>'}
      </button>`;
    }).join("");
    el.onclick = async e => {
      const b = e.target.closest(".tile"); if (!b) return;
      const sym = b.dataset.sym, start = b.dataset.on !== "1";
      if (start && !confirm(`Start trading ${sym}?\n\nThe bot may open new positions on it.`)) return;
      if (!start && !confirm(`Stop new trades on ${sym}?\n\nAnything already open stays open and keeps its stop and target.`)) return;
      await control("set_symbol_trading", {symbol: sym, enabled: start,
        reason: start ? "started from dashboard" : "stopped from dashboard"});
    };
  }
  const srows = Object.entries(st).map(([sym, v]) => ({
    symbol: sym,
    state: v.disabled_reason ? say(v.disabled_reason) : say(v.state),
    event: v.event || (v.next_event ? v.next_event.title : null),
    when: v.event_time || (v.next_event ? v.next_event.time : null),
    legs: v.legs ? Object.entries(v.legs).map(([k, l]) => `${k.toLowerCase()} ${say(l.state).toLowerCase()} at ${n(l.price)}`).join(", ") : null,
    last: v.last_outcome ? say(v.last_outcome.outcome) : (v.last_skip ? say(v.last_skip.reason) : null)}));
  table($("#straddle"), srows,
    [["symbol", "Symbol"], ["state", "Right now", null, v => STATUS_CLASS(v)],
     ["event", "Which release", v => v || "nothing scheduled"],
     ["when", `Due (${tzLabel()})`, v => v ? tm(v).slice(0, 16) : "–"],
     ["legs", "Orders placed", v => v || "none"], ["last", "Last time", v => v || "–"]],
    "No symbol is set up for news trading yet");
}

function renderStatusReasons(s, element) {
  if (!element) return;
  const monitor = s.trading_status || {};
  const reasons = s.status_reasons || [];
  const paused = !monitor.is_trading_allowed;
  const timer = paused ? duration(monitor.downtime_seconds) : "00:00:00";
  const last = monitor.last_downtime_seconds == null ? "–" : duration(monitor.last_downtime_seconds);
  const total = duration(monitor.total_downtime_seconds);
  const row = (code, fallback, severity) => {
    const t = term(code);
    const color = severity === "CRITICAL" ? "bad" : severity === "WARNING" ? "wait" : STATUS_CLASS(code);
    return `<div class="statusrow ${color}" title="${esc(code)}">
      <span class="msg">${esc(t.say)}${t.mean || fallback ? `<small>${esc(t.mean || fallback)}</small>` : ""}</span>
      <span class="code">${esc(code)}</span></div>`;
  };
  const rows = reasons.length
    ? reasons.map(r => row(r.code, r.message, r.severity)).join("")
    : row("SYSTEM_READY", "", "INFO");
  // The monitor only counts DOWNtime, so there is no uptime figure to show - saying "running for 00:00:00"
  // would be worse than saying nothing.
  element.innerHTML = `<div class="summary-line">
    <span class="${paused ? "wait" : "good"}">${paused ? `Paused for <b>${timer}</b>` : "Running normally"}</span>
    <span>Last pause lasted <b>${last}</b></span><span>Paused <b>${total}</b> in total</span>
    ${monitor.last_resumed_at ? `<span>Last resumed <b>${esc(tm(monitor.last_resumed_at))}</b> ${tzLabel()}</span>` : ""}
  </div><div class="statuslist">${rows}</div>`;
}

// The funnel: how many chances the bot looked at, and where they fell out. This is the single most useful
// thing on the page when the answer to "why so few trades" is wanted, so it is built from the real
// candidate-evaluation records rather than a guess.
// The counts come from funnelModel() in glossary.js, which the walkthrough uses too, so the two can never
// disagree. Each step is a button: it lists why chances fell out just before it.
let FUNNEL = null, FUNNEL_FLOW = null, FUNNEL_GAP = null;
async function loadFunnel() {
  const box = $("#funnel"), list = $("#funnelReasons");
  if (!box) return;
  let d;
  try { d = await (await fetch("/api/candidates/summary?days=1")).json(); } catch (_) { return; }
  if (d.error || !d.groups) {
    box.innerHTML = `<div class="empty"><b>Nothing recorded yet</b>${esc(d.error || "This fills in once the bot has looked at a few candles.")}</div>`;
    list.innerHTML = ""; return;
  }
  if (typeof funnelModel !== "function") {        // an old saved glossary.js - see start() at the bottom
    box.innerHTML = `<div class="empty"><b>Reload the page</b>This panel needs the current version of the page. Press Ctrl+F5.</div>`;
    list.innerHTML = ""; return;
  }
  FUNNEL = funnelModel(d);
  if (FUNNEL_GAP == null) FUNNEL_GAP = FUNNEL.reasons[3].length ? 3 : 2;
  if (!FUNNEL_FLOW && $("#funnelFlow")) FUNNEL_FLOW = VZ.flow($("#funnelFlow"), {onPick: i => { FUNNEL_GAP = i; renderFunnel(); }});
  if (FUNNEL_FLOW) FUNNEL_FLOW.set(FUNNEL.steps);
  renderFunnel();
}

function renderFunnel() {
  const box = $("#funnel"), list = $("#funnelReasons"), F = FUNNEL;
  if (!F) return;
  const names = ["Chances looked at", "Its rule was switched on", "Matched a rule's setup", "Became a trade"];
  const max = Math.max(1, F.looked);
  box.innerHTML = F.steps.map((s, i) => `
    <button class="fstep${i && i === FUNNEL_GAP ? " on" : ""}" ${i ? `data-gap="${i}"` : "disabled"}
        data-tip="${esc(i ? `${n(F.steps[i - 1].value - s.value, 0)} ${s.drop.label} before this step.\n${s.drop.why}\nClick to see the reasons.` : "Every time a rule checked a finished candle today.")}">
      <span>${names[i]}</span>
      <div class="track"><div class="fill ${i === 0 ? "lead" : i === 3 ? "last" : ""}" style="width:${Math.max(1.5, 100 * s.value / max)}%"></div></div>
      <span class="n">${n(s.value, 0)}</span>
      <span class="drop ${i ? s.drop.tone : ""}">${i ? `−${n(F.steps[i - 1].value - s.value, 0)} ${esc(s.drop.label)}` : ""}</span>
    </button>`).join("");
  box.onclick = e => { const b = e.target.closest("[data-gap]"); if (!b) return; FUNNEL_GAP = +b.dataset.gap; renderFunnel(); };
  const step = F.steps[FUNNEL_GAP], top = (F.reasons[FUNNEL_GAP] || []).slice(0, 7);
  const most = Math.max(1, ...top.map(x => x.n));
  list.innerHTML = `<div class="reasons-h"><b>${n(F.steps[FUNNEL_GAP - 1].value - step.value, 0)} ${esc(step.drop.label)}</b>
      <span class="mut">— ${esc(step.drop.why)}${FUNNEL_GAP === 2 ? " One candle can fail several conditions, so these add up to more." : ""}</span></div>`
    + (top.length ? top.map(v => `
    <div class="reason ${v.tone}">
      <div><div>${esc(v.say)}</div>${v.mean ? `<div class="why">${esc(v.mean)}</div>` : ""}</div>
      <span class="rbar"><i style="width:${Math.max(2, 100 * v.n / most)}%"></i></span>
      <span class="n" title="${esc(v.code || "")}">${n(v.n, 0)}</span>
    </div>`).join("") : `<div class="empty">Nothing dropped out at this step today.</div>`);
}

// Seatbelts, drawn: a gauge for how much it may still lose today and a ladder showing how far the account is
// below its best against the points where trade sizes are cut. Both update in place, so they slide rather
// than redraw. The tiers come from the settings file through /api/config.
let RISK_CFG = null;
async function loadRiskCfg() {
  try { RISK_CFG = ((await (await fetch("/api/config")).json()).app || {}).risk || null; } catch (_) {}
}
function renderRiskViz(r) {
  const gEl = $("#riskGauge"), lEl = $("#riskLadder");
  if (gEl) {
    const lim = RISK_CFG && r.day_start_equity ? RISK_CFG.daily_loss_limit_fraction * r.day_start_equity : null;
    const left = r.daily_loss_remaining;
    VZ.gauge(gEl, {value: left, max: lim || left || 1, text: n(left), sub: lim ? `of ${n(lim)} left` : "left today",
      label: `Can still lose today${RISK_CFG ? ` <span class="mut">(${pct(RISK_CFG.daily_loss_limit_fraction, 0)} of the day's start)</span>` : ""}`});
  }
  if (!lEl || !RISK_CFG) return;
  const c = RISK_CFG, top = Math.max(0.35, (c.dd_emergency || 0.3) + 0.05), P = v => Math.min(100, v / top * 100);
  if (!lEl._built) {
    lEl._built = true;
    const tiers = [[0, c.dd_reduced, "full size", "good"], [c.dd_reduced, c.dd_restricted, "half", "wait"],
      [c.dd_restricted, c.dd_no_new_trades, "quarter", "wait"], [c.dd_no_new_trades, c.dd_emergency, "no new trades", "bad"],
      [c.dd_emergency, top, "emergency", "bad"]];
    lEl.innerHTML = `<div class="ld-h">${defn("DRAWDOWN", "How far below its best")}</div>
      <div class="ld-track">${tiers.map(([a, b, name, tone]) => `<i class="ld-seg ${tone}" style="left:${P(a)}%;width:${P(b) - P(a)}%"
          data-tip="${esc(`${pct(a, 0)} to ${pct(b, 0)} below its best: ${name === "full size" ? "trades at full size" : name === "half" ? "every new trade at half size" : name === "quarter" ? "every new trade at a quarter size" : name === "no new trades" ? "opens no new trades" : "emergency stop: everything new halted"}`)}"></i>`).join("")}
        <i class="ld-mark"></i></div>
      <div class="ld-ticks">${tiers.map(([a, , name]) => `<span style="left:${P(a)}%">${a ? "−" + pct(a, 0) : "0"}<em>${name}</em></span>`).join("")}</div>
      <div class="ld-now"></div>`;
  }
  const dd = Math.max(0, +r.drawdown || 0);
  lEl.querySelector(".ld-mark").style.left = P(dd) + "%";
  lEl.querySelector(".ld-now").innerHTML = `Now <b>${pct(dd, 1)}</b> below its best of ${n(r.peak_equity)} — ${esc(term(r.state || "UNKNOWN").say.toLowerCase())}.`;
}

async function loadOverviewSlow() {
  loadEquity();
  loadFunnel();
  try {
    const cf = await (await fetch("/api/counterfactuals/summary?days=1")).json();
    const r = cf.counterfactual_r;
    const verdict = r == null ? "Not enough skipped trades to judge yet."
      : r > 0.1 ? "The skipped trades would have made money on balance — the rules may be too strict."
      : r < -0.1 ? "The skipped trades would have lost money — the rules are earning their keep."
      : "The skipped trades roughly break even.";
    const won = +cf.would_have_won || 0, lost = +cf.would_have_lost || 0, open = +cf.still_open || 0, all = won + lost + open;
    const seg = (v, c, t) => v ? `<i class="${c}" style="width:${100 * v / all}%" data-tip="${esc(t)}"></i>` : "";
    $("#cf").innerHTML = (all ? `<div class="stack" aria-label="Skipped trades: would have won, would have lost, still open">
        ${seg(won, "good", `${won} would have won — the check may have cost money there`)}${seg(lost, "bad", `${lost} would have lost — the check saved money there`)}${seg(open, "mut", `${open} still being followed on paper`)}</div>` : "")
      + `<div class="kv">
      <span>Signals it chose not to take</span><span>${cf.rejected ?? "–"}</span>
      <span>Those that would have won</span><span class="good">${cf.would_have_won ?? "–"}</span>
      <span>Those that would have lost</span><span class="bad">${cf.would_have_lost ?? "–"}</span>
      <span>Still running on paper</span><span>${cf.still_open ?? "–"}</span>
      <span>Net, in risk units</span><span class="${cls(r)}">${signed(r)}R</span>
      <span>Rule that cost the most</span><span>${esc(say(cf.most_expensive_gate || "–"))}</span>
      <span>Rule that saved the most</span><span>${esc(say(cf.most_valuable_gate || "–"))}</span></div>
      <div class="note">${esc(verdict)} ${defn("R", "“R”")} means one trade's risk: +1R is a win the size of what was risked.</div>`;
  } catch (_) {}
}

function renderRealtime(s) {
  const a = s.account || {}, m = s.market || {};
  kpis($("#top"), [[defn("BALANCE", "Balance"), n(a.balance)], [defn("EQUITY", "Worth now"), n(a.equity)],
    [defn("FLOATING", "Open trades"), signed(a.floating_pnl), cls(a.floating_pnl)],
    ["Banked today", signed(a.today_realized), cls(a.today_realized)], [defn("MARGIN", "Margin in use"), n(a.margin)],
    [defn("SPREAD", "Buy/sell gap"), n(m.spread_points, 0) + " pts"],
    ["Broker response", m.broker_latency_ms != null ? n(m.broker_latency_ms, 0) + " ms" : "–"]]);
  renderOpenPositions(s);
  const gapShare = m.spread_points != null && m.spread_limit_points ? m.spread_points / m.spread_limit_points : null;
  if ($("#gapGauge")) VZ.gauge($("#gapGauge"), {value: m.spread_points, max: m.spread_limit_points || m.spread_points || 1,
    text: m.spread_points == null ? "–" : n(m.spread_points, 0) + " pts", sub: m.spread_limit_points ? `of ${n(m.spread_limit_points, 0)} allowed` : "",
    tone: gapShare == null ? "mut" : gapShare > 1 ? "bad" : gapShare > 0.8 ? "wait" : "good",
    label: `${defn("SPREAD", "Buy/sell gap")} on ${esc(s.symbol || "this market")} — ${gapShare == null ? "no limit known"
      : gapShare > 1 ? "too wide: it will not trade here now" : gapShare > 0.8 ? "close to its limit" : "comfortably inside its limit"}`});
  $("#market").innerHTML = `<div class="kv">
    <span>Sell price (bid)</span><span>${n(m.bid)}</span>
    <span>Buy price (ask)</span><span>${n(m.ask)}</span>
    <span>Gap between them</span><span class="${gapShare != null && gapShare > 1 ? "bad" : gapShare != null && gapShare > .8 ? "wait" : ""}">${n(m.spread_points, 0)} pts${m.spread_limit_points != null ? ` of ${n(m.spread_limit_points, 0)} allowed` : ""}</span>
    ${m.spread_limit_basis ? `<span>Where that limit comes from</span><span class="mut">${esc(m.spread_limit_basis)}</span>` : ""}
    <span>Wider than usual?</span><span>${m.spread_pctile != null ? (m.spread_pctile > .9 ? "yes, unusually wide" : m.spread_pctile > .6 ? "a little" : "no, normal") : "–"}</span>
    <span>${defn("ATR", "How much it moves (ATR)")}</span><span>${n(m.atr_m15)}</span>
    <span>Speed right now</span><span>${esc(say(m.volatility_state || "–"))}</span>
    <span>Trading session</span><span>${esc(String(m.session || "–").replace(/_/g, " ").toLowerCase())}</span>
    <span>${defn("REGIME", "Market shape")}</span><span>${esc(say(m.regime || "–"))}</span>
    <span>News window</span><span>${esc(say(m.event_state || "–"))}</span>
    <span>Next big release</span><span>${m.next_event ? esc(tm(m.next_event.time).slice(0, 16) + " " + m.next_event.title) : "none scheduled"}</span>
    <span>Backtests on this symbol</span><span class="${m.research_block ? "bad" : "good"}">${m.research_block
      ? esc("blocking " + m.research_block.strategies.join(", ")) : "not blocking anything"}</span>
    ${m.research_block && m.research_block.reason
      ? `<span>Why it is blocked</span><span class="mut">${esc(m.research_block.reason)}</span>` : ""}
    <span>Price feed delay</span><span>${m.tick_age_sec != null ? n(m.tick_age_sec, 1) + " s" : "–"}</span>
    <span>Price updates</span><span>${m.tick_rate != null ? n(m.tick_rate, 2) + "/s" : "–"}</span>
    <span>Average slippage</span><span>${s.slippage && s.slippage.mean != null ? n(s.slippage.mean, 1) + " pts over " + s.slippage.n + " fills" : "–"}</span></div>`;

  const blockers = s.blockers || [];
  const line = (label, body) => `<div style="margin-bottom:8px"><b>${esc(label)}</b> — ${body}</div>`;
  const sys = blockers.length
    ? blockers.map(b => `<span class="wait">${esc(explain(b))}</span>`).join("; ")
    : '<span class="good">nothing is in the way</span>';
  const strat = Object.entries(s.strategies || {}).map(([id, v]) => {
    const d = (v.diagnostics || []);
    return line(id, d.length ? d.map(x => `${esc(say(x))} <span class="code">${esc(x)}</span>`).join("; ") : "no reading yet");
  }).join("");
  $("#why").innerHTML = `<div class="why">${line("The system itself", sys)}${strat}
    ${(s.pending_signals || []).length
      ? line("Waiting on", s.pending_signals.map(p => esc(say(p.reason))).join("; ")) : ""}</div>`;
}

async function loadToday() {
  const rows = await (await fetch("/api/trades/today")).json();
  table($("#today"), rows, [["closed_at", "Closed", v => tm(v).slice(11, 16)], ["symbol", "Symbol"],
    ["strategy_id", "Rule"], ["direction", "Bought or sold", v => String(v || "").toLowerCase()],
    ["filled_volume", "Size", v => n(v, 2)], ["entry_price", "In at", autoPx], ["exit_price", "Out at", autoPx],
    ["net_pnl", "Made / lost", v => signed(v), cls],
    ["r_multiple", "vs. what it risked", v => v == null ? "–" : signed(v) + "R", cls],
    ["exit_reason", "How it ended", (v, row) => {
      // "SL" only means the stop order filled. Whether that was a win is decided by the money.
      const pnl = row && row.net_pnl;
      if (pnl != null && +pnl > 0 && /^(SL|STOP_OUT)$/i.test(String(v))) return "protected win (trailed stop)";
      return say(v);
    }]],
    "No trades have finished today yet.");
  storyRows($("#today"), rows);
}

// ------------------------------------------------------------------ logs
const LOG_COLS = {
  execution: [["created_at", "Time", tm], ["event_type", "What happened", v => say(v)], ["retcode_text", "Broker said", v => say(v)],
    ["requested_volume", "Size asked for", v => n(v, 2)], ["filled_volume", "Size filled", v => n(v, 2)],
    ["requested_price", "Price wanted", v => n(v)], ["price", "Price got", v => n(v)],
    ["slippage_points", "Difference (pts)", v => n(v, 1)], ["latency_ms", "Took (ms)", v => n(v, 0)],
    ["error", "Problem"], ["retcode", "Code"]],
  errors: [["created_at", "Time", tm], ["level", "How bad", v => v === "CRITICAL" ? "serious" : v === "ERROR" ? "error" : "warning", v => v === "ERROR" || v === "CRITICAL" ? "bad" : "wait"], ["logger", "Where from"], ["message", "What happened"]],
  signals: [["created_at", "Time", tm], ["strategy_id", "Strategy"], ["direction", "Dir"], ["entry", "Entry", v => n(v)], ["sl", "SL", v => n(v)], ["tp", "TP", v => n(v)],
    ["quality", "Quality"], ["edge_r", "Net edge R", v => n(v, 3)], ["cost_r", "Cost R", v => n(v, 3)], ["regime", "Regime"], ["session", "Session"], ["event_state", "Event"], ["status", "Status"]],
  rejected: [["created_at", "Time", tm], ["strategy_id", "Strategy"], ["symbol", "Symbol"], ["direction", "Dir"], ["reason", "Why not", v => say(v)], ["gate", "Check that stopped it", v => say(v)],
    ["hypothetical_r", "Would have been", v => v == null ? "–" : signed(v) + "R", cls], ["cf_status", "Followed up"]],
  candidate_evaluations: [["created_at", "Evaluated", tm], ["symbol", "Symbol"], ["strategy_id", "Strategy"], ["stage", "Got as far as", v => say(v)],
    ["outcome", "Result", v => say(v)], ["reason", "Why", v => say(v)], ["gate", "Check", v => say(v)]],
  counterfactuals: [["created_at", "Time", tm], ["strategy_id", "Strategy"], ["direction", "Dir"], ["gate", "Gate"], ["reason", "Reason"], ["status", "Status"],
    ["hypothetical_fill", "Fill", v => n(v)], ["hypothetical_exit", "Exit", v => n(v)], ["exit_reason", "Exit reason"], ["mfe_r", "MFE R", v => n(v)],
    ["mae_r", "MAE R", v => n(v)], ["hypothetical_r", "R", v => signed(v), cls], ["bars_held", "Bars"]],
  risk: [["created_at", "Time", tm], ["state", "Risk setting", v => say(v), STATUS_CLASS], ["risk_multiplier", "Size", v => v == null ? "–" : Math.round(v * 100) + "%"],
    ["equity", "Account worth", v => n(v)], ["peak_equity", "Best ever", v => n(v)],
    ["drawdown", "Down from best", v => pct(v)], ["daily_pnl", "That day", v => signed(v), cls],
    ["daily_loss_remaining", "Could still lose", v => n(v)], ["reason", "Why it changed", v => say(v)]],
  reconciliation: [["created_at", "Time", tm], ["trade_id", "Trade", v => String(v).slice(0, 8)], ["event_type", "Event"], ["from_state", "From"], ["to_state", "To"], ["details", "Details"]],
  health: [["created_at", "Time", tm], ["component", "Part"], ["status", "State", v => say(v), STATUS_CLASS], ["startup_state", "Startup", v => say(v)], ["message", "Detail"]],
  research: [["created_at", "Time", tm], ["kind", "Kind"], ["strategy_id", "Strategy"], ["hypothesis_id", "Hypothesis"], ["n_trades", "Trades"],
    ["expectancy_r", "Expectancy R", v => signed(v, 3), cls], ["evidence_level", "Evidence"], ["report_path", "Report"], ["id", "Experiment id"]],
  trades: [["created_at", "Created", tm], ["strategy_id", "Strategy"], ["direction", "Side", v => String(v || "").toLowerCase()], ["state", "Where it got to", v => say(v)], ["filled_volume", "Size", v => n(v, 2)], ["entry_price", "In at", v => n(v)],
    ["exit_price", "Exit", v => n(v)], ["net_pnl", "Made / lost", v => signed(v), cls], ["r_multiple", "vs. risked", v => v == null ? "–" : signed(v) + "R", cls],
    ["mfe_r", "Best it reached", v => v == null ? "–" : signed(v) + "R"],
    ["giveback_r", "Given back", v => v == null ? "–" : n(v) + "R"], ["exit_reason", "How it ended", v => say(v)]],
  config: [["created_at", "Time", tm], ["actor", "Changed by"], ["action", "What", v => say(v)], ["path", "Setting"], ["old_value", "Was"], ["new_value", "Now"], ["reason", "Why"]],
};
const TAB_NAMES = {trades: "Trades", execution: "Orders sent", signals: "Signals found",
  rejected: "Trades not taken", candidate_evaluations: "Every chance it looked at",
  counterfactuals: "What skipped trades would have done", risk: "Risk changes", errors: "Problems",
  reconciliation: "Broker checks", health: "System checks", research: "Research runs", config: "Setting changes"};
// One line per tab on what it holds and when it is worth opening, for someone who has never seen these records.
const TAB_NOTES = {
  trades: "Every trade the bot has tried to make, newest first — including ones cancelled before they filled. Click a finished one for its story.",
  execution: "Every message to and from the broker: orders sent, fills, stop moves. Look here when a trade did something unexpected.",
  signals: "Every time a rule's setup appeared on a chart, whether or not it became a trade.",
  rejected: "Signals a safety, cost or risk check refused, and what each would have made or lost on paper afterwards.",
  candidate_evaluations: "Every single chance a rule looked at and where it stopped — the raw data behind the funnel on the Overview.",
  counterfactuals: "Refused signals followed on paper to their stop or target, so you can see whether refusing them was right.",
  risk: "Each time the risk setting changed — for example sizes cut after the account fell from its best.",
  errors: "Warnings and errors the bot logged. A few are normal; a burst of them is worth a look.",
  reconciliation: "Checks that the bot's own record matches what the broker shows.",
  health: "Connection, price-feed and database checks over time.",
  research: "Backtest runs from the research lab and what each one found.",
  config: "Every setting change: who made it, when, and the reason they gave.",
};
let TAB = "trades";
async function loadLogs() {
  const rows = await (await fetch(`/api/logs/${TAB}?limit=${$("#limit").value}`)).json();
  if ($("#tabnote")) $("#tabnote").textContent = TAB_NOTES[TAB] || "";
  table($("#logtable"), rows, LOG_COLS[TAB], "No records");
  if (TAB === "trades") storyRows($("#logtable"), rows); else { $("#logtable").onclick = null; $("#logtable").onkeydown = null; }
  if (TAB === "rejected" || TAB === "counterfactuals") {
    const cf = await (await fetch("/api/counterfactuals/summary?days=" + $("#cfdays").value)).json();
    $("#cfsum").innerHTML = `Rejected: <b>${cf.rejected}</b> · would have won <b class="ok">${cf.would_have_won}</b> · lost <b class="bad">${cf.would_have_lost}</b> · open ${cf.still_open} · counterfactual <b class="${cls(cf.counterfactual_r)}">${signed(cf.counterfactual_r)} R</b> · most expensive gate <b>${cf.most_expensive_gate || "–"}</b> · most valuable gate <b>${cf.most_valuable_gate || "–"}</b>`;
    $("#cfsum").style.display = "block";
  } else if (TAB === "candidate_evaluations") {
    const funnel = await (await fetch("/api/candidates/summary?days=" + $("#cfdays").value)).json();
    if (!funnel.error) {
      const top = funnel.groups.slice(0, 12).map(g =>
        `<tr><td>${esc(g.symbol)}</td><td>${esc(g.strategy_id)}</td><td>${esc(g.stage)}</td><td>${esc(g.gate)}</td><td>${esc(g.reason)}</td><td>${g.count}</td><td>${esc(JSON.stringify(g.outcomes))}</td></tr>`).join("");
      $("#cfsum").innerHTML = `<b>${funnel.evaluations}</b> strategy evaluations in ${n(funnel.days, 1)} days
        <div class="scroll"><table><thead><tr><th>Symbol</th><th>Strategy</th><th>Stage</th><th>Gate</th><th>Reason</th><th>Count</th><th>Outcomes</th></tr></thead>
        <tbody>${top || '<tr><td colspan="7">No candidate evaluations recorded</td></tr>'}</tbody></table></div>`;
      $("#cfsum").style.display = "block";
    } else {
      $("#cfsum").textContent = funnel.error;
      $("#cfsum").style.display = "block";
    }
  } else $("#cfsum").style.display = "none";
}
function initLogs() {
  $("#tabs").innerHTML = Object.entries(TAB_NAMES).map(([k, v]) => `<button data-t="${k}" class="${k === TAB ? "on" : ""}">${v}</button>`).join("");
  $("#tabs").onclick = e => { const t = e.target.dataset.t; if (!t) return; TAB = t; [...$("#tabs").children].forEach(b => b.classList.toggle("on", b.dataset.t === t)); loadLogs(); };
  $("#reload").onclick = loadLogs; loadLogs(); setInterval(loadLogs, 10000);
}

// ------------------------------------------------------------------ config
function getPath(o, p) { return p.split(".").reduce((a, k) => a == null ? a : a[k], o); }
let CFG = null;
async function initConfig() {
  CFG = await (await fetch("/api/config")).json();
  const app = CFG.app;
  const envWord = {DEMO: "Practice account — real prices, pretend money",
                   DRY_RUN: "Paper trading — live prices, no orders sent",
                   SHADOW: "Watching only — it records signals but never orders",
                   REAL: "REAL MONEY"}[app.environment] || app.environment;
  const straddle = app.news_straddle || {};
  $("#envinfo").innerHTML = `<div class="kv">
    <span>Account type</span><span><b class="env ${app.environment}">${esc(envWord)}</b></span>
    <span>Symbols it watches</span><span>${app.broker.symbol_candidates.length}</span>
    <span>News trading</span><span>${straddle.enabled ? `on, ${(straddle.symbols || []).length} symbols` : "off"}</span>
    <span>Retunes itself hourly</span><span>${app.adaptive && app.adaptive.enabled ? "on" : "off"}</span>
    <span>Learned filter</span><span>${app.ml_filter && app.ml_filter.enabled ? app.ml_filter.mode : "off"}</span>
    <span>Alerts</span><span>${app.alerts && app.alerts.enabled ? esc(app.alerts.channel) : "off"}</span>
    <span>Pending orders allowed</span><span>${app.execution.allow_limit_orders ? "yes" : "no"}</span>
    <span>Settings version</span><span>${app.config_version}</span></div>
    <div class="note">The account type is changed only by editing <span class="code">config/config.json</span> and
    restarting — never from a browser. Real-money order execution is disabled in this release.</div>`;
  const rows = CFG.strategies.strategies.map(s => ({...s, stats_level: (s.stats || {}).evidence_level || "INSUFFICIENT", oos: (s.stats || {}).oos_expectancy_r}));
  table($("#cstrats"), rows, [["id", "Strategy"], ["version", "Version"], ["status", "Promotion status"], ["enabled", "Enabled", v => v ? "yes" : "no", v => v ? "ok" : "bad"],
    ["allowed_regimes", "Regimes", v => (v || []).join(",") || "all"], ["allowed_sessions", "Sessions", v => (v || []).join(",") || "all (measured)"],
    ["stats_level", "Evidence"], ["oos", "OOS exp. R", v => n(v, 3)], ["id", "", (v, r) => r.enabled ? "▶ disable" : "▶ enable"]]);
  $("#cstrats").onclick = async e => { const td = e.target.closest("td"); if (!td || !td.textContent.startsWith("▶")) return;
    const id = td.parentElement.children[0].textContent; const en = td.textContent.includes("enable") && !td.textContent.includes("disable");
    const reason = prompt(`${en ? "Enable" : "Disable"} ${id} - reason:`); if (!reason) return;
    await control(en ? "enable_strategy" : "disable_strategy", {strategy_id: id, reason}); initConfig(); };
  $("#params").innerHTML = `<table><thead><tr><th>Parameter</th><th>Current</th><th>New value</th><th>Ceiling</th><th></th></tr></thead><tbody>${
    CFG.runtime_editable.map(p => { const v = getPath(app, p); const input = typeof v === "boolean" ? `<select data-p="${p}"><option ${v ? "selected" : ""}>true</option><option ${!v ? "selected" : ""}>false</option></select>`
      : (p === "events.mode" ? `<select data-p="${p}"><option ${v === "block" ? "selected" : ""}>block</option><option ${v === "observe" ? "selected" : ""}>observe</option></select>` : `<input data-p="${p}" value="${v}" size="10">`);
      return `<tr><td>${p}</td><td>${v}</td><td>${input}</td><td>${CFG.safety_ceilings[p] ?? ""}</td><td><button class="btn" data-apply="${p}">Apply</button></td></tr>`; }).join("")}</tbody></table>`;
  $("#params").onclick = async e => { const p = e.target.dataset.apply; if (!p) return; const el = $(`[data-p="${p}"]`);
    const reason = prompt(`Change ${p} to ${el.value} - reason (required):`); if (!reason) return;
    await control("set_config", {path: p, value: el.value, reason}); initConfig(); };
  const hist = await (await fetch("/api/logs/config?limit=50")).json();
  table($("#hist"), hist, LOG_COLS.config, "No changes recorded yet");
}
// ------------------------------------------------------------------ how strictly it judges itself
// Every row here was invisible until something went looking for it. A ceiling equal to the number of
// things it limits, a gate that permits whenever evidence is missing, one evidence bar shared by eight
// rules that behave nothing alike. None of them announced itself, and each read like a working control.
async function loadIntegrity() {
  const el = $("#integrity"); if (!el) return;
  let d;
  try { d = await (await fetch("/api/integrity")).json(); }
  catch (e) { el.innerHTML = `<div class="empty">Could not load: ${esc(e.message)}</div>`; return; }

  const r = d.research || {}, p = d.portfolio_risk || {}, ns = d.news_straddle || {}, c = d.cost || {};
  const warn = t => `<b class="wait">${t}</b>`;
  const diversified = p.mode === "diversified";

  el.innerHTML = `<div class="kv">
    <span>Rules researched</span><span>${r.researched} of ${Object.keys(r.thresholds || {}).length}</span>
    <span>Rules that may refuse a trade</span><span>${(r.gated || []).length
      ? (r.gated || []).map(s => (r.gated_disabled || []).includes(s)
          ? `${esc(s)} ${warn("(switched off - the gate has nothing to refuse)")}` : esc(s)).join(", ")
      : warn("none")}</span>
    ${(r.record_only || []).length ? `<span>Researched, cannot refuse</span><span class="mut">${esc(r.record_only.join(", "))}</span>` : ""}

    <span>When research is missing or stale</span><span class="${r.require_evidence ? "good" : "wait"}">${
      r.require_evidence ? "refuse the trade" : "allow the trade"}</span>
    <span>Evidence bar per rule</span><span>${r.all_on_global_bar
      ? warn("the same for every rule") : "set individually"}</span>

    <span>Several trades at once</span><span>${diversified
      ? "<b class='good'>counted as a portfolio</b>" : warn("each counted as separate risk")}</span>
    <span>Most at risk at one time</span><span>${n(p.max_total_open_risk_fraction * 100, 1)}% of the account${
      diversified ? `, hard cap ${n(p.max_gross_open_risk_fraction * 100, 1)}%` : ""}</span>
    ${diversified ? `<span>Assumed in a crisis</span><span>everything moves together at ${n(p.stress_correlation, 2)}</span>` : ""}
    <span>Most positions at once</span><span>${p.max_concurrent_positions}</span>

    <span>News trades armed at once</span><span>${!ns.enabled ? "off"
      : (ns.ceiling_binds ? `at most ${ns.max_armed_symbols} of ${ns.symbols} symbols`
         : warn(`${ns.max_armed_symbols} of ${ns.symbols} — limits nothing`))}</span>

    <span>Commission per lot</span><span>${(c.commission_per_lot_round_trip || c.per_symbol_overrides)
      ? n(c.commission_per_lot_round_trip, 2) : warn("zero — correct only on an Exness Standard account")}</span>
    ${d.adaptive && d.adaptive.enabled
      ? `<span>Hourly spread limit expires after</span><span>${d.adaptive.max_spread_cap_age_minutes} min</span>` : ""}
  </div>`;

  const checks = d.real_money_checks || [];
  table($("#readiness"), checks, [
    ["name", "Check"],
    ["pass", "", v => v ? "ready" : "not yet", v => v ? "ok" : "bad"],
    ["got", "Right now"],
    ["needs", "Needs to be"],
    ["why", "Why it matters"],
  ], "No configuration checks available");
}

function renderConfigState(s) {
  const k = s.kill_switch || {};
  const monitor = s.trading_status || {};
  const vol = (k.volatile || []);
  $("#killstate").innerHTML = `<div class="kv">
    <span>Right now</span><span class="${monitor.is_trading_allowed ? "good" : "wait"}">${esc(say(s.system_status || "UNKNOWN"))}</span>
    ${monitor.is_trading_allowed ? "" : `<span>Paused for</span><span>${duration(monitor.downtime_seconds)}</span>`}
    <span>Last pause lasted</span><span>${monitor.last_downtime_seconds == null ? "–" : duration(monitor.last_downtime_seconds)}</span>
    <span>Paused in total</span><span>${duration(monitor.total_downtime_seconds)}</span>
    <span>Manual stop</span><span class="${k.engaged ? "wait" : "good"}">${k.engaged ? "on" : "off"}</span>
    ${k.reason ? `<span>Why</span><span>${esc(explain(k.reason))}</span>` : ""}
    ${k.by ? `<span>Stopped by</span><span>${esc(k.by)}${k.at ? " " + esc(tm(k.at)) : ""}</span>` : ""}
    ${vol.length ? `<span>Automatic stops</span><span>${vol.map(v => esc(say(v))).join(", ")}</span>` : ""}</div>`;
  renderStatusReasons(s, $("#configStatusReasons"));
}
function initConfigActions() {
  $("#a_halt").onclick = () => onKill();
  $("#a_force_resume").onclick = async () => {
    const confirmed = confirm("Recheck account, margin, health, risk limits, and loss cooldown, then resume only if all gates pass? Genuine drawdown/daily-loss blocks will not be cleared.");
    if (!confirmed) return;
    const reason = prompt("Reason for force reset and resume (required):");
    if (!reason) return;
    await control("force_reset_resume", {reason});
  };
  $("#a_close").onclick = async () => { const c = prompt("Close ALL bot-managed positions at market. Type CLOSE_ALL to confirm:"); if (c !== "CLOSE_ALL") return;
    const reason = prompt("Reason:") || "manual"; await control("close_all", {confirm: c, reason}); };
  $("#a_recon").onclick = () => control("reconcile_now", {});
  $("#a_events").onclick = () => control("reload_events", {});
  $("#a_token").onclick = () => { localStorage.removeItem("ata_token"); toast("Token forgotten on this browser"); };
}

// ------------------------------------------------------------------ boot
// Called by start() at the bottom of this file, once everything the page needs has loaded.
function boot() {
  header();
  refresh(); setInterval(refresh, 1500); connectWS();
  // The charts redraw themselves on resize, so a resize no longer refetches everything.
  if (PAGE === "overview") { loadRiskCfg(); loadOverviewSlow(); setInterval(loadOverviewSlow, 30000);
    $("#eqh").onchange = loadEquity; if ($("#eqkey")) $("#eqkey").onchange = loadEquity; }
  if (PAGE === "realtime") { loadToday(); setInterval(loadToday, 15000); fetch("/api/events?n=150").then(r => r.json()).then(es => es.forEach(e => pushFeed(e))); }
  if (PAGE === "logs") initLogs();
  if (PAGE === "analysis") initAnalysis();
  if (PAGE === "config") { initConfig(); initConfigActions(); loadIntegrity(); setInterval(loadIntegrity, 60000); }
}


// ------------------------------------------------------------------ analysis page: losses + blocked signals
function sigNotes(sg) {
  if (!sg || !Object.keys(sg).length) return "no signal record";
  const b = [];
  if (sg.trigger) b.push(`trigger ${sg.trigger}`);
  if (sg.quality != null) b.push(`quality ${n(sg.quality, 0)}`);
  if (sg.edge_r != null) b.push(`edge ${n(sg.edge_r, 2)}R`);
  if (sg.cost_r != null) b.push(`cost ${n(sg.cost_r, 2)}R`);
  if (sg.regime) b.push(`regime ${sg.regime}`);
  if (sg.session) b.push(`session ${sg.session}`);
  if (sg.event_state) b.push(`news ${sg.event_state}`);
  const m = sg.microstructure; if (m && m.status === "OK") b.push(`flow: CVD ${m.cvd}, VWAPσ ${m.vwap_dev_sigma}, VPIN ${m.vpin}`);
  return b.join(" · ");
}

async function loadAnalysis() {
  const hl = $("#lhours").value, hb = $("#bhours").value, only = $("#bonly").checked ? "&only=positive" : "";
  const [L, B] = await Promise.all([fetch(`/api/analysis/losses?hours=${hl}&limit=100`).then(r => r.json()), fetch(`/api/analysis/blocked?hours=${hb}&limit=200${only}`).then(r => r.json())]);
  const ls = L.summary || {};
  $("#lsum").innerHTML = !ls.losing_trades
    ? "Nothing lost money in this window."
    : `<b>${ls.losing_trades}</b> losing trades, costing <b class="bad">${n(ls.total_loss, 2)}</b> in total. `
      + `Worst rules: ${Object.entries(ls.by_strategy || {}).sort((a, b) => a[1].loss - b[1].loss).slice(0, 4)
          .map(([k, v]) => `${esc(k)} ${n(v.loss, 2)} over ${v.trades}`).join(", ") || "none"}. `
      + `How they ended: ${Object.entries(ls.by_exit_reason || {})
          .map(([k, v]) => `${esc(say(k).toLowerCase())} ${v.trades}`).join(", ") || "not recorded"}.`;
  const lrows = (L.rows || []).map(r => ({...r, setup: sigNotes(r.signal)}));
  table($("#ltable"), lrows,
    [["closed_at", "Closed", tm], ["symbol", "Symbol"], ["strategy", "Rule"],
     ["direction", "Side", v => String(v || "").toLowerCase()], ["net_pnl", "Lost", v => n(v, 2), () => "bad"],
     ["r_multiple", "vs. risked", v => v == null ? "–" : n(v, 2) + "R"],
     ["outcome", "How it ended"], ["exit_reason", "Broker said", v => say(v)],
     ["mfe_r", "Best it got to", v => v == null ? "–" : signed(v, 2) + "R", v => v > 0.3 ? "wait" : ""],
     ["mae_r", "Worst it got to", v => v == null ? "–" : n(v, 2) + "R"],
     ["bars_held", "Candles held"], ["setup", "What it saw when it entered"]],
    "No losing trades in this window. ");
  storyRows($("#ltable"), lrows, r => r.r_multiple != null);
  const bs = B.summary || {};
  $("#bsum").innerHTML = !bs.blocked
    ? "Every signal in this window was taken — nothing was blocked."
    : `<b>${bs.blocked}</b> signals were not taken. Reasons: `
      + `${Object.entries(bs.by_category || {}).map(([k, v]) => `${esc(say(k).toLowerCase())} ${v}`).join(", ") || "not recorded"}.`
      + (bs.positive_calc_blocked_by_system
          ? ` <b class="bad">${bs.positive_calc_blocked_by_system}</b> of them were signals the bot had already
             calculated as worth taking, stopped by a system problem rather than by a deliberate rule — those are
             the ones worth looking at.`
          : " None of them were signals the bot had calculated as worth taking, so nothing was lost to a system problem.");
  table($("#btable"), (B.rows || []).map(r => ({...r, setup: sigNotes(r.signal)})),
    [["created_at", "Time", tm], ["symbol", "Symbol"], ["strategy", "Rule"],
     ["direction", "Side", v => String(v || "").toLowerCase()],
     ["category", "Stopped by", v => say(v), v => /SYSTEM/.test(v) ? "bad" : ""],
     ["reason", "Exactly why", v => explain(v)],
     ["calc_positive", "It thought this would win", v => v ? "yes" : "no", v => v ? "wait" : ""],
     ["setup", "What it saw"]],
    "Nothing was blocked in this window.");
}
async function loadLab() {
  const d = await fetch("/api/research/lab").then(r => r.json());
  renderTimeframes(d);
  const syms = ((d.latest || {}).symbols) || {};
  const gates = d.live_gate || {}, gstrat = (d.gate_strategies || []).join(", ") || "strategy";
  const f = (m, k, nd) => m && m[k] != null ? n(m[k], nd) : "–";
  const live = s => { const g = gates[s]; if (!g) return "–";
    return g.blocked ? `BLOCKED ${gstrat} ${String(g.reason || "").replace(/^RESEARCH_LAB_NO_EDGE /, "")}` : `allowed - ${g.reason}`; };
  table($("#rtable"), Object.entries(syms).map(([s, e]) => ({symbol: s, verdict: e.verdict, signals: e.signals, best: e.best ? `SL ${e.best.sl_atr}×ATR · RR ${e.best.rr}` : "–",
      train: e.train ? `${f(e.train, "n_trades", 0)} tr · ${f(e.train, "expectancy_r", 2)}R` : "–", test: e.test ? `${f(e.test, "n_trades", 0)} tr · ${f(e.test, "expectancy_r", 2)}R · PF ${f(e.test, "profit_factor", 2)}` : "–",
      bt: e.backtrader && e.backtrader.n_trades != null ? `${f(e.backtrader, "n_trades", 0)} tr · ${f(e.backtrader, "expectancy_r", 2)}R` : (e.backtrader && e.backtrader.error ? "error" : "–"), note: e.note || "", live: live(s)})),
    [["symbol", "Symbol"], ["verdict", "Verdict", null, v => v === "ROBUST_ON_TEST" ? "ok" : (v === "NOT_ROBUST_ON_TEST" ? "bad" : "")], ["signals", "Signals"], ["best", "Best on train"], ["train", "Train"], ["test", "Test (unseen)"], ["bt", "Backtrader check"], ["live", "Live trading", null, v => /^BLOCKED/.test(v) ? "bad" : ""], ["note", "Note"]],
    "No research results yet - the lab starts ~2 minutes after the bot and repeats every 6 hours");
  renderLabForest(d);
}

// ------------------------------------------------------------------ analysis page: the replay report
// research/results/backtest/latest.json (tools/backtest_report.py, refreshed by the nightly lab run): every rule
// over all the broker's history, pooled across symbols. Total, worst drawdown and worst month say "how bad"; the
// unseen column is the only part that counts as evidence.
let RP = null, RP_CHART = null, RP_PICK = null;
const rMonth = m => m ? `${m[0]} (${signed(m[1], 1)}R)` : "–";
const rRange = s => s && s.avg_r != null ? `${signed(s.avg_r, 3)}R` + (s.lo != null ? ` [${signed(s.lo, 2)}, ${signed(s.hi, 2)}]` : "") : "–";
const rTone = s => !s || s.lo == null ? "" : (s.lo > 0 ? "ok" : (s.hi < 0 ? "bad" : ""));
async function loadReplay() {
  const el = $("#rpTable"); if (!el) return;
  try { RP = await fetch("/api/backtest/latest").then(r => r.json()); } catch (e) { RP = null; }
  const S = (RP && RP.strategies) || {};
  if (!Object.keys(S).length) {
    el.innerHTML = `<div class="empty">No replay yet. It is written by tools/backtest_report.py and refreshed after each nightly research run.</div>`;
    $("#rpHead").innerHTML = ""; return;
  }
  const rows = Object.entries(S).map(([sid, v]) => ({sid, ...(v.pooled || {})}))
    .sort((a, b) => (b.n ? 1 : 0) - (a.n ? 1 : 0) || (b.total_r ?? -1e9) - (a.total_r ?? -1e9));
  const traded = rows.filter(r => r.n), up = traded.filter(r => r.total_r > 0), proven = traded.filter(r => r.lo > 0);
  const risk = RP.risk_per_trade_money;
  $("#rpHead").innerHTML = `Replayed ${esc(tm(RP.created_at))}. <b class="${up.length ? "good" : "bad"}">${up.length}</b> of ${traded.length} rules
    finished above zero after costs, and <b class="${proven.length ? "good" : "bad"}">${proven.length}</b> did so with the whole
    ${defn("RANGE95", "95% range")} above zero.` + (risk ? ` At ${n(risk, 0)} per trade, 1R = ${n(risk, 0)}.` : "");
  table(el, rows,
    [["sid", "Rule"], ["n", "Trades", v => v ? n(v, 0) : "none"], ["win_rate", "Won", v => pct(v, 0)],
     ["avg_r", "Average per trade [95% range]", (v, r) => rRange(r), (v, r) => rTone(r)],
     ["total_r", "Total", v => v == null ? "–" : signed(v, 1) + "R", v => v > 0 ? "ok" : (v < 0 ? "bad" : "")],
     ["money", "In money", v => v ? signed(v.total, 0) : "–", (v) => v && v.total > 0 ? "ok" : (v && v.total < 0 ? "bad" : "")],
     ["max_drawdown_r", "Worst drawdown", v => v == null ? "–" : n(v, 1) + "R"],
     ["worst_losing_streak", "Worst losing run", v => v == null ? "–" : `${v} in a row`],
     ["worst_month", "Worst month", v => rMonth(v)], ["best_month", "Best month", v => rMonth(v)],
     ["losing_months", "Losing months", (v, r) => r.months ? `${v} of ${r.months}` : "–"],
     ["unseen", "Unseen last 30%", v => v && v.n ? `${v.n} tr · ${rRange(v)}` : "–", v => rTone(v)]],
    "No replay yet");
  el.querySelectorAll("tbody tr").forEach((tr, i) => { if (rows[i].n) { tr.classList.add("click"); tr.tabIndex = 0; } });
  el.onclick = e => { const tr = e.target.closest("tbody tr"); if (tr && tr.classList.contains("click")) replayDetail(rows[[...tr.parentNode.children].indexOf(tr)].sid); };
  el.onkeydown = e => { if (e.key === "Enter") el.onclick(e); };
  if (RP_PICK && S[RP_PICK]) replayDetail(RP_PICK);
}
function replayDetail(sid) {
  const v = ((RP || {}).strategies || {})[sid]; if (!v) return;
  RP_PICK = sid;
  const p = v.pooled || {}, box = $("#rpDetail"); box.hidden = false;
  $("#rpTitle").innerHTML = `<b>${esc(sid)}</b>: running total in R over ${n(p.n, 0)} trades on ${p.symbols_traded || 0} symbols,
    from ${esc(tm(p.first).slice(0, 10))} to ${esc(tm(p.last).slice(0, 10))}. Best trade ${signed(p.best_trade_r, 1)}R, worst ${signed(p.worst_trade_r, 1)}R.`;
  const pts = (p.curve || []).map(([t, r]) => ({t, equity: r}));
  if (!RP_CHART) RP_CHART = VZ.lineChart($("#rpCurve"), {formatX: t => tm(t).slice(0, 10), formatY: v => signed(v, 1) + "R",
    empty: "Too few trades to draw a line"});
  RP_CHART.set({pts, key: "equity", markers: [], flags: []});
  const syms = Object.entries(v.symbols || {}).map(([sym, s]) => ({sym, ...s}))
    .sort((a, b) => (b.total_r ?? -1e9) - (a.total_r ?? -1e9));
  table($("#rpSyms"), syms,
    [["sym", "Symbol"], ["history_from", "History from"], ["n", "Trades", v => v ? n(v, 0) : "none"], ["win_rate", "Won", v => pct(v, 0)],
     ["avg_r", "Average per trade [95% range]", (x, r) => rRange(r), (x, r) => rTone(r)],
     ["total_r", "Total", x => x == null ? "–" : signed(x, 1) + "R", x => x > 0 ? "ok" : (x < 0 ? "bad" : "")],
     ["max_drawdown_r", "Worst drawdown", x => x == null ? "–" : n(x, 1) + "R"],
     ["worst_month", "Worst month", x => rMonth(x)],
     ["unseen", "Unseen last 30%", x => x && x.n ? `${x.n} tr · ${rRange(x)}` : "–", x => rTone(x)],
     ["grade", "Evidence", x => x ? say(x) : "–", x => x === "EDGE" ? "ok" : (x === "NEGATIVE" ? "bad" : "")]],
    "No symbols");
}

// A long forest is shown a page at a time, strongest evidence first, with a button for the rest.
const FOREST_ALL = {};
function forestMore(el, rows, opts, first = 14) {
  if (!el) return;
  const all = FOREST_ALL[el.id], shown = all || rows.length <= first ? rows : rows.slice(0, first);
  const axis = rows.flatMap(r => [r.lo, r.mid, r.hi]).filter(v => v != null && isFinite(v));
  VZ.forest(el, shown, {...opts, min: Math.min(0, ...axis), max: Math.max(0, ...axis)});
  if (rows.length > first) {
    const b = document.createElement("button");
    b.className = "btn small"; b.style.marginTop = "8px";
    b.textContent = all ? "Show fewer" : `Show all ${rows.length}`;
    b.onclick = () => { FOREST_ALL[el.id] = !all; forestMore(el, rows, opts, first); };
    el.appendChild(b);
  }
}

// Every backtest cell the lab graded, as a range on one axis: has any rule shown it makes money on history it
// was not tuned on? The same stats objects the tables below print as text.
const GRADE_CHIP = {EDGE: ["EDGE", "good"], NO_EDGE_SHOWN: ["no edge shown", "wait"], NEGATIVE: ["negative", "bad"], THIN: ["too few trades", "mut"]};
// Graded cells (stats.lo / mean / hi plus a grade) get a full range. A result the lab has not graded yet - the
// per-rule replay carries only its average, count and verdict - gets its dot alone: drawing a range without the
// spread of the trades would invent one.
function renderLabForest(d) {
  const el = $("#labForest"); if (!el) return;
  const latest = d.latest || {}, syms = latest.symbols || {}, rows = [];
  const push = (label, sub, e) => {
    const s = (e && e.stats) || {}; if (s.mean == null) return false;
    const g = gradeOf(e), chip = GRADE_CHIP[g] || [String(g).toLowerCase(), "mut"];
    rows.push({label, sub, lo: s.lo, mid: s.mean, hi: s.hi, chip: {text: chip[0], tone: chip[1]}, dim: g === "THIN", grade: g, graded: true});
    return true;
  };
  const pushPlain = (label, sym, e) => {
    const t = e && e.test; if (!t || !t.n_trades) return;
    const few = t.n_trades < (e.min_trades_applied || 30);
    const chip = few ? ["too few trades", "mut"] : e.verdict === "ROBUST_ON_TEST" ? ["held up on later data", "info"]
      : e.verdict === "NOT_ROBUST_ON_TEST" ? ["did not hold up", "bad"] : [String(e.verdict || "–").toLowerCase().replace(/_/g, " "), "mut"];
    rows.push({label, sub: `${sym} · ${t.n_trades} trades on the later stretch · won ${pct(t.win_rate, 0)}`, mid: t.expectancy_r,
      chip: {text: chip[0], tone: chip[1]}, dim: few, grade: few ? "THIN" : e.verdict, graded: false,
      tip: `${label} on ${sym}: ${signed(t.expectancy_r, 3)}R per trade over ${t.n_trades} trades of history after the split`
        + `${few ? "\nToo few trades to read anything into." : ""}\n${e.note || ""}`});
  };
  for (const [sid, per] of Object.entries(latest.strategies || {})) {
    for (const [sym, e] of Object.entries(per || {})) if (!push(sid, `${sym} · graded`, e)) pushPlain(sid, sym, e);
  }
  for (const [s, e] of Object.entries(syms)) {
    for (const [rule, per] of Object.entries(e.hypotheses || {})) {
      if (per && !per.error) for (const [tf, h] of Object.entries(per)) push(`${rule} (research only)`, `${s} · ${tf} candles`, h);
    }
    if (e.intraday_momentum) push("intraday_momentum (research only)", `${s} · M5`, e.intraday_momentum);
    if (e.trend_daily) push("trend_daily_v1", `${s} · daily candles`, e.trend_daily);
  }
  const order = {EDGE: 0, ROBUST_ON_TEST: 1, NO_EDGE_SHOWN: 2, NOT_ROBUST_ON_TEST: 3, NEGATIVE: 3, THIN: 4};
  rows.sort((a, b) => (order[a.grade] ?? 5) - (order[b.grade] ?? 5) || (b.mid ?? -9) - (a.mid ?? -9));
  const graded = rows.filter(r => r.graded), edge = graded.filter(r => r.grade === "EDGE").length;
  const head = $("#labHead");
  if (head) head.innerHTML = !rows.length ? "" : !graded.length
    ? `The lab has not graded these results yet — ranges and ${defn("GRADES", "evidence grades")} appear after its next research cycle.
       Until then each dot is a rule's average per trade on the later stretch of history, and a grey row has too few trades to read.`
    : edge ? `<b class="good">${edge}</b> of ${graded.length} graded combinations show an EDGE: the whole range above zero, better than random entries, and still standing after the penalty for trying many settings.`
    : `None of the ${graded.length} graded combinations has shown an EDGE yet. That is a finding, not a fault: no rule has proved itself on unseen data so far.`;
  forestMore(el, rows, {unit: "R", empty: "No backtest results yet — they appear after the next research cycle"});
}
// The lab's evidence grade: EDGE only when the 95% range is above zero, the deflated Sharpe (which counts every
// cell looked at) clears 0.95, and the same entries taken with random sides did worse. See research/lab/stats.py.
const gradeOf = e => (e && (e.evidence_grade || (e.stats || {}).grade)) || "–";
const gradeCls = v => v === "EDGE" ? "ok" : (v === "NEGATIVE" ? "bad" : "");
const evText = e => { const s = (e && e.stats) || {}; if (s.lo == null) return "–";
  return `${n(s.mean, 3)}R [${n(s.lo, 2)}, ${n(s.hi, 2)}]` + (s.dsr != null ? ` · DSR ${n(s.dsr, 2)}` : "")
    + (s.random_p != null ? ` · vs random p ${n(s.random_p, 3)}` : "") + (s.pbo != null ? ` · PBO ${n(s.pbo, 2)}` : ""); };
function renderTimeframes(d) {
  const syms = ((d.latest || {}).symbols) || {};
  const r2 = (m, k) => m && m[k] != null ? n(m[k], 2) : "–";
  const rows = [];
  for (const [s, e] of Object.entries(syms)) {
    const c = e.costs || {};
    for (const [tf, t] of Object.entries(e.timeframes || {})) {
      const o = t.optuna || {}, wf = o.walk_forward || {}, pp = o.params_on_all_data || {};
      rows.push({symbol: s, tf, verdict: t.verdict, signals: t.signals, grade: gradeOf(tf === "M1" ? e : t),
        evidence: evText(tf === "M1" ? e : t),
        test: t.test ? `${t.test.n_trades ?? "–"} tr · ${r2(t.test, "expectancy_r")}R` : "–",
        wf: o.error ? `error: ${o.error}` : (wf.n_trades != null ? `${wf.n_trades} tr · ${r2(wf, "expectancy_r")}R · PF ${r2(wf, "profit_factor")}` : "–"),
        wfv: o.verdict || "", params: pp.sl_atr != null ? `SL ${pp.sl_atr}×ATR · RR ${pp.rr} · vol ≥ ${pp.min_rv}` : "–",
        cost: c.tick_spread_median != null ? `tick ${n(c.tick_spread_median, 5)} vs bar ${n(c.bar_spread_median, 5)} (×${n(c.tick_vs_bar_spread, 2)})` : (c.bar_spread_median != null ? `bar ${n(c.bar_spread_median, 5)} (no ticks)` : "–"),
        comm: c.commission_price_round_trip ? n(c.commission_price_round_trip, 5) : "0"});
    }
  }
  table($("#tftable"), rows, [["symbol", "Symbol"], ["tf", "Trigger"], ["verdict", "Grid verdict", null, v => v === "ROBUST_ON_TEST" ? "ok" : (v === "NOT_ROBUST_ON_TEST" ? "bad" : "")],
    ["grade", "Evidence", null, gradeCls], ["evidence", "Unseen R per trade [95% range]"],
    ["signals", "Signals"], ["test", "Test (unseen)"], ["wf", "Walk-forward (out of sample)"], ["wfv", "WF verdict", null, v => v === "WALK_FORWARD_POSITIVE" ? "ok" : (v ? "bad" : "")],
    ["params", "Optuna params (all data)"], ["cost", "Median spread"], ["comm", "Commission (price)"]],
    "No timeframe results yet - they appear after the next research cycle");
  const frows = [];
  for (const [s, e] of Object.entries(syms)) {
    for (const [sid, f] of Object.entries(e.strategy_funnel || {})) {
      if (f.error) { frows.push({symbol: s, strategy: sid, blocker: "ERROR", note: f.error}); continue; }
      const cond = f.conditions || {};
      const clauses = Object.entries(cond).filter(([k]) => k !== "all_clauses")
        .map(([k, v]) => `${k} ${pct(v.rate, 0)}`).join(" · ");
      const all = (cond.all_clauses || {}).rate;
      frows.push({symbol: s, strategy: sid, setups: f.setups, rate: f.setups_per_week,
        regimes: f.regime_allowed_share != null ? `${(f.allowed_regimes || []).join("/")} = ${pct(f.regime_allowed_share, 0)} of bars` : "–",
        mix: Object.entries(f.regime_mix || {}).map(([k, v]) => `${k} ${pct(v, 0)}`).join(" · "),
        blocker: f.blocker_pct != null ? `${f.biggest_blocker} (${f.blocker_pct})` : f.biggest_blocker,
        funnel: Object.entries(f.reasons || {}).map(([k, v]) => `${k} ${v}`).join(" · "),
        clauses: clauses + (all != null ? ` → all ${pct(all, 0)}` : "")});
    }
  }
  table($("#ftable"), frows, [["symbol", "Symbol"], ["strategy", "Strategy"], ["setups", "Setups"],
    ["rate", "Setups per week", null, v => v === 0 ? "bad" : ""],
    ["regimes", "Router lets it run in", null, (v, r) => /= 0%/.test(v || "") ? "bad" : ""],
    ["mix", "Regime mix (H1)"], ["blocker", "Blocked most by"],
    ["clauses", "H1 regime clauses (pass rate on their own)"], ["funnel", "Full funnel"]],
    "No funnel yet - it appears after the next research cycle");
  const hrows = [];
  for (const [s, e] of Object.entries(syms)) {
    for (const [rule, per] of Object.entries(e.hypotheses || {})) {
      if (per && per.error) { hrows.push({symbol: s, rule, tf: "–", verdict: "ERROR", note: per.error}); continue; }
      for (const [tf, h] of Object.entries(per || {})) {
        const o = h.optuna || {}, wf = o.walk_forward || {};
        hrows.push({symbol: s, rule, tf, verdict: h.verdict, signals: h.signals, grade: gradeOf(h), evidence: evText(h),
          best: h.best ? `SL ${h.best.sl_atr}×ATR · RR ${h.best.rr}` : "–",
          test: h.test ? `${h.test.n_trades ?? "–"} tr · ${r2(h.test, "expectancy_r")}R · PF ${r2(h.test, "profit_factor")}` : "–",
          wf: wf.n_trades != null ? `${wf.n_trades} tr · ${r2(wf, "expectancy_r")}R · PF ${r2(wf, "profit_factor")}` : (o.error ? "error" : "not run"),
          wfv: o.verdict || "", note: h.note || ""});
      }
    }
  }
  for (const [s, e] of Object.entries(syms)) {      // the two rules from the published evidence, scored by their own paths
    const im = e.intraday_momentum;
    if (im) hrows.push({symbol: s, rule: "intraday_momentum", tf: "M5", verdict: im.verdict, signals: im.signals,
      grade: gradeOf(im), evidence: evText(im), best: "2×ATR stop · exit at NY close",
      test: im.test ? `${im.test.n_trades ?? "–"} tr · ${r2(im.test, "expectancy_r")}R` : "–", wf: "no search", wfv: "", note: ""});
    const td = e.trend_daily;
    if (td) hrows.push({symbol: s, rule: "trend_daily", tf: "D1", verdict: td.verdict, signals: (td.metrics || {}).n_trades,
      grade: (td.stats || {}).grade || "–", evidence: evText(td), best: "Clenow 50-day breakout · 3×ATR(100) trail",
      test: `${(td.metrics || {}).n_trades ?? 0} tr over ${td.days ?? "–"} days · swap ${n(td.avg_swap_r, 2)}R/trade`,
      wf: td.halves ? `halves ${td.halves.map(x => n(x, 2)).join(" / ")}` : "–", wfv: "",
      note: td.exits ? `with the profit lock: ${n(td.exits.with_profit_lock, 3)}R vs ${n(td.exits.trail_only, 3)}R` : ""});
  }
  const rank = r => r.grade === "EDGE" ? 0 : (r.wfv === "WALK_FORWARD_POSITIVE" ? 1 : 2);
  hrows.sort((a, b) => rank(a) - rank(b));
  table($("#htable"), hrows, [["symbol", "Symbol"], ["rule", "Candidate rule"], ["tf", "Timeframe"],
    ["verdict", "Grid verdict", null, v => v === "ROBUST_ON_TEST" ? "ok" : (v === "NOT_ROBUST_ON_TEST" ? "bad" : "")],
    ["grade", "Evidence", null, gradeCls], ["evidence", "Unseen R per trade [95% range]"],
    ["signals", "Signals"], ["best", "Best on train"], ["test", "Test (unseen)"], ["wf", "Walk-forward"],
    ["wfv", "WF verdict", null, v => v === "WALK_FORWARD_POSITIVE" ? "ok" : (v ? "bad" : "")], ["note", "Note"]],
    "No candidate-rule results yet - they appear after the next research cycle");
  const ml = ((d.latest || {}).ml_filter) || {};
  const mrows = Object.entries(ml).filter(([k, v]) => v && typeof v === "object").map(([sid, m]) => {
    const t = m.test || {};
    return {strategy: sid, status: m.status, samples: m.samples, note: m.note || "",
      test: t.trades != null ? `${t.accepted}/${t.trades} kept · kept ${n(t.accepted_r, 3)}R vs all ${n(t.baseline_r, 3)}R (uplift ${n(t.uplift_r, 3)}R, AUC ${t.auc ?? "–"})` : "–",
      top: (m.top_features || []).slice(0, 5).map(x => x[0]).join(", "), at: m.trained_at};
  });
  table($("#mltable"), mrows, [["strategy", "Strategy"], ["status", "Status", null, v => v === "VALIDATED" ? "ok" : (v === "NOT_VALIDATED" ? "bad" : "")], ["samples", "Outcomes"],
    ["test", "Unseen 30%"], ["top", "Most used measurements"], ["at", "Trained", v => v ? tm(v) : "–"], ["note", "Note"]],
    ml.status ? `Learned filter: ${ml.status}${ml.note ? " - " + ml.note : ""}` : "Not trained yet - it trains in the research cycle once enough outcomes are recorded");
}
// Expectancy per trading session. The verdict sentence comes from the server so the words can never drift
// away from the numbers under them, and a bucket below the trade threshold is shown greyed rather than hidden
// - an untrustworthy number you can see is safer than one you cannot.
let SESS_DISABLED = [];
async function loadSessions() {
  const box = $("#sesstable"); if (!box) return;
  const hours = $("#sesshours") ? $("#sesshours").value : 720;
  const sym = $("#sesssym") ? $("#sesssym").value : "";
  // A session table is a weighted average of whatever was trading. If one rule supplied most of the
  // trades and has since been switched off, the table describes that rule's habits rather than the
  // hours - so leaving it out is what actually answers "which hours are better".
  const box2 = $("#sessexcl");
  const wrap = $("#sessexclwrap");
  const excl = (wrap && !wrap.hidden && box2 && box2.checked) ? (SESS_DISABLED || []) : [];
  const q = `hours=${hours}${sym ? "&symbol=" + encodeURIComponent(sym) : ""}`
          + (excl.length ? "&exclude=" + encodeURIComponent(excl.join(",")) : "");
  let d;
  try { d = await (await fetch(`/api/analysis/sessions?${q}`)).json(); }
  catch (_) { return; }

  // Remember which rules are switched off, and only offer the checkbox once we know there are any.
  if (Array.isArray(d.disabled_strategies)) {
    SESS_DISABLED = d.disabled_strategies;
    if (wrap) {
      const any = SESS_DISABLED.length > 0;
      wrap.hidden = !any;
      if (any) $("#sessexcllbl").textContent = `leave out ${SESS_DISABLED.join(", ")} (switched off)`;
    }
  }
  const note = $("#sessexclnote");
  if (note) {
    const on = (d.excluded || []).length > 0;
    note.hidden = !(SESS_DISABLED || []).length;
    note.innerHTML = on
      ? `Showing the hours <b>without</b> ${esc((d.excluded || []).join(", "))}. This is the picture that
         applies to what is trading now.`
      : `<b class="wait">Including rules that are switched off.</b> Those trades still sit in the record,
         so these numbers describe what used to trade, not what trades now. Tick the box above to see the
         hours as they apply today.`;
  }

  // fill the symbol picker once, from whatever has actually traded
  const pick = $("#sesssym");
  if (pick && pick.options.length <= 1) {
    const syms = [...new Set((d.sessions || []).flatMap(x => x.symbols || []))].sort();
    syms.forEach(x => pick.add(new Option(x, x)));
  }

  $("#sessverdict").innerHTML = esc(d.verdict || "");
  const pretty = v => String(v || "").replace(/_/g, " ").toLowerCase();
  VZ.barChart($("#sessbars"), (d.sessions || []).map(x => ({label: pretty(x.session), value: x.mean_r, dim: !x.trustworthy,
      sub: `${x.n} trade${x.n === 1 ? "" : "s"}${x.trustworthy ? "" : " · not enough yet"}`,
      tip: `${pretty(x.session)}: ${signed(x.mean_r, 3)}R per trade over ${x.n} trades\n${signed(x.total_r, 2)}R in total · won ${pct(x.win_rate, 0)}`
        + `${x.trustworthy ? "" : "\nToo few trades for this number to mean anything yet."}`})),
    {fmt: v => signed(v, 3) + "R", empty: "No finished trades in this window."});
  table(box, (d.sessions || []).map(x => ({...x, judged: x.trustworthy})),
    [["session", "Session", pretty],
     ["n", "Trades"],
     ["mean_r", "Per trade", v => signed(v, 3) + "R", cls],
     ["total_r", "Total", v => signed(v, 2) + "R", cls],
     ["money", "Money", v => signed(v, 2), cls],
     ["win_rate", "Won", v => pct(v, 0)],
     ["avg_win_r", "Average win", v => v == null ? "–" : signed(v, 2) + "R"],
     ["avg_loss_r", "Average loss", v => v == null ? "–" : signed(v, 2) + "R"],
     ["full_stop_rate", "Hit the first stop", v => pct(v, 0), v => v > 0.4 ? "wait" : ""],
     ["avg_slippage_points", "Slippage", v => v == null ? "–" : n(v, 1) + " pts"],
     ["judged", "Enough data?", v => v ? "yes" : "not yet", v => v ? "good" : "mut"]],
    "No finished trades in this window.");

  table($("#hourtable"), d.hours || [],
    [["hour_utc", "Hour (UTC)", v => String(v).padStart(2, "0") + ":00"],
     ["n", "Trades"], ["mean_r", "Per trade", v => signed(v, 3) + "R", cls],
     ["total_r", "Total", v => signed(v, 2) + "R", cls],
     ["win_rate", "Won", v => pct(v, 0)],
     ["full_stop_rate", "Hit the first stop", v => pct(v, 0)],
     ["trustworthy", "Enough data?", v => v ? "yes" : "not yet", v => v ? "good" : "mut"]],
    "Nothing to show yet.");
}

async function loadScoreboard() {
  const d = await fetch("/api/analysis/scoreboard?hours=720").then(r => r.json()).catch(() => ({}));
  const f = (m, k, nd) => m && m[k] != null ? n(m[k], nd) : "–";
  // one card per rule: what it actually did with money, and what its refused or watch-only signals did on paper
  const cards = $("#ruleCards");
  if (cards) {
    const side = (m, what) => !m || !m.n ? `<div class="rc-none mut">${what}: none yet</div>`
      : `<div class="rc-s"><span>${what}</span><b class="${cls(m.mean_r)}">${signed(m.mean_r, 3)}R</b><span class="mut">per trade · ${m.n} · won ${pct(m.win_rate, 0)}</span>
          <i class="rc-bar"><i class="${(m.mean_r || 0) >= 0 ? "good" : "bad"}" style="width:${Math.min(100, Math.abs(m.mean_r || 0) * 100)}%"></i></i></div>`;
    cards.innerHTML = (d.rows || []).length ? (d.rows || []).map(r => {
      // only a rule with enough results gets the seafoam chip; "too few" and "no signals yet" stay grey
      const enough = r.evidence && !/few|no /i.test(r.evidence);
      return `<div class="rcard"><div class="rc-h"><b>${esc(r.strategy)}</b>
          <span class="chip ${enough ? "good" : "mut"}">${r.evidence === "too few" ? "too few to judge" : esc(r.evidence || "–")}</span></div>
        <div class="rc-st mut">cleared for ${r.status === "DEMO" ? "the practice account" : esc(String(r.status || "").toLowerCase())}</div>
        ${side(r.live, "Real trades")}${side(r.shadow, `${defn("PAPER", "On paper")}`)}</div>`;
    }).join("") : `<div class="empty">Nothing to score yet. Rules appear here once they have produced signals.</div>`;
  }
  table($("#stable"), (d.rows || []).map(r => ({
      strategy: r.strategy, status: r.status, evidence: r.evidence,
      live: r.live.n ? `${r.live.n} tr · ${f(r.live, "mean_r", 3)}R · ${pct(r.live.win_rate, 0)} won · ${f(r.live, "total_r", 1)}R total` : "–",
      shadow: r.shadow.n ? `${r.shadow.n} of ${r.shadow_signals} · ${f(r.shadow, "mean_r", 3)}R · ${pct(r.shadow.win_rate, 0)} won` : (r.shadow_signals ? `${r.shadow_signals} signals, none resolved yet` : "–")})),
    [["strategy", "Rule"], ["status", "Cleared for", v => v === "DEMO" ? "practice account" : String(v || "").toLowerCase()],
     ["evidence", "Enough data?", v => v === "too few" ? "not yet" : v, v => v === "too few" ? "mut" : "good"],
     ["live", "Trades it actually took"], ["shadow", "Signals followed on paper"]],
    "Nothing to score yet. Rules appear here once they have produced signals.");
}
async function loadSelector() {
  if (!$("#seltable")) return;
  const d = await fetch("/api/selector").then(r => r.json()).catch(() => ({}));
  const rows = [], tally = {};
  for (const [sym, s] of Object.entries(d.symbols || {})) {
    for (const a of Object.values(s.allocations || {})) {
      tally[a.state] = (tally[a.state] || 0) + 1;
      const c = a.counts || {};
      rows.push({symbol: sym, strategy: a.strategy_id, state: a.state, size: a.multiplier,
        est: a.posterior_r, range: `${signed(a.lower_r, 3)} to ${signed(a.upper_r, 3)}R`,
        evidence: `${n(a.n_eff, 0)} (live ${c.live || 0} · paper ${c.shadow || 0} · backtest ${(c.lab_test || 0) + (c.lab_train || 0)})`,
        veto: (a.vetoed_regimes || []).join(", "), reason: a.reason, at: s.computed_at,
        lo: a.lower_r, hi: a.upper_r, n_eff: a.n_eff});
    }
  }
  const order = {ACTIVE: 0, PROBATION: 1, UNPROVEN: 2, BENCHED: 3};
  rows.sort((x, y) => (order[x.state] ?? 9) - (order[y.state] ?? 9) || (y.est || 0) - (x.est || 0));
  const STATE_CHIP = {ACTIVE: "good", PROBATION: "wait", UNPROVEN: "mut", BENCHED: "bad"};
  forestMore($("#selForest"), rows.map(r => ({label: r.strategy, sub: `${r.symbol} · ${n(r.n_eff, 0)} evidence trades · trades at ${pct(r.size, 0)} size`,
      lo: r.lo, mid: r.est, hi: r.hi, chip: {text: String(r.state || "").toLowerCase(), tone: STATE_CHIP[r.state] || "mut"},
      tip: `${r.strategy} on ${r.symbol} — ${String(r.state || "").toLowerCase()}, trading at ${pct(r.size, 0)} of normal size\n`
        + `best guess ${signed(r.est, 3)}R per trade after costs, likely between ${signed(r.lo, 3)} and ${signed(r.hi, 3)}R\n${r.reason || ""}`})),
    {unit: "R", empty: "Not scored yet - the selector runs within a minute of start-up and then every hour"});
  const mode = d.mode || "off";
  $("#selsum").innerHTML = `Mode: <b>${esc(mode)}</b>${mode === "auto" ? " — sizes below are applied to new trades."
      : mode === "shadow" ? " — computed and recorded only; trading is unchanged." : " — the selector is switched off."} `
    + (rows.length ? Object.keys(order).filter(k => tally[k]).map(k => `${esc(k.toLowerCase())} ${tally[k]}`).join(", ") + "." : "");
  table($("#seltable"), rows,
    [["symbol", "Symbol"], ["strategy", "Rule"],
     ["state", "State", null, v => v === "ACTIVE" ? "ok" : (v === "BENCHED" ? "bad" : (v === "PROBATION" ? "wait" : "mut"))],
     ["size", "Size vs normal", v => v == null ? "–" : pct(v, 0)],
     ["est", "Estimate per trade", v => v == null ? "–" : signed(v, 3) + "R", v => v > 0 ? "good" : (v < 0 ? "bad" : "")],
     ["range", "Likely range"], ["evidence", "Evidence (trades)"],
     ["veto", "Benched in regime", v => v || "–", v => v ? "bad" : ""], ["reason", "Why"], ["at", "Scored", v => v ? tm(v) : "–"]],
    "Not scored yet - the selector runs within a minute of start-up and then every hour");
  table($("#selhist"), d.history || [],
    [["created_at", "When", tm], ["symbol", "Symbol"], ["strategy_id", "Rule"],
     ["previous_state", "From", v => v || "new"], ["state", "To"], ["multiplier", "Size", v => pct(v, 0)],
     ["applied", "Applied", v => v ? "yes" : "shadow"], ["reason", "Why"]],
    "No changes recorded yet.");
}
// ------------------------------------------------------------------ the book as a whole
// Deliberately separate from the per-rule scoreboard. A rule's own win rate cannot show whether it
// loses at the same time as everything else, and that is the question that decides portfolio risk.
async function loadPortfolio() {
  const head = $("#pfhead"); if (!head) return;
  const hours = ($("#pfhours") || {}).value || 720;
  const bucket = ($("#pfbucket") || {}).value || "D";
  let d;
  try { d = await (await fetch(`/api/analysis/portfolio?hours=${hours}&bucket=${bucket}`)).json(); }
  catch (e) { head.innerHTML = `<div class="empty">Could not load: ${esc(e.message)}</div>`; return; }
  const range = $("#pfrange");
  if (!d || !d.n_trades) {
    head.innerHTML = `<div class="empty">${esc((d && d.note) || "No closed trades in this period")}</div>`;
    if (range) range.innerHTML = "";
    $("#pfco").innerHTML = ""; table($("#pftable"), [], []); return;
  }
  const dd = d.drawdown || {}, ci = d.ci95;
  // An interval straddling zero is the single most important thing on this page: it means the book
  // has not been shown to make money, however good the headline looks.
  const unproven = ci && ci[0] < 0 && ci[1] > 0;
  if (range) {
    VZ.forest(range, [{label: "The whole book, per trade", sub: `${d.n_trades} finished trade${d.n_trades === 1 ? "" : "s"}`,
      lo: ci ? ci[0] : null, mid: d.mean_r, hi: ci ? ci[1] : null, dim: d.n_trades < 30}],
      {big: true, min: Math.min(-1, ci ? ci[0] : d.mean_r), max: Math.max(1, ci ? ci[1] : d.mean_r)});
    range.insertAdjacentHTML("beforeend", `<div class="note">${d.n_trades < 30
      ? `Only <b>${d.n_trades}</b> trade${d.n_trades === 1 ? "" : "s"} so far — about thirty are needed before the ${defn("RANGE95", "range")} means much. Judge nothing yet.`
      : !ci ? "" : ci[0] > 0 ? `<b class="good">The whole range is above zero</b> — the book has been shown to make money after costs.`
      : ci[1] < 0 ? `<b class="bad">The whole range is below zero</b> — the book has been shown to lose money after costs.`
      : `<b class="wait">The range crosses zero</b> — not shown to make money yet, however good the headline looks.`}</div>`);
  }
  head.innerHTML = `<div class="kv">
    <span>Total net R</span><span class="${d.total_net_r > 0 ? "good" : "bad"}"><b>${n(d.total_net_r, 2)}R</b> over ${d.n_trades} trades</span>
    <span>Per trade</span><span>${n(d.mean_r, 4)}R${ci ? ` &nbsp;<span class="mut">95% [${n(ci[0], 3)}, ${n(ci[1], 3)}]</span>` : ""}</span>
    <span>Win rate</span><span>${n(d.win_rate * 100, 1)}%</span>
    <span>Worst drawdown</span><span class="${dd.max_drawdown_r > 0 ? "wait" : ""}">${n(dd.max_drawdown_r, 2)}R</span>
    <span>Longest losing stretch</span><span>${dd.longest_drawdown_trades ?? "–"} trades${dd.still_open ? " <b class='wait'>(still open)</b>" : ""}</span>
  </div>${unproven ? `<div class="note bad" style="margin-top:10px">The range around the average crosses zero,
    so this book has not been shown to have an edge. The headline figure is consistent with having none.</div>` : ""}`;

  const cl = d.correlated_losses || {};
  const together = cl.together_rate;
  $("#pfco").innerHTML = `<b>Losing ${cl.bucket}s:</b> ${cl.losing_buckets} of ${cl.buckets}.
    ${together == null ? "" : `<b>${cl.multi_strategy_loss_buckets}</b> of those (${n(together * 100, 0)}%) hit two or more rules at once.`}
    ${together != null && together > 0.5 ? `<br><b class="bad">Most losing ${cl.bucket}s hit several rules together.</b>
      They are less independent than their number suggests, and any risk budget that treats them as separate
      is sized on an assumption that does not hold.` : ""}
    ${(cl.pairs || []).length ? `<br><span class="mut">Most often together:</span> ${
      cl.pairs.slice(0, 3).map(p => `${esc(p.pair[0])} + ${esc(p.pair[1])} (${p.lost_together}/${p.both_traded})`).join(", ")}` : ""}`;

  const rows = Object.entries(d.marginal || {}).map(([id, m]) => ({strategy: id, ...m}))
    .sort((a, b) => (a.marginal_r || 0) - (b.marginal_r || 0));
  table($("#pftable"), rows, [
    ["strategy", "Rule"], ["n_trades", "Trades"],
    ["own_total_r", "Its own R", v => n(v, 2) + "R", v => v > 0 ? "ok" : "bad"],
    ["portfolio_without_r", "Book without it", v => n(v, 2) + "R", v => v > 0 ? "ok" : "bad"],
    ["portfolio_dd_without", "Drawdown without it", v => n(v, 2) + "R"],
    ["verdict", "Effect on the book", v => v,
      v => /cushions/.test(v || "") ? "ok" : (/costs R/.test(v || "") ? "bad" : "")],
  ], "No closed trades in this period");
}

function initAnalysis() {
  loadSessions(); setInterval(loadSessions, 60000);
  if ($("#sesshours")) $("#sesshours").onchange = loadSessions;
  if ($("#sesssym")) $("#sesssym").onchange = loadSessions;
  if ($("#sessexcl")) $("#sessexcl").onchange = loadSessions;
  if ($("#pfhours")) $("#pfhours").onchange = loadPortfolio;
  if ($("#pfbucket")) $("#pfbucket").onchange = loadPortfolio;
  loadPortfolio(); setInterval(loadPortfolio, 60000);
  loadScoreboard(); setInterval(loadScoreboard, 60000); loadSelector(); setInterval(loadSelector, 60000); loadLab(); setInterval(loadLab, 60000); loadReplay(); setInterval(loadReplay, 600000); $("#areload").onclick = loadAnalysis; loadAnalysis(); setInterval(loadAnalysis, 30000); }

/* ------------------------------------------------------------------ install as an app
 * Registering this makes the browser offer "Install app" / "Add to Home screen",
 * which is what turns the dashboard into a home-screen icon that opens without an
 * address bar. The worker caches no dashboard data — see sw.js for why.
 *
 * Only runs over HTTPS or on localhost; browsers refuse service workers anywhere
 * else. Reaching the dashboard at a bare http://100.x.x.x address will skip this
 * silently and the page keeps working exactly as before.
 */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    // update() asks for a newer sw.js straight away rather than at the browser's convenience, so a fix to the
    // worker (such as always re-checking pages with the server) reaches every browser on its next page load
    navigator.serviceWorker.register("/sw.js").then(function (reg) { return reg.update(); }).catch(function (err) {
      console.debug("service worker not registered:", err && err.message);
    });
  });
}

/* ------------------------------------------------------------------ start
 * A page the browser saved before 20 Oct 2026 still loads this script, but not the files the header and the charts
 * now need (viz.js, help.js, learn.js, the new glossary.js and the new stylesheet). On that page the header threw
 * "HELP is not defined" and the Live page stayed empty. So a page first checks it is current. If it is not, it
 * reloads once, which makes the browser ask the server for the page again. If something is still missing after
 * that, it fetches the missing files itself and starts anyway. A flag in sessionStorage stops a reload loop.
 */
const ASSET_V = (() => { try { return new URL(document.currentScript.src).searchParams.get("v"); } catch (_) { return null; } })();
(function start() {
  const KEY = "qa_stale_reload";
  const current = typeof VZ !== "undefined" && typeof HELP !== "undefined" && typeof LEARN !== "undefined"
    && typeof funnelModel !== "undefined" && typeof TERMS !== "undefined";
  const styled = !!document.querySelector('link[rel="stylesheet"][href*="style.css?v="]');
  if (current && styled) {
    try { sessionStorage.removeItem(KEY); } catch (_) {}
    boot(); return;
  }
  let reload = false;
  try { if (!sessionStorage.getItem(KEY)) { sessionStorage.setItem(KEY, String(Date.now())); reload = true; } } catch (_) {}
  if (reload) { location.reload(); return; }
  // Still an old page. Fetch what is missing and start. (An old glossary.js cannot be swapped in place - its names
  // are already taken - so the few things that need the new one check for it and say "reload the page" instead.)
  const v = encodeURIComponent(ASSET_V || String(Date.now()));
  if (!styled) {
    const link = document.createElement("link");
    link.rel = "stylesheet"; link.href = `/static/style.css?v=${v}`; document.head.appendChild(link);
  }
  const load = name => new Promise(done => {
    const s = document.createElement("script");
    s.src = `/static/${name}?v=${v}`; s.onload = done; s.onerror = done; document.body.appendChild(s);
  });
  const missing = [];
  if (typeof VZ === "undefined") missing.push("viz.js");
  if (typeof HELP === "undefined") missing.push("help.js");
  if (typeof LEARN === "undefined") missing.push("learn.js");
  missing.reduce((p, name) => p.then(() => load(name)), Promise.resolve()).then(() => {
    if (typeof VZ === "undefined" || typeof HELP === "undefined") {
      document.body.insertAdjacentHTML("afterbegin", `<div class="banner show halt" style="margin:12px">This page is an
        old saved copy and could not update itself. Press Ctrl+F5 (or Cmd+Shift+R) to load the current version.</div>`);
      return;
    }
    boot();
  });
})();
