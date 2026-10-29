/* Quantara - the five-minute walkthrough.
 *
 * Seven short animated scenes, in the spirit of the "explainer" pieces collected in
 * github.com/yihui-dev/awesome-opus5-5-videos: one idea per scene, a caption that changes as the picture does,
 * and one thing to try with your own hands. Unlike a video it talks about THIS bot - the markets, balance,
 * funnel counts, spread, risk tiers and results are read live from the same endpoints the dashboard uses.
 *
 * It explains; it never acts. Nothing here can place, close or stop anything - the stop button in scene 5 is a
 * drawing, and the only request this file makes is a read.
 *
 * Opened three ways: the "Start here" button in the header, #learn in the address, and the /learn page, which
 * mounts the same scenes inline.
 *
 * A second series, "Going deeper" (DEEPER below; #deeper in the address), picks up where the five minutes stop:
 * how big each trade is, how a winner is protected, the market's mood, the news window and the open-risk limits.
 * The player shows one series at a time, with a switch between them at the top.
 */
const LEARN = (() => {
  const store = (typeof HELP !== "undefined") ? HELP.store : {get: () => null, set: () => {}};
  const REDUCED = typeof VZ !== "undefined" && VZ.REDUCED;
  const fmt = VZ.fmt, sgn = VZ.sgn, escH = VZ.escH, clamp = VZ.clamp;
  const pc = (v, d = 0) => v == null || isNaN(+v) ? "–" : fmt(100 * v, d) + "%";
  const lerp = (a, b, p) => a + (b - a) * clamp(p, 0, 1);
  const smooth = p => { p = clamp(p, 0, 1); return p * p * (3 - 2 * p); };
  // a repeatable "random" so a scene looks the same every time it is played
  const rng = seed => () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  const svgPoint = (svg, e) => { const p = svg.createSVGPoint(); p.x = e.clientX; p.y = e.clientY; return p.matrixTransform(svg.getScreenCTM().inverse()); };
  const findKey = (o, k) => { if (!o || typeof o !== "object") return undefined; if (k in o) return o[k];
    for (const v of Object.values(o)) { const r = findKey(v, k); if (r !== undefined) return r; } return undefined; };

  // ================================================================ the scenes
  const SCENES = [
    // ---------------------------------------------------------------- 1
    {short: "Meet the bot", title: "Meet Quantara", secs: 34,
     beats: [
       [0, () => `Quantara is a robot trader. It runs on this computer and places orders through MetaTrader 5, the trading app your broker provides.`],
       [8, L => `It watches <b>${L.symbols.length || "several"}</b> markets — currencies, gold, oil, share prices — and checks each one every time a price ${defn("CANDLE", "candle")} finishes.`],
       [17, () => `It only acts when one of its written ${defn("RULE", "rules")} matches. No hunches, no headlines, no mood. Tap a market to see what it is doing right now.`],
       [25, L => L.env === "REAL"
         ? `<b class="bad">This is a REAL-MONEY account</b>, worth ${fmt(L.balance)} ${escH(L.ccy)}.`
         : L.env === "DEMO" ? `Right now it trades a ${defn("PRACTICE", "practice account")} worth <b>${fmt(L.balance)} ${escH(L.ccy)}</b>: real prices, pretend money. Nothing on this screen can lose real money.`
         : `Right now it is paper trading: live prices, and no orders are sent at all.`],
     ],
     mount(stage, L, api) {
       const syms = L.symbols.length ? L.symbols : ["EURUSD", "GBPUSD", "XAUUSD", "USOIL", "NVDA", "BTCUSD"];
       const tone = L.env === "REAL" ? "bad" : "info";
       const badge = L.env === "REAL" ? `REAL MONEY · ${fmt(L.balance)}` : L.env === "DEMO" ? `PRACTICE MONEY · ${fmt(L.balance)} ${L.ccy}` : "PAPER ONLY · no orders";
       stage.innerHTML = `<svg class="lr-svg" viewBox="0 0 800 340" role="img" aria-label="The bot in the middle, the markets it watches circling it">
         <ellipse cx="400" cy="168" rx="318" ry="112" class="ln faint" stroke-dasharray="3 7"/>
         <g class="beams"></g>
         <circle cx="400" cy="168" r="56" class="core-glow"/>
         <clipPath id="lrclip"><circle cx="400" cy="168" r="42"/></clipPath>
         <image href="/static/quantara-logo.png" x="358" y="126" width="84" height="84" clip-path="url(#lrclip)"/>
         <text x="400" y="246" class="tx c">the bot</text>
         <g class="syms">${syms.map((s, i) => `<g class="sym" data-i="${i}" tabindex="0" role="button" aria-label="${escH(s)}">
           <rect x="-48" y="-14" width="96" height="28" rx="14"/><text y="5" class="tx c">${escH(s)}</text></g>`).join("")}</g>
         <g class="badge ${tone}"><rect x="14" y="12" width="${badge.length * 8 + 26}" height="34" rx="9"/><text x="27" y="34" class="tx">${escH(badge)}</text></g>
         <text class="syminfo tx c" x="400" y="330"></text></svg>`;
       const svg = stage.querySelector("svg"), els = [...svg.querySelectorAll(".sym")], beams = svg.querySelector(".beams");
       const info = svg.querySelector(".syminfo"), badgeEl = svg.querySelector(".badge");
       let lastPing = -1, pos = [];
       const describe = i => {
         const s = syms[i], st = (L.state.symbol_trading || {})[s], mk = ((L.state.symbol_market_status || {})[s] || {}).status;
         const open = (L.state.positions || []).filter(p => p.symbol === s).length;
         return `${s}: ${st && !st.enabled ? "you stopped new trades here" : "watching"}${mk ? " · market " + String(mk).toLowerCase().replace(/_/g, " ") : ""} · ${open ? open + " trade" + (open > 1 ? "s" : "") + " open" : "no trade open"}`;
       };
       svg.addEventListener("click", e => { const g = e.target.closest(".sym"); if (!g) return; api.pause(); info.textContent = describe(+g.dataset.i); });
       svg.addEventListener("keydown", e => { const g = e.target.closest(".sym"); if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); api.pause(); info.textContent = describe(+g.dataset.i); } });
       return {tick(t) {
         const base = REDUCED ? 0.5 : t * 0.11, n = els.length;
         pos = els.map((g, i) => {
           const a = base + i * 2 * Math.PI / n, x = 400 + 318 * Math.cos(a), y = 168 + 112 * Math.sin(a);
           const depth = (Math.sin(a) + 1) / 2, appear = REDUCED ? 1 : clamp((t - 1 - i * 0.18) / 0.8, 0, 1);
           g.setAttribute("transform", `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${(0.82 + depth * 0.28).toFixed(3)})`);
           g.style.opacity = (appear * (0.5 + depth * 0.5)).toFixed(3);
           return [x, y];
         });
         badgeEl.classList.toggle("on", t >= 25 || REDUCED);
         const k = Math.floor(t / 0.5);
         if (!REDUCED && t > 4 && k !== lastPing) {
           lastPing = k; const i = k % n, [x, y] = pos[i];
           els[i].classList.remove("ping"); void els[i].getBoundingClientRect(); els[i].classList.add("ping");
           beams.innerHTML = `<line x1="400" y1="168" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" class="beam"/>`;
         }
       }};
     }},

    // ---------------------------------------------------------------- 2
    {short: "One trade", title: "One trade, start to finish", secs: 46,
     beats: [
       [0, () => `Every trade starts with three prices, all decided BEFORE it opens: where it gets in (the ${defn("ENTRY", "entry")}), where it gives up (the ${defn("STOP", "stop")}) and where it takes its profit (the ${defn("TARGET", "target")}).`],
       [11, () => `The stop is a real order waiting at the broker. If this computer crashed, the stop would still close the trade.`],
       [21, () => `The distance from the entry to the stop is what the trade can lose. That amount is called <b>1R</b> — ${defn("R", "one unit of risk")}.`],
       [32, () => `So +1.5R means it won one and a half times what it risked, and −1R means the stop was hit and it lost exactly what it planned. <b>Drag the dot on the right</b> to try it.`],
     ],
     mount(stage, L, api) {
       const p = (L.state.positions || []).find(q => q.entry != null && q.sl != null && q.tp != null
         && Math.sign(q.tp - q.entry) === -Math.sign(q.sl - q.entry) && q.sl !== q.entry);
       const T = p ? {sym: p.symbol, side: String(p.side).toUpperCase(), entry: +p.entry, sl: +p.sl, tp: +p.tp, d: p.digits ?? 2,
                       real: true, cur: p.current, pnl: p.floating_pnl}
                   : {sym: "Example", side: "BUY", entry: 100, sl: 99, tp: 101.5, d: 2, real: false};
       const dir = Math.sign(T.tp - T.entry), risk = Math.abs(T.entry - T.sl), tR = Math.abs(T.tp - T.entry) / risk;
       let riskMoney = null;
       if (T.real && T.cur != null && T.pnl != null && Math.abs(T.cur - T.entry) > risk * 0.02) {
         const per = T.pnl / ((T.cur - T.entry) * dir); if (isFinite(per) && per > 0) riskMoney = per * risk;
       }
       if (riskMoney == null) riskMoney = 10;
       const lo = Math.min(T.sl, T.tp), hi = Math.max(T.sl, T.tp), padP = (hi - lo) * 0.18;
       const pLo = lo - padP, pHi = hi + padP, X0 = 70, X1 = 610, Ytop = 26, Ybot = 300;
       const Y = v => Ytop + (pHi - v) / (pHi - pLo) * (Ybot - Ytop), Yinv = y => pHi - (y - Ytop) / (Ybot - Ytop) * (pHi - pLo);
       const priceAtR = r => T.entry + r * risk * dir;
       // the illustrative path, in R: a scare towards the stop, then the run to the target
       const keys = [[0, 0], [0.1, 0.2], [0.26, -0.35], [0.4, -0.72], [0.52, -0.25], [0.66, 0.45], [0.78, 0.95], [0.86, 0.8], [1, tR]];
       const r0 = rng(7), N = 150;
       const path = Array.from({length: N + 1}, (_, i) => {
         const u = i / N; let k = 1; while (keys[k][0] < u) k++;
         const [ua, ra] = keys[k - 1], [ub, rb] = keys[k];
         const r = ra + (rb - ra) * smooth((u - ua) / (ub - ua)) + (i && i < N ? (r0() - 0.5) * 0.12 : 0);
         return [X0 + u * (X1 - X0), Y(priceAtR(r))];
       });
       const lbl = v => fmt(v, T.d);
       stage.innerHTML = `${T.real ? `<p class="lr-note">Drawn with one of your open trades — ${escH(T.sym)} ${T.side.toLowerCase()}: entry ${lbl(T.entry)}, stop ${lbl(T.sl)}, target ${lbl(T.tp)}. The price path is illustrative.</p>` : ""}
         <svg class="lr-svg" viewBox="0 0 800 320" role="img" aria-label="A trade between its stop and its target">
         <rect x="${X0}" y="${Math.min(Y(T.entry), Y(T.tp))}" width="${X1 - X0}" height="${Math.abs(Y(T.tp) - Y(T.entry))}" class="zone good"/>
         <rect x="${X0}" y="${Math.min(Y(T.entry), Y(T.sl))}" width="${X1 - X0}" height="${Math.abs(Y(T.sl) - Y(T.entry))}" class="zone bad"/>
         <line x1="${X0}" x2="${X1 + 60}" y1="${Y(T.tp)}" y2="${Y(T.tp)}" class="ln good" stroke-dasharray="6 5"/>
         <line x1="${X0}" x2="${X1 + 60}" y1="${Y(T.entry)}" y2="${Y(T.entry)}" class="ln dim"/>
         <line x1="${X0}" x2="${X1 + 60}" y1="${Y(T.sl)}" y2="${Y(T.sl)}" class="ln bad" stroke-dasharray="6 5"/>
         <text x="${X1 + 66}" y="${Y(T.tp) + 4}" class="tx good">target +${fmt(tR, 1)}R</text>
         <text x="${X1 + 66}" y="${Y(T.entry) + 4}" class="tx">entry</text>
         <text x="${X1 + 66}" y="${Y(T.sl) + 4}" class="tx bad">stop −1R</text>
         <g class="brace" opacity="0"><path d="M58 ${Y(T.entry)} h-10 V${Y(T.sl)} h10" class="ln wait"/>
           <text x="40" y="${(Y(T.entry) + Y(T.sl)) / 2 + 4}" class="tx wait" text-anchor="end">1R</text></g>
         <polyline class="ln sky path" points=""/>
         <line class="rail" x1="${X1 + 30}" x2="${X1 + 30}" y1="${Ytop}" y2="${Ybot}"/>
         <g class="handle" data-drag="1" tabindex="0" role="slider" aria-label="Drag to move the price" aria-valuemin="${lbl(pLo)}" aria-valuemax="${lbl(pHi)}">
           <circle r="16" class="hit"/><circle r="9" class="knob"/></g>
         <text class="tx c hint" x="${X1 + 30}" y="${Ybot + 16}">drag me</text></svg>
         <div class="lr-readout" aria-live="polite"></div>`;
       const svg = stage.querySelector("svg"), pl = svg.querySelector(".path"), h = svg.querySelector(".handle");
       const out = stage.querySelector(".lr-readout"), brace = svg.querySelector(".brace");
       let manual = null, dragging = false;
       const show = price => {
         const y = Y(price); h.setAttribute("transform", `translate(${X1 + 30},${y.toFixed(1)})`);
         h.setAttribute("aria-valuenow", lbl(price));
         const r = (price - T.entry) * dir / risk, money = r * riskMoney;
         const m = v => `${sgn(v)} ${escH(L.ccy)}${T.real ? "" : " (if it risked 10)"}`;
         out.className = "lr-readout " + (r > 0.02 ? "good" : r < -0.02 ? "bad" : "");
         out.innerHTML = r >= tR - 1e-9 ? `Price reached the target: the target order fills and banks <b>+${fmt(tR, 2)}R</b> = ${m(tR * riskMoney)}.`
           : r <= -1 + 1e-9 ? `Price reached the stop: the stop order fills. <b>−1R</b> = ${m(-riskMoney)} — exactly the loss it planned for, no more.`
           : `If price is here: <b>${sgn(r)}R</b> ≈ ${m(money)}. Nothing is banked until the stop or the target is reached.`;
       };
       const fromEvent = e => { const pt = svgPoint(svg, e); manual = clamp(Yinv(clamp(pt.y, Ytop, Ybot)), Math.min(T.sl, T.tp), Math.max(T.sl, T.tp)); show(manual); };
       h.addEventListener("pointerdown", e => { dragging = true; h.setPointerCapture(e.pointerId); api.pause(); fromEvent(e); });
       h.addEventListener("pointermove", e => { if (dragging) fromEvent(e); });
       h.addEventListener("pointerup", () => { dragging = false; });
       svg.querySelector(".rail").addEventListener("pointerdown", e => { api.pause(); fromEvent(e); });
       h.addEventListener("keydown", e => {
         if (!["ArrowUp", "ArrowDown"].includes(e.key)) return;
         e.preventDefault(); e.stopPropagation(); api.pause();
         const cur = manual ?? T.entry, step = risk * 0.1 * (e.key === "ArrowUp" ? 1 : -1);
         manual = clamp(cur + step, Math.min(T.sl, T.tp), Math.max(T.sl, T.tp)); show(manual);
       });
       return {tick(t) {
         const u = REDUCED ? 1 : clamp((t - 2) / 28, 0, 1), k = Math.max(1, Math.round(u * N));
         pl.setAttribute("points", path.slice(0, k + 1).map(q => q[0].toFixed(1) + "," + q[1].toFixed(1)).join(" "));
         brace.setAttribute("opacity", t >= 21 || REDUCED ? 1 : 0);
         if (manual == null) show(Yinv(path[k][1]));
       }};
     }},

    // ---------------------------------------------------------------- 3
    {short: "Why it waits", title: "Why it mostly waits", secs: 44,
     beats: [
       [0, L => L.funnel.looked ? `Today it has looked at <b>${fmt(L.funnel.looked, 0)}</b> chances so far — every finished candle, on every market, for every rule.`
                                : `Nothing has been recorded yet today. Once it runs for a while, every chance it looks at flows through this pipe.`],
       [9, L => L.funnel.on ? `<b>${pc(1 - L.funnel.matched / L.funnel.on)}</b> of the chances its switched-on rules looked at dropped out because the chart simply did not look like the setup a rule waits for. That is normal: the rules are meant to be picky.`
                            : `Most chances drop out at the first step, because the chart does not look like the setup a rule waits for. That is normal.`],
       [20, () => `The few that do match then face safety checks: is the ${defn("SPREAD", "buy/sell gap")} too wide, is big news due, is there risk budget left, has this rule been losing lately?`],
       [32, L => `<b>${fmt(L.funnel.traded, 0)}</b> became trades. Waiting is not the bot being broken — it is the bot working. <b>Tap a gap in the pipe</b> to see the actual reasons.`],
     ],
     mount(stage, L, api) {
       stage.innerHTML = `<canvas class="lr-flow" aria-label="Chances flowing through the bot's checks"></canvas>
         <div class="lr-reasons" aria-live="polite"></div>
         <p class="lr-note">The pipe narrows on a log scale so the last step stays visible; the numbers are the real counts for today.</p>`;
       const box = stage.querySelector(".lr-reasons");
       const showGap = i => {
         const step = L.funnel.steps[i], list = (L.funnel.reasons[i] || []).slice(0, 5);
         if (!step) return;
         const max = Math.max(1, ...list.map(x => x.n));
         box.innerHTML = `<b>${fmt(L.funnel.steps[i - 1].value - step.value, 0)} ${escH(step.drop.label)}</b> <span class="mut">— ${escH(step.drop.why)}</span>
           ${list.length ? `<div class="lr-rlist">${list.map(x => `<div class="lr-r ${x.tone}"><span>${escH(x.say)}</span>
             <i style="width:${Math.max(2, 100 * x.n / max)}%"></i><b>${fmt(x.n, 0)}</b></div>`).join("")}</div>` : ""}`;
       };
       const fl = VZ.flow(stage.querySelector("canvas"), {onPick: i => { api.pause(); showGap(i); }});
       fl.set(L.funnel.steps);
       return {beat(k) { if (k === 1) showGap(2); if (k === 3) showGap(3); }, destroy() { fl.destroy(); }};
     }},

    // ---------------------------------------------------------------- 4
    {short: "The hidden toll", title: "The hidden toll: the buy/sell gap", secs: 42,
     beats: [
       [0, () => `Every market has two prices: one you can buy at and a slightly lower one you can sell at. The gap between them is how the broker gets paid.`],
       [10, () => `So every trade starts a little behind. The moment it opens it is down by the gap, and it has to travel that far just to get back to zero.`],
       [21, L => `On ${escH(L.toll.sym)} right now the gap is <b>${fmt(L.toll.spread, L.toll.d)}</b>. With a small stop, that gap can be most of what the trade risks — a losing game, however good the chart looks.`],
       [31, L => `So the bot refuses any trade where the gap is more than <b>${pc(L.toll.maxFrac)}</b> of the stop distance. <b>Slide the stop size</b> and watch the share change.`],
     ],
     mount(stage, L, api) {
       const {spread, maxFrac} = L.toll;
       stage.innerHTML = `<svg class="lr-svg short" viewBox="0 0 800 190" role="img" aria-label="The buy price above the sell price, with the gap between them">
           <polygon class="gapband" points=""/>
           <polyline class="ln good ask" points=""/><polyline class="ln bad bid" points=""/>
           <text x="24" y="30" class="tx good">buy price</text><text x="24" y="150" class="tx bad">sell price</text>
           <text x="776" y="30" class="tx wait" text-anchor="end">the shaded gap — drawn bigger than life</text>
           <g class="buy" opacity="0"><circle cx="300" class="fl good d-ask" r="7"/><circle cx="300" class="fl bad d-bid" r="7"/>
             <line x1="312" x2="312" class="ln bad d-gap"/>
             <text x="322" y="58" class="tx">you buy here…</text><text x="322" y="150" class="tx">…and it is worth this the instant you own it</text>
             <text x="322" y="176" class="tx bad">so it starts ${escH(fmt(spread, L.toll.d))} behind</text></g></svg>
         <div class="toll">
           <label class="lr-lab">Stop distance: <b class="v-stop"></b> <span class="mut v-mult"></span>
             <input type="range" min="1" max="30" step="0.5" value="20" class="lr-range" aria-label="Stop distance as a multiple of the gap"></label>
           <div class="toll-bar"><i class="toll-gap"></i><span class="toll-cap">what the trade risks</span></div>
           <div class="lr-readout v-verdict" aria-live="polite"></div></div>`;
       const svg = stage.querySelector("svg"), ask = svg.querySelector(".ask"), bid = svg.querySelector(".bid");
       const range = stage.querySelector("input"), bar = stage.querySelector(".toll-gap"), verdict = stage.querySelector(".v-verdict");
       let touched = false;
       const set = m => {
         const stop = spread * m, share = 1 / m, bad = share > maxFrac + 1e-9;
         stage.querySelector(".v-stop").textContent = fmt(stop, L.toll.d);
         stage.querySelector(".v-mult").textContent = L.toll.atr ? `(${fmt(stop / L.toll.atr, 1)}× its typical 15-minute move)` : "";
         bar.style.width = clamp(share * 100, 0.5, 100) + "%"; bar.className = "toll-gap " + (bad ? "bad" : "wait");
         verdict.className = "lr-readout v-verdict " + (bad ? "bad" : "good");
         verdict.innerHTML = `The gap eats <b>${pc(share, share < 0.1 ? 1 : 0)}</b> of what this trade risks. ` + (bad
           ? `<b>The bot refuses this</b> — the gap would eat the trade (its limit is ${pc(maxFrac)}).`
           : `Allowed — the gap is a small share of the risk.`);
       };
       range.addEventListener("input", () => { touched = true; api.pause(); set(+range.value); });
       set(+range.value);
       const band = svg.querySelector(".gapband");
       const yAt = (i, w) => Math.sin(i * 0.55 + w * 1.4) * 9 + Math.sin(i * 0.17 + w * 0.6) * 6;
       return {tick(t) {
         const w = REDUCED ? 0 : t;
         // the two prices move together; the shaded gap between them moves with them
         const pts = off => Array.from({length: 41}, (_, i) => [20 + i * 19, off + yAt(i, w)]);
         const a = pts(80), b = pts(120), s = p => p.map(q => q[0] + "," + q[1].toFixed(1)).join(" ");
         ask.setAttribute("points", s(a)); bid.setAttribute("points", s(b));
         band.setAttribute("points", s(a) + " " + s(b.slice().reverse()));
         const yb = 80 + yAt(15, w);         // the buy dot rides the line at x = 300 (point 15)
         svg.querySelector(".d-ask").setAttribute("cy", yb.toFixed(1)); svg.querySelector(".d-bid").setAttribute("cy", (yb + 40).toFixed(1));
         const gl = svg.querySelector(".d-gap"); gl.setAttribute("y1", yb.toFixed(1)); gl.setAttribute("y2", (yb + 40).toFixed(1));
         svg.querySelector(".buy").setAttribute("opacity", t >= 10 || REDUCED ? 1 : 0);
         if (!touched) {
           const m = REDUCED ? (t >= 31 ? 14 : t >= 21 ? 3 : 20) : t < 21 ? 20 : t < 30 ? lerp(20, 2.5, (t - 21) / 7) : lerp(2.5, 14, (t - 32) / 6);
           range.value = m; set(m);
         }
       }};
     }},

    // ---------------------------------------------------------------- 5
    {short: "Seatbelts", title: "Seatbelts that work on their own", secs: 42,
     beats: [
       [0, () => `Like a car, the bot has seatbelts that work by themselves. The main one watches how far the account has fallen from its best value — its ${defn("DRAWDOWN", "drawdown")}.`],
       [9, L => `Down ${pc(L.risk.dd_reduced)} from the best → every new trade is half size. Down ${pc(L.risk.dd_restricted)} → a quarter. Down ${pc(L.risk.dd_no_new_trades)} → no new trades at all.`],
       [20, L => `There is a daily limit too: once a day's losses reach ${pc(L.risk.daily_loss_limit_fraction)} of what the account was worth that morning, it opens nothing new until the next day.`],
       [30, L => `And there is you: the stop button at the top of every page halts all new trades at once; open trades keep their stop and target at the broker. Right now the account is <b>${pc(L.risk.dd, 1)}</b> below its best, so it trades at <b>${pc(L.multAt(L.risk.dd))}</b> of normal size. <b>Drag the slider</b> to see the seatbelts tighten.`],
     ],
     mount(stage, L, api) {
       const r = L.risk, maxDD = Math.max(0.35, r.dd_emergency + 0.05);
       const Y = dd => 36 + dd / maxDD * 236;
       const tiers = [[0, r.dd_reduced, "full size", "good"], [r.dd_reduced, r.dd_restricted, "half size", "wait"],
         [r.dd_restricted, r.dd_no_new_trades, "quarter size", "wait"], [r.dd_no_new_trades, r.dd_emergency, "no new trades", "bad"],
         [r.dd_emergency, maxDD, "emergency stop", "bad"]];
       stage.innerHTML = `<svg class="lr-svg" viewBox="0 0 800 300" role="img" aria-label="How trade size shrinks as the account falls from its best">
         <text x="60" y="18" class="tx">how far below its best</text>
         ${tiers.map(([a, b, name, tone], i) => `<rect x="60" y="${Y(a)}" width="120" height="${Y(b) - Y(a)}" class="tier ${tone} t${i}"/>
           <text x="214" y="${(Y(a) + Y(b)) / 2 + 4}" class="tx ${tone}">${name}</text>
           <text x="50" y="${Y(a) + 4}" class="tx num" text-anchor="end">${a ? "−" : ""}${fmt(a * 100, 0)}%</text>`).join("")}
         <g class="ddmark"><line x1="56" x2="186" y1="0" y2="0" class="ln text"/><polygon points="186,0 200,-7 200,7" class="fl text"/></g>
         <text x="470" y="60" class="tx v-dd"></text>
         <text x="470" y="100" class="tx">size of each new trade</text>
         <rect x="470" y="112" width="300" height="38" rx="7" class="barbg"/><rect x="470" y="112" width="300" height="38" rx="7" class="sizebar good"/>
         <text x="470" y="178" class="tx big v-size"></text>
         <g class="mockbtn" opacity="0"><rect x="470" y="214" width="214" height="36" rx="7"/><text x="577" y="237" class="tx c">Stop all new trades</text>
           <text x="470" y="270" class="tx mut">(a drawing — the real button is at the top of every page)</text></g></svg>
         <label class="lr-lab">What if the account fell this far from its best? <b class="v-sl"></b>
           <input type="range" min="0" max="${(maxDD * 100).toFixed(0)}" step="0.5" value="0" class="lr-range" aria-label="How far the account has fallen, in percent"></label>`;
       const svg = stage.querySelector("svg"), mark = svg.querySelector(".ddmark"), size = svg.querySelector(".sizebar"), range = stage.querySelector("input");
       let touched = false;
       const set = dd => {
         const m = L.multAt(dd), name = dd >= r.dd_emergency ? "emergency stop — nothing new" : dd >= r.dd_no_new_trades ? "no new trades" : `${pc(m)} of normal size`;
         mark.setAttribute("transform", `translate(0,${Y(dd).toFixed(1)})`);
         svg.querySelector(".v-dd").textContent = `The arrow: the account is ${pc(dd, 1)} below its best`;
         size.setAttribute("width", Math.max(0.01, 300 * m)); size.setAttribute("class", "sizebar " + (m >= 1 ? "good" : m > 0 ? "wait" : "bad"));
         svg.querySelector(".v-size").textContent = name;
         stage.querySelector(".v-sl").textContent = pc(dd, 1);
         svg.querySelectorAll(".tier").forEach((el, i) => el.classList.toggle("on", dd >= tiers[i][0] && dd < tiers[i][1]));
       };
       range.addEventListener("input", () => { touched = true; api.pause(); set(+range.value / 100); });
       return {tick(t) {
         svg.querySelector(".mockbtn").setAttribute("opacity", t >= 30 || REDUCED ? 1 : 0);
         if (touched) return;
         const dd = REDUCED ? (t >= 30 ? r.dd : t >= 9 ? r.dd_restricted + 0.01 : 0)
           : t < 9 ? 0 : t < 26 ? lerp(0, maxDD - 0.02, (t - 9) / 15) : lerp(maxDD - 0.02, r.dd, (t - 28) / 3);
         range.value = (dd * 100).toFixed(1); set(dd);
       }};
     }},

    // ---------------------------------------------------------------- 6
    {short: "Win rate is a trap", title: "Win rate is a trap. Is it luck?", secs: 58,
     beats: [
       [0, () => `Most people judge a trader by how often they win. That is the wrong number.`],
       [9, () => `What matters is the ${defn("EXPECTANCY", "average result per trade")} after costs, in ${defn("R")}: how much a typical trade adds or takes away.`],
       [19, () => `A rule can win 70% of the time and still lose money if its wins are small and its losses are big — and win 35% of the time and make money. Try both buttons.`],
       [32, () => `And a good average can still be luck. The bar is the ${defn("RANGE95", "95% range")}: if it crosses zero, the result has not been shown to work yet. More trades make it narrower — try 20 against 500.`],
       [46, L => L.pfSentence],
     ],
     mount(stage, L, api) {
       const S = {p: 0.55, w: 1.2, l: 1, c: 0.05, n: 100, seed: 3, fan: false};
       stage.innerHTML = `<div class="sim">
         <div class="sim-ctl">
           <div class="sim-pre"><button class="btn small" data-pre="a">Wins 70%, still loses</button><button class="btn small" data-pre="b">Wins 35%, still makes money</button></div>
           ${[["p", "How often it wins", 0.1, 0.9, 0.01], ["w", "Average win (R)", 0.2, 4, 0.1], ["l", "Average loss (R)", 0.2, 2, 0.1], ["c", "Costs per trade (R)", 0, 0.3, 0.01]]
             .map(([k, lab, a, b, s]) => `<label class="lr-lab">${lab} <b class="v-${k}"></b><input type="range" data-k="${k}" min="${a}" max="${b}" step="${s}" class="lr-range"></label>`).join("")}
           <div class="sim-pre"><span class="mut">Trades:</span>${[20, 100, 500].map(v => `<button class="btn small" data-n="${v}">${v}</button>`).join("")}
             <button class="btn small" data-a="luck">Run it 30 times</button><button class="btn small" data-a="seed">New luck</button></div>
           <div class="lr-readout v-e" aria-live="polite"></div></div>
         <div class="sim-out"><canvas class="sim-cv" aria-label="Simulated account in R, trade by trade"></canvas><div class="v-range"></div><div class="lr-note v-grade"></div></div></div>`;
       const cv = stage.querySelector("canvas"), q = s => stage.querySelector(s);
       let runs = [], main = null, prog = 1, stopA = () => {}, touched = false;
       const simulate = seed => { const rnd = rng(seed), rs = [], curve = [0]; let cum = 0;
         for (let i = 0; i < S.n; i++) { const x = rnd() < S.p ? S.w - S.c : -S.l - S.c; rs.push(x); cum += x; curve.push(cum); }
         return {rs, curve}; };
       function draw() {
         const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight; if (!W) return;
         cv.width = W * dpr; cv.height = H * dpr; const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
         const c = VZ.COLOR(), all = (S.fan ? runs : []).concat(main ? [main] : []);
         let lo = 0, hi = 0; all.forEach(r => r.curve.forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); }));
         if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
         const X = i => 36 + i / S.n * (W - 46), Yc = v => 10 + (hi - v) / (hi - lo) * (H - 30);
         g.font = `11px ${c.num}`; g.fillStyle = c.faint; g.fillText(sgn(hi, 0) + "R", 2, Yc(hi) + 4); g.fillText(sgn(lo, 0) + "R", 2, Yc(lo));
         g.strokeStyle = c.rule; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(36, Yc(0)); g.lineTo(W - 10, Yc(0)); g.stroke(); g.setLineDash([]);
         g.fillStyle = c.faint; g.fillText("0 = break even", 40, Yc(0) - 5); g.fillText(`trade 1 → ${S.n}`, W - 90, H - 6);
         const line = (r, upto, col, wdt, a) => { g.globalAlpha = a; g.strokeStyle = col; g.lineWidth = wdt; g.beginPath();
           r.curve.slice(0, upto + 1).forEach((v, i) => i ? g.lineTo(X(i), Yc(v)) : g.moveTo(X(i), Yc(v))); g.stroke(); g.globalAlpha = 1; };
         if (S.fan) runs.forEach(r => line(r, S.n, r.curve[S.n] >= 0 ? c.seafoam : c.coral, 1, 0.28));
         if (main) line(main, Math.max(1, Math.round(S.n * prog)), main.curve[S.n] >= 0 ? c.seafoam : c.coral, 2.4, 1);
       }
       function update(animate) {
         ["p", "w", "l", "c"].forEach(k => { q(`[data-k="${k}"]`).value = S[k]; });
         q(".v-p").textContent = pc(S.p); q(".v-w").textContent = fmt(S.w, 1); q(".v-l").textContent = fmt(S.l, 1); q(".v-c").textContent = fmt(S.c, 2);
         stage.querySelectorAll("[data-n]").forEach(b => b.classList.toggle("on", +b.dataset.n === S.n));
         const E = S.p * S.w - (1 - S.p) * S.l - S.c;
         const e = q(".v-e"); e.className = "lr-readout v-e " + (E > 0.005 ? "good" : E < -0.005 ? "bad" : "");
         e.innerHTML = `Average per trade = ${pc(S.p)} × ${fmt(S.w, 1)} − ${pc(1 - S.p)} × ${fmt(S.l, 1)} − ${fmt(S.c, 2)} = <b>${sgn(E, 3)}R</b>
           <br><span class="mut">${E > 0.005 ? "Over many trades this makes money" : E < -0.005 ? "Over many trades this loses money" : "Over many trades this breaks even"}, whatever the win rate says.</span>`;
         main = simulate(S.seed);
         runs = S.fan ? Array.from({length: 30}, (_, i) => simulate(S.seed * 97 + i + 1)) : [];
         const n = S.n, m = main.rs.reduce((a, b) => a + b, 0) / n;
         const sd = Math.sqrt(main.rs.reduce((a, b) => a + (b - m) * (b - m), 0) / Math.max(1, n - 1)), half = 1.96 * sd / Math.sqrt(n);
         VZ.forest(q(".v-range"), [{label: "Measured per trade", sub: `from this run of ${n} trades`, lo: m - half, mid: m, hi: m + half}],
           {big: true, min: Math.min(-1, m - half), max: Math.max(1, m + half)});
         const grade = n < 30 ? ["THIN", "Too few trades to say anything yet."] : m - half > 0 ? ["shown to make money", "Even the unlucky end of the range is above zero."]
           : m + half < 0 ? ["NEGATIVE", "Even the lucky end of the range is below zero."] : ["NO EDGE SHOWN", "The range crosses zero: this result could be luck either way."];
         q(".v-grade").innerHTML = `<b>${grade[0]}</b> — ${grade[1]} <span class="mut">The true average for these settings is ${sgn(E, 3)}R; one run of ${n} trades measured ${sgn(m, 3)}R.</span>`;
         stopA(); stopA = animate ? VZ.tween(0, 1, 1500, v => { prog = v; draw(); }) : (prog = 1, draw(), () => {});
       }
       const preset = k => { Object.assign(S, k === "a" ? {p: 0.7, w: 0.3, l: 1, c: 0.05} : {p: 0.35, w: 2.5, l: 1, c: 0.05}); update(true); };
       stage.addEventListener("input", e => { const k = e.target.dataset.k; if (!k) return; touched = true; api.pause(); S[k] = +e.target.value; update(false); });
       stage.addEventListener("click", e => {
         const b = e.target.closest("button"); if (!b) return; touched = true; api.pause();
         if (b.dataset.pre) preset(b.dataset.pre);
         if (b.dataset.n) { S.n = +b.dataset.n; update(true); }
         if (b.dataset.a === "luck") { S.fan = true; update(true); }
         if (b.dataset.a === "seed") { S.seed += 1; update(true); }
       });
       let done = {};
       const once = (k, f) => { if (!done[k]) { done[k] = 1; f(); } };
       if (window.ResizeObserver) new ResizeObserver(() => draw()).observe(cv);
       update(true);
       return {tick(t) {
         if (touched) return;
         if (t >= 21) once("a", () => preset("a"));
         if (t >= 27) once("b", () => preset("b"));
         if (t >= 33) once("fan", () => { S.fan = true; update(true); });
         if (t >= 39) once("n20", () => { S.n = 20; update(true); });
         if (t >= 43) once("n500", () => { S.n = 500; update(true); });
       }, destroy() { stopA(); }};
     }},

    // ---------------------------------------------------------------- 7
    {short: "Your daily check", title: "Your five-minute daily check", secs: 40,
     beats: [
       [0, () => `That is the whole idea. Here is a five-minute routine for every day.`],
       [5, () => `<b>1</b> · Read the sentence at the top of the Overview. Seafoam means working, sand means waiting, coral means look now.`],
       [11, () => `<b>2</b> · Check what trading made or lost today, in money. Deposits are shown separately, so they never look like profit.`],
       [17, () => `<b>3</b> · Glance at the open trades. Each bar runs from the stop on the left to the target on the right.`],
       [23, () => `<b>4</b> · If it is quiet, the funnel tells you why — usually that nothing matched a rule, which is fine.`],
       [29, () => `<b>5</b> · Once a week, open Analysis and judge each rule by its average per trade and its range — never by its win rate.`],
     ],
     mount(stage, L, api) {
       const spots = [["Today in one sentence", "hero"], ["Today's money", "money"], ["Open trades", "open"], ["Why not more trades", "funnel"], ["Analysis — weekly", "analysis"]];
       stage.innerHTML = `<div class="minimap" aria-label="A map of the dashboard">
           ${spots.map(([label, cls], i) => `<div class="mm ${cls}" data-i="${i}" tabindex="0"><span class="mm-n">${i + 1}</span><span>${label}</span></div>`).join("")}
         </div>
         <div class="lr-end"><button class="btn green" data-a="tour">Show me on the real page</button>
           <button class="btn green" data-a="deeper">Go deeper: five more scenes</button>
           <button class="btn" data-a="home">${api.inline ? "Go to the dashboard" : "Close and look around"}</button></div>`;
       const mm = [...stage.querySelectorAll(".mm")];
       stage.addEventListener("click", e => {
         const b = e.target.closest("button");
         if (b) { if (b.dataset.a === "tour") api.tour(); else if (b.dataset.a === "deeper") api.series("deeper"); else api.close(); return; }
         const m = e.target.closest(".mm"); if (m) { api.pause(); api.seek(SCENES[6].beats[+m.dataset.i + 1][0]); }
       });
       return {beat(k) { mm.forEach((m, i) => m.classList.toggle("on", i === k - 1)); }};
     }},
  ];

  // ================================================================ going deeper
  /* A second series, for after the first five minutes: what happens between "a rule matched" and "money banked".
   * Each scene draws one mechanism the way the code applies it - the sizing budgets (app/risk/sizing.py), the
   * breakeven and profit-lock ladder (app/alpha/exits.py), the market-mood rule (app/regime/detector.py), the news
   * window and the straddle (app/market/events.py, app/execution/straddle.py) and the open-risk limits
   * (app/storage/repo.py) - with the thresholds read from the live config. Where a picture needs a market or a price
   * path that is not the bot's own, the scene says it is an example. */
  const money = (L, v, d = 2) => `${fmt(v, d)} ${escH(L.ccy)}`;
  const base = s => String(s).replace(/[a-z.]+$/, "").toUpperCase();          // XAUUSDm -> XAUUSD
  // the currencies a market moves on, as app/market/events.py:symbol_currencies reads them from the broker spec
  const ccyOf = s => { const b = base(s); return /^[A-Z]{6}$/.test(b) ? [b.slice(0, 3), b.slice(3)] : ["USD"]; };
  const MOOD = {TREND_UP: ["trending up", "up"], TREND_DOWN: ["trending down", "down"], RANGE: ["sideways", "range"],
    TRANSITION: ["changing its mind", "trans"], STRESS: ["stressed", "stress"], UNKNOWN: ["not enough history yet", "trans"]};
  const ruleName = id => String(id).replace(/_v\d+$/, "").replace(/_/g, " ");

  const DEEPER = [
    // ---------------------------------------------------------------- deeper 1
    {short: "How big?", title: "How big each trade is", secs: 48,
     beats: [
       [0, L => `Before every trade the bot decides how much it is willing to lose if the ${defn("STOP", "stop")} is hit: a fixed slice of the account. ${pc(L.size.research)} while a rule is still proving itself, never more than ${pc(L.size.cap, 1)}${L.size.live ? ` — on ${money(L, L.size.equity, 0)} that is at most <b>${money(L, L.size.equity * L.size.cap)}</b>` : ""}.`],
       [10, () => `Then it works backwards. The chart decides where the stop goes; the ${defn("LOT", "trade size")} is whatever makes "stop hit" cost exactly that slice.`],
       [20, () => `A far stop means a small trade, a near stop a bigger one — and the money at risk stays the same. <b>Drag the stop</b> and watch.`],
       [31, L => `Several limits must all agree: this trade's slice, all open trades together (${pc(L.size.total)} of the account), what is left of today's loss limit (${pc(L.risk.daily_loss_limit_fraction)}) and the margin the broker asks for. The smallest one sets the size.`],
       [40, () => `And it never rounds up. If even the broker's smallest size would risk more than the slice, there is <b>no trade</b> — it does not quietly take the extra risk.`],
     ],
     mount(stage, L, api) {
       const Z = L.size, eq = Z.live ? Z.equity : 1000, slice = eq * Z.research;
       const PER = 100, MIN = 0.01;                  // example market: 100 per lot for each 1.00 of price, as gold
       const sAt = slice / (PER * MIN);              // beyond this stop even the smallest size is too much
       const sMin = slice / (PER * Z.maxVol) * 0.5, sMax = sAt * 1.6, LR = Math.log(sMax / sMin);
       const stopAt = u => sMin * Math.exp(u * LR);
       const Ye = 70, Ys = u => Ye + 22 + u * 190, X0 = 40, X1 = 360, BW = 330;
       const r0 = rng(4);
       let y = Ye - 30;
       const squig = Array.from({length: 27}, (_, i) => { y = i === 26 ? Ye : clamp(y + (r0() - 0.5) * 16 + (Ye - 4 - y) * 0.12, 18, Ye + 14); return `${X0 + i * 10},${y.toFixed(1)}`; }).join(" ");
       stage.innerHTML = `<p class="lr-note">${Z.live ? `The slice is ${pc(Z.research)} of your account's value, ${money(L, eq, 0)}.` : "An example account worth 1,000."} The market is an example: each 1.00 the price moves is worth ${PER} ${escH(L.ccy)} per lot, as with gold.</p>
         <svg class="lr-svg" viewBox="0 0 800 300" role="img" aria-label="The stop distance, the trade size and the money at risk">
         <polyline points="${squig}" class="ln dim"/>
         <rect x="${X0}" y="${Ye}" width="${X1 - X0}" height="0" class="zone bad v-zone"/>
         <line x1="${X0}" x2="${X1}" y1="${Ye}" y2="${Ye}" class="ln text"/><text x="${X1 + 8}" y="${Ye + 4}" class="tx">entry</text>
         <g class="v-stop"><line x1="${X0}" x2="${X1}" y1="0" y2="0" class="ln bad" stroke-dasharray="6 5"/>
           <text x="${X1 + 8}" y="4" class="tx bad">stop</text><text x="${X0 + 6}" y="-8" class="tx num v-dist"></text>
           <g class="handle" data-drag="1"><circle cx="300" r="16" class="hit"/><circle cx="300" r="9" class="knob"/></g></g>
         <text x="440" y="40" class="tx">trade size</text>
         <rect x="440" y="50" width="${BW}" height="26" rx="6" class="barbg"/><rect x="440" y="50" width="0" height="26" rx="6" class="sizebar good v-lots"/>
         <text x="440" y="104" class="tx big v-lotsn"></text>
         <text x="440" y="146" class="tx">money lost if the stop is hit</text>
         <rect x="440" y="156" width="${BW}" height="26" rx="6" class="barbg"/><rect x="440" y="156" width="0" height="26" rx="6" class="sizebar good v-risk"/>
         <line x1="${440 + BW}" x2="${440 + BW}" y1="150" y2="188" class="ln wait"/><text x="${440 + BW}" y="202" class="tx wait" text-anchor="end">the slice: ${escH(money(L, slice))}</text>
         <text x="440" y="236" class="tx num v-sum"></text></svg>
         <label class="lr-lab">Stop distance <input type="range" min="0" max="1000" step="1" class="lr-range" aria-label="Stop distance"></label>
         <div class="lr-readout v-verdict" aria-live="polite"></div>
         <div class="lr-budgets"></div>`;
       const svg = stage.querySelector("svg"), q = s => stage.querySelector(s), range = q("input");
       const day = eq * L.risk.daily_loss_limit_fraction + Math.min(0, +((L.state.account || {}).today_pnl) || 0);
       const budgets = [["This trade's slice", slice, "the rule's share of the account"],
         ["All open trades together", eq * Z.total, "before what open trades already risk"],
         ["Today's loss limit", Math.max(0, day), "what is left of it today"]];
       const bmax = Math.max(...budgets.map(b => b[1]));
       q(".lr-budgets").innerHTML = budgets.map(([n, v, why], i) => `<div class="lr-r ${i ? "" : "good"}"><span>${n} <span class="mut">— ${why}</span></span>
         <i style="width:${Math.max(2, 100 * v / bmax)}%"></i><b>${fmt(v)}</b></div>`).join("")
         + `<div class="lr-r"><span>Margin <span class="mut">— set by the broker, checked at the moment of the trade</span></span><i style="width:0"></i><b>–</b></div>`;
       let touched = false;
       const set = u => {
         u = clamp(u, 0, 1); range.value = Math.round(u * 1000);
         const s = stopAt(u), raw = slice / (s * PER), lots = Math.floor(Math.min(raw, Z.maxVol) / MIN + 1e-9) * MIN;
         const ok = lots >= MIN - 1e-9, risk = ok ? lots * s * PER : 0, capped = raw > Z.maxVol, ys = Ys(u);
         q(".v-zone").setAttribute("height", (ys - Ye).toFixed(1));
         q(".v-stop").setAttribute("transform", `translate(0,${ys.toFixed(1)})`);
         q(".v-dist").textContent = `stop ${fmt(s, 2)} away`;
         const tone = !ok ? "bad" : capped ? "wait" : "good";
         q(".v-lots").setAttribute("width", (BW * clamp(lots / Z.maxVol, 0, 1)).toFixed(1)); q(".v-lots").setAttribute("class", "sizebar " + tone + " v-lots");
         q(".v-risk").setAttribute("width", (BW * clamp(risk / slice, 0, 1)).toFixed(1)); q(".v-risk").setAttribute("class", "sizebar " + tone + " v-risk");
         q(".v-lotsn").textContent = ok ? `${fmt(lots, 2)} lots` : "no trade";
         q(".v-sum").textContent = ok ? `${fmt(s, 2)} × ${fmt(lots, 2)} lots × ${PER} = ${fmt(risk)} ${L.ccy}` : `${fmt(s, 2)} × 0.01 lots × ${PER} = ${fmt(MIN * s * PER)} ${L.ccy} — over the slice`;
         const v = q(".v-verdict"); v.className = "lr-readout v-verdict " + tone;
         v.innerHTML = !ok ? `Even the smallest size, 0.01 lots, would lose ${money(L, MIN * s * PER)} here — more than the slice of ${money(L, slice)}. <b>No trade.</b>`
           : capped ? `The stop is so near that the slice would buy ${fmt(raw, 2)} lots, more than the ${fmt(Z.maxVol, 2)}-lot ceiling. It trades ${fmt(lots, 2)} lots and risks <b>less</b> than the slice.`
           : `${fmt(lots, 2)} lots: if the stop is hit it loses ${money(L, risk)}, inside the slice of ${money(L, slice)}.${raw - lots > 1e-6 ? ` <span class="mut">(${fmt(raw, 4)} rounded <b>down</b> to the broker's 0.01 steps.)</span>` : ""}`;
       };
       const fromEvent = e => { touched = true; api.pause(); set((svgPoint(svg, e).y - Ye - 22) / 190); };
       let dragging = false;
       svg.addEventListener("pointerdown", e => { if (!e.target.closest(".handle,.v-zone")) return; dragging = true; svg.setPointerCapture(e.pointerId); fromEvent(e); });
       svg.addEventListener("pointermove", e => { if (dragging) fromEvent(e); });
       svg.addEventListener("pointerup", () => { dragging = false; });
       range.addEventListener("input", () => { touched = true; api.pause(); set(+range.value / 1000); });
       set(0.42);
       return {tick(t) {
         q(".lr-budgets").classList.toggle("on", t >= 31);
         if (touched) return;
         set(REDUCED ? (t >= 40 ? 0.97 : t >= 20 ? 0.6 : 0.42)
           : t < 20 ? 0.42 : t < 26 ? lerp(0.42, 0.05, (t - 20) / 6) : t < 31 ? lerp(0.05, 0.6, (t - 26) / 5) : t < 40 ? 0.6 : lerp(0.6, 0.97, (t - 40) / 6));
       }};
     }},

    // ---------------------------------------------------------------- deeper 2
    {short: "Locking in", title: "Locking in a winner", secs: 54,
     beats: [
       [0, () => `Once a trade is open the bot keeps managing it. Its stop moves as the trade goes well — and only ever one way: towards safety.`],
       [10, L => `When the trade is ${fmt(L.exits.be, 1)}R ahead, the stop jumps to the entry plus enough to cover the ${defn("SPREAD", "buy/sell gap")}: ${defn("BREAKEVEN", "break even")}. From then on this trade cannot turn into a loss.`],
       [21, L => `At the same point a ${defn("PROFIT_LOCK", "profit lock")} switches on. ${L.exits.give > 0 ? `It keeps ${pc(1 - L.exits.give)} of the best profit so far, so every new high raises the floor.` : `It keeps ${fmt(L.exits.smallFloor, 1)}R, and ${fmt(L.exits.ambFloor, 1)}R once the trade has been ${fmt(L.exits.ambAct, 1)}R ahead.`} The floor never comes down.`],
       [33, () => `When the price turns and falls back to the floor, the trade closes with most of its gain kept, instead of riding all the way back.`],
       [43, () => `Trend-following rules switch the lock off: keeping most of a small early gain would end every big trend before it pays. <b>Try another path</b>, or switch the lock off, and compare the endings.`],
     ],
     mount(stage, L, api) {
       const E = L.exits, TGT = 2, BE = 0.03, N = 240, X0 = 50, X1 = 600, lo = -1.35, hi = 2.4;
       const Y = r => 18 + (hi - r) / (hi - lo) * 250, X = i => X0 + i / N * (X1 - X0);
       // app/alpha/exits.py:profit_lock_target, in R
       const lockAt = pk => E.ambAct > 0 && pk >= E.ambAct ? Math.max(E.ambFloor, E.give > 0 ? pk * (1 - E.give) : pk - (E.ambAct - E.ambFloor))
         : E.smallAct > 0 && pk >= E.smallAct ? (E.give > 0 ? Math.max(E.smallFloor, pk * (1 - E.give)) : E.smallFloor) : null;
       const scripted = r0 => { const keys = [[0, 0], [0.1, 0.22], [0.2, -0.28], [0.33, 0.62], [0.45, 0.95], [0.53, 0.72], [0.64, 1.3], [0.74, 1.68], [0.8, 1.42], [0.88, 0.6], [1, -1.1]];
         return Array.from({length: N + 1}, (_, i) => { const u = i / N; let k = 1; while (keys[k][0] < u) k++;
           const [ua, ra] = keys[k - 1], [ub, rb] = keys[k]; return ra + (rb - ra) * smooth((u - ua) / (ub - ua)) + (i && i < N ? (r0() - 0.5) * 0.08 : 0); }); };
       const walk = r0 => { let r = 0; const drift = (r0() - 0.45) * 0.02; return Array.from({length: N + 1}, (_, i) => i ? (r += drift + (r0() - 0.5) * 0.2) : 0); };
       const simulate = (p, mode) => {         // mode: "lock" | "be" (break even only) | "none" (stop never moves)
         let peak = 0, stop = -1, floor = -Infinity, out = null; const stops = [];
         for (let i = 0; i <= N; i++) {
           const r = p[i];
           if (!out && r <= stop) out = {i, r: stop, why: stop > BE + 1e-9 ? "lock" : stop > -1 ? "be" : "stop"};
           else if (!out && r >= TGT) out = {i, r: TGT, why: "target"};
           if (!out) {
             peak = Math.max(peak, r);
             if (mode !== "none" && peak >= E.be) stop = Math.max(stop, BE);
             const k = mode === "lock" ? lockAt(peak) : null;
             if (k != null) { floor = Math.max(floor, k); stop = Math.max(stop, floor); }
           }
           stops.push(stop);
         }
         return {stops, out, peak};
       };
       stage.innerHTML = `<svg class="lr-svg" viewBox="0 0 800 290" role="img" aria-label="A trade's stop rising behind the price">
         <line x1="${X0}" x2="${X1}" y1="${Y(TGT)}" y2="${Y(TGT)}" class="ln good" stroke-dasharray="6 5"/><text x="${X1 + 8}" y="${Y(TGT) + 4}" class="tx good">target +${TGT}R</text>
         <line x1="${X0}" x2="${X1}" y1="${Y(0)}" y2="${Y(0)}" class="ln dim"/><text x="${X1 + 8}" y="${Y(0) + 4}" class="tx">entry</text>
         <line x1="${X0}" x2="${X1}" y1="${Y(-1)}" y2="${Y(-1)}" class="ln bad" stroke-dasharray="6 5"/><text x="${X1 + 8}" y="${Y(-1) + 4}" class="tx bad">first stop −1R</text>
         <line x1="${X0}" x2="${X1}" y1="${Y(E.be)}" y2="${Y(E.be)}" class="ln faint" stroke-dasharray="2 5"/><text x="${X0 - 6}" y="${Y(E.be) + 4}" class="tx num" text-anchor="end">+${fmt(E.be, 1)}R</text>
         <polygon class="kept" points=""/>
         <polyline class="ln faint ghost" points=""/>
         <polyline class="ln sky path" points=""/>
         <polyline class="ln wait stopln" points=""/>
         <g class="peak" opacity="0"><circle r="4" class="fl text"/><text y="-10" class="tx c">best</text></g>
         <g class="exitm" opacity="0"><circle r="7" class="fl good"/><text x="12" y="-10" class="tx v-exit"></text></g>
         <text x="${X1 + 8}" y="${Y(1.2)}" class="tx v-now"></text><text x="${X1 + 8}" y="${Y(1.2) + 20}" class="tx v-now2"></text></svg>
         <div class="sim-pre"><button class="btn small" data-a="new">Try another path</button>
           <button class="btn small on" data-a="lock">Profit lock: on</button><button class="btn small" data-a="replay">Replay</button></div>
         <div class="lr-rlist v-cmp"></div>
         <p class="lr-note">Levels in R from your settings; the path is an example. On a very small trade the money minimums in Settings can apply instead, and the lock is never set below ${fmt(E.costMult, 1)}× the trade's own cost of closing.</p>`;
       const svg = stage.querySelector("svg"), q = s => stage.querySelector(s);
       let seed = null, lockOn = true, path = scripted(rng(9)), prog = 0, stopA = () => {}, touched = false;
       const pts = (arr, a, b) => arr.slice(a, b + 1).map((r, j) => `${X(a + j).toFixed(1)},${Y(r).toFixed(1)}`).join(" ");
       const say = o => !o ? "still open" : o.why === "target" ? `target hit: <b>+${fmt(TGT, 2)}R</b>` : `${o.why === "lock" ? "the lock closed it" : o.why === "be" ? "closed at break even" : "the first stop was hit"}: <b>${sgn(o.r, 2)}R</b>`;
       function draw() {
         const sim = simulate(path, lockOn ? "lock" : "be"), o = sim.out, k = Math.round(prog * N), end = o ? Math.min(k, o.i) : k;
         q(".path").setAttribute("points", pts(path, 0, Math.max(1, end)));
         q(".ghost").setAttribute("points", o && k > o.i ? pts(path, o.i, k) : "");
         q(".stopln").setAttribute("points", sim.stops.slice(0, end + 1).map((s, i) => `${X(i).toFixed(1)},${Y(s).toFixed(1)}`).join(" "));
         const kept = sim.stops.slice(0, end + 1).map((s, i) => [i, Math.max(0, s)]);
         q(".kept").setAttribute("points", kept.map(([i, s]) => `${X(i).toFixed(1)},${Y(s).toFixed(1)}`).join(" ") + ` ${X(end).toFixed(1)},${Y(0)} ${X0},${Y(0)}`);
         let pk = 0, pi = 0; for (let i = 0; i <= end; i++) if (path[i] > pk) { pk = path[i]; pi = i; }
         const pg = q(".peak"); pg.setAttribute("opacity", pk >= E.be ? 1 : 0); pg.setAttribute("transform", `translate(${X(pi).toFixed(1)},${Y(pk).toFixed(1)})`);
         pg.querySelector("text").textContent = `best +${fmt(pk, 2)}R`;
         const done = o && k >= o.i, em = q(".exitm");
         em.setAttribute("opacity", done ? 1 : 0);
         if (o) { em.setAttribute("transform", `translate(${X(o.i).toFixed(1)},${Y(o.r).toFixed(1)})`); em.querySelector("circle").setAttribute("class", "fl " + (o.r > 0 ? "good" : o.r < -0.5 ? "bad" : "text"));
           q(".v-exit").textContent = `closed ${sgn(o.r, 2)}R`; }
         const st = sim.stops[end];
         q(".v-now").textContent = done ? "closed" : `stop now ${sgn(st, 2)}R`;
         q(".v-now2").textContent = done ? "" : st > BE ? `${sgn(st, 2)}R is kept` : st >= 0 ? "cannot lose now" : "can still lose 1R";
         q(".v-now2").setAttribute("class", "tx " + (done || st >= 0 ? "good" : "bad") + " v-now2");
       }
       function compare(show) {
         if (!show) { q(".v-cmp").innerHTML = ""; return; }
         const rows = [["With the profit lock", simulate(path, "lock").out], ["Break even only", simulate(path, "be").out], ["Stop never moved", simulate(path, "none").out]];
         q(".v-cmp").innerHTML = rows.map(([n, o]) => `<div class="lr-r ${!o ? "" : o.r > 0.05 ? "good" : o.r < -0.05 ? "bad" : "wait"}"><span>${n}</span><i style="width:0"></i><span>${say(o)}</span></div>`).join("");
       }
       const play = () => { compare(false); stopA(); stopA = VZ.tween(0, 1, 5000, v => { prog = v; draw(); }, () => compare(true)); };
       stage.addEventListener("click", e => {
         const b = e.target.closest("button"); if (!b) return; touched = true; api.pause();
         if (b.dataset.a === "new") { seed = (seed || 20) + 1; path = walk(rng(seed)); }
         if (b.dataset.a === "lock") { lockOn = !lockOn; b.textContent = `Profit lock: ${lockOn ? "on" : "off"}`; b.classList.toggle("on", lockOn); }
         play();
       });
       draw();
       return {tick(t) {
         if (touched) return;
         prog = REDUCED ? (t >= 33 ? 1 : t >= 21 ? 0.7 : t >= 10 ? 0.4 : 0.1) : clamp((t - 2) / 36, 0, 1);
         draw(); compare(t >= 43);
       }, destroy() { stopA(); }};
     }},

    // ---------------------------------------------------------------- deeper 3
    {short: "Market mood", title: "Reading the market's mood", secs: 48,
     beats: [
       [0, () => `The same rule can do well in one kind of market and badly in another. So every hour the bot labels each market's mood — its ${defn("REGIME", "shape")}.`],
       [10, () => `<b>Trending</b>: the 50-hour average is above the 200-hour one (or below, for down), price is on the same side, and the trend-strength gauge is 22 or more.`],
       [19, () => `<b>Sideways</b>: trend strength 18 or less and a flat average. Anything in between it calls <b>changing its mind</b> — which, honestly, is most of the time between trends.`],
       [29, () => `A new mood must hold for 2 hours in a row before it counts, so one odd candle cannot flip it. And when price swings far more than usual, the market is <b>stressed</b> and new trades wait.`],
       [40, L => (L.mood.label ? `Right now ${escH(L.mood.sym)} reads <b>${MOOD[L.mood.label] ? MOOD[L.mood.label][0] : escH(L.mood.label)}</b>${L.mood.adx != null ? `, trend strength ${fmt(L.mood.adx, 0)}` : ""}. ` : `The bot has not labelled a market yet: it needs about 200 hours of history first. `) + `<b>Drag along the chart</b> to see any hour again.`],
     ],
     mount(stage, L, api) {
       // An example market built to pass through every mood, labelled by the same rule as
       // app/regime/detector.py (RegimeParams defaults: EMA 50/200, ADX 22/18, slope 0.02, 2 bars to confirm, stress at
       // the 98th percentile of the typical move). Nothing here is painted on: the labels come out of the arithmetic.
       const r0 = rng(11), N = 560, h = [], l = [], c = [];
       const phases = [[200, 0, 1.15], [280, 0.2, 0.85], [385, 0, 0.75], [480, -0.2, 0.85], [490, 0, 4], [560, 0, 0.9]];
       let p = 100, ph = -1, anchor = p;
       for (let i = 0; i < N; i++) {
         if (ph < 0 || i >= phases[ph][0]) { ph++; anchor = p; }
         const [, drift, vol] = phases[ph], nx = p + drift + (r0() - 0.5) * vol + (drift === 0 ? (anchor - p) * 0.08 : 0);
         h.push(Math.max(p, nx) + r0() * 0.3 * vol); l.push(Math.min(p, nx) - r0() * 0.3 * vol); c.push(nx); p = nx;
       }
       const ema = (a, n) => { const k = 2 / (n + 1), o = []; a.forEach((v, i) => o.push(i ? o[i - 1] + k * (v - o[i - 1]) : v)); return o; };
       const wilder = (a, n) => { const o = Array(a.length).fill(NaN); let s = 0;
         for (let i = 0; i < a.length; i++) { if (i < n) { s += a[i]; if (i === n - 1) o[i] = s / n; } else o[i] = o[i - 1] + (a[i] - o[i - 1]) / n; } return o; };
       const tr = [h[0] - l[0]], pdm = [0], mdm = [0];
       for (let i = 1; i < N; i++) { tr.push(Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])));
         const up = h[i] - h[i - 1], dn = l[i - 1] - l[i]; pdm.push(up > dn && up > 0 ? up : 0); mdm.push(dn > up && dn > 0 ? dn : 0); }
       const atr = wilder(tr, 14), sp = wilder(pdm, 14), sm = wilder(mdm, 14);
       const pdi = sp.map((v, i) => 100 * v / atr[i]), mdi = sm.map((v, i) => 100 * v / atr[i]);
       const adx = Array(14).fill(NaN).concat(wilder(pdi.map((v, i) => 100 * Math.abs(v - mdi[i]) / (v + mdi[i])).slice(14), 14));
       const e50 = ema(c, 50), e200 = ema(c, 200), slope = e50.map((v, i) => i >= 10 ? (v - e50[i - 10]) / (10 * atr[i]) : NaN);
       const lab = []; let cur = "UNKNOWN", cand = null, cnt = 0;
       for (let k = 0; k < N; k++) {
         let raw = "TRANSITION";
         if (isNaN(adx[k]) || isNaN(slope[k])) raw = "UNKNOWN";
         else if (adx[k] >= 22 && e50[k] > e200[k] && c[k] > e50[k] && slope[k] > 0.02 && pdi[k] > mdi[k]) raw = "TREND_UP";
         else if (adx[k] >= 22 && e50[k] < e200[k] && c[k] < e50[k] && slope[k] < -0.02 && mdi[k] > pdi[k]) raw = "TREND_DOWN";
         else if (adx[k] <= 18 && Math.abs(slope[k]) <= 0.03) raw = "RANGE";
         if (cur === "UNKNOWN" || raw === "UNKNOWN" || raw === cur) { cur = raw; cand = null; cnt = 0; }
         else { if (raw === cand) cnt++; else { cand = raw; cnt = 1; } if (cnt >= 2) { cur = raw; cand = null; cnt = 0; } }
         const win = atr.slice(Math.max(14, k - 250), k + 1), pct = k >= 64 ? win.filter(x => x <= atr[k]).length / win.length : 0;
         lab.push(pct >= 0.98 ? "STRESS" : cur);
       }
       const S0 = 200, M = N - S0, X0 = 20, X1 = 590, X = i => X0 + i / (M - 1) * (X1 - X0);
       const seg = c.slice(S0).concat(e50.slice(S0), e200.slice(S0)), pLo = Math.min(...seg), pHi = Math.max(...seg);
       const Yp = v => 20 + (pHi - v) / (pHi - pLo) * 170, Ya = v => 306 - clamp(v, 0, 90) / 90 * 66;
       const runs = []; lab.slice(S0).forEach((v, i) => { const last = runs[runs.length - 1]; if (last && last[0] === v) last[2] = i; else runs.push([v, i, i]); });
       stage.innerHTML = `<svg class="lr-svg" viewBox="0 0 800 316" role="img" aria-label="An example market with its mood labelled hour by hour">
         <polyline class="ln dim thin v-c" points=""/><polyline class="ln sky thin v-e50" points=""/><polyline class="ln iris thin v-e200" points=""/>
         <g class="v-rib"></g>
         <text x="${X0}" y="232" class="tx mut">trend strength (ADX)</text>
         <rect x="${X0}" y="${Ya(90)}" width="${X1 - X0}" height="${Ya(22) - Ya(90)}" class="zone good"/>
         <rect x="${X0}" y="${Ya(18)}" width="${X1 - X0}" height="${Ya(0) - Ya(18)}" class="adxflat"/>
         <text x="${X1 + 4}" y="${Ya(22) - 3}" class="tx num good">22 trend</text><text x="${X1 + 4}" y="${Ya(18) + 11}" class="tx num sky">18 flat</text>
         <polyline class="ln text thin v-adx" points=""/>
         <line class="ln text thin v-ph" y1="16" y2="308" x1="0" x2="0"/>
         <rect x="0" y="12" width="${X1 - X0}" height="298" class="v-hit" fill="transparent" style="cursor:ew-resize;touch-action:none"/>
         <text x="612" y="40" class="tx mut">this hour</text><text x="612" y="68" class="tx big v-mood"></text>
         <text x="612" y="94" class="tx v-for"></text>
         <text x="612" y="130" class="tx num v-r1"></text><text x="612" y="150" class="tx num v-r2"></text><text x="612" y="170" class="tx num v-r3"></text>
         </svg>
         <div class="moodkey"><span><i class="ln50"></i>50-hour average</span><span><i class="ln200"></i>200-hour average</span>
           ${["TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION", "STRESS"].map(k => `<span><i class="rib ${MOOD[k][1]}"></i>${MOOD[k][0]}</span>`).join("")}</div>
         <div class="lr-lab">Rules allowed to open trades in this mood</div><div class="moodrules"></div>
         <p class="lr-note">The market is an example; its moods are worked out with the bot's own rule. Each line of the chart is one hour.</p>`;
       const svg = stage.querySelector("svg"), q = s => stage.querySelector(s);
       const line = (arr, k, Yf) => arr.slice(S0, S0 + k + 1).map((v, i) => isNaN(v) ? "" : `${X(i).toFixed(1)},${Yf(v).toFixed(1)}`).join(" ");
       const rules = L.rules.filter(r => r.enabled);
       let touched = false, shown = -1;
       const set = k => {
         k = Math.round(clamp(k, 0, M - 1)); if (k === shown) return; shown = k;
         const g = S0 + k, m = lab[g];
         q(".v-c").setAttribute("points", line(c, k, Yp)); q(".v-e50").setAttribute("points", line(e50, k, Yp));
         q(".v-e200").setAttribute("points", line(e200, k, Yp)); q(".v-adx").setAttribute("points", line(adx, k, Ya));
         q(".v-rib").innerHTML = runs.filter(r => r[1] <= k).map(([v, a, b]) => `<rect x="${X(a).toFixed(1)}" y="202" width="${Math.max(1.5, X(Math.min(b, k) + 1) - X(a)).toFixed(1)}" height="14" class="rib ${MOOD[v][1]}"/>`).join("");
         q(".v-ph").setAttribute("x1", X(k)); q(".v-ph").setAttribute("x2", X(k));
         const mt = q(".v-mood"); mt.textContent = MOOD[m][0]; mt.setAttribute("class", "tx big v-mood mood-" + MOOD[m][1]);
         let since = k; while (since > 0 && lab[S0 + since - 1] === m) since--;
         q(".v-for").textContent = `for ${k - since + 1} hour${k - since ? "s" : ""}`;
         q(".v-r1").textContent = `trend strength ${fmt(adx[g], 0)}`;
         q(".v-r2").textContent = `50-hour avg ${e50[g] > e200[g] ? "above" : "below"} 200-hour`;
         q(".v-r3").textContent = `slope ${sgn(slope[g], 2)} per hour`;
         q(".moodrules").innerHTML = rules.length ? rules.map(r => {
           const ok = m !== "STRESS" && m !== "UNKNOWN" && (!r.regimes.length || r.regimes.includes(m));
           const which = r.regimes.length ? r.regimes.map(x => MOOD[x] ? MOOD[x][0] : x).join(" or ") + " only" : "any mood";
           return `<span class="mrule ${ok ? "on" : ""}">${escH(ruleName(r.id))} <span class="mut">· ${escH(which)}</span></span>`; }).join("")
           : `<span class="mut">No rules are switched on in the settings.</span>`;
       };
       const fromEvent = e => { touched = true; api.pause(); set((svgPoint(svg, e).x - X0) / (X1 - X0) * (M - 1)); };
       let dragging = false;
       q(".v-hit").addEventListener("pointerdown", e => { dragging = true; svg.setPointerCapture(e.pointerId); fromEvent(e); });
       svg.addEventListener("pointermove", e => { if (dragging) fromEvent(e); });
       svg.addEventListener("pointerup", () => { dragging = false; });
       // the playhead is timed to the captions: up-trend, the drift into sideways, the fall and its shock
       const keys = [[0, 0], [2, 0], [10, 90], [19, 140], [24, 172], [29, 222], [34, 286], [39, M - 1]];
       const at = t => { let i = 1; while (i < keys.length - 1 && keys[i][0] < t) i++; const [ta, ka] = keys[i - 1], [tb, kb] = keys[i]; return lerp(ka, kb, (t - ta) / (tb - ta)); };
       set(0);
       return {tick(t) { if (!touched) set(REDUCED ? at(Math.min(t, 39)) : at(t)); }};
     }},

    // ---------------------------------------------------------------- deeper 4
    {short: "News", title: "When the news hits", secs: 52,
     beats: [
       [0, () => `A few times a week a big announcement — an interest-rate decision, inflation, jobs figures — can move a market more in one minute than in a whole day.`],
       [10, L => `So the ordinary rules step aside: from <b>${L.news.pre} minutes before</b> a high-impact release until <b>${L.news.during} minutes after</b> it, they open nothing new. Trades already open keep their stop and target.`],
       [20, () => `Only markets priced in, or made of, that currency pause. A US release pauses almost everything here, since most of it is priced in dollars; a UK one pauses only the pound pairs. <b>Tap a currency</b> to see.`],
       [31, L => L.news.straddle.on
         ? `One rule is built for exactly this moment: the ${defn("STRADDLE", "news straddle")}. ${fmt(L.news.straddle.arm, 0)} minutes before the release it places a buy order above the price and a sell order below. Whichever way the price breaks, that order fills — and the other is cancelled at once.`
         : `The news straddle — a rule that trades the release itself — is switched off in your settings, so the bot simply waits the news out.`],
       [42, L => `${L.news.straddle.on ? `If neither side fills within ${fmt(L.news.straddle.expire, 0)} minutes of the release, both orders are withdrawn. ` : ""}${L.news.nextText}`],
     ],
     mount(stage, L, api) {
       const NW = L.news, syms = (L.symbols.length ? L.symbols : ["EURUSDm", "GBPUSDm", "GBPJPYm", "XAUUSDm", "BTCUSDm", "NVDAm", "US500m"]).slice(0, 12);
       const ccys = NW.currencies.length ? NW.currencies : ["USD", "EUR", "GBP", "JPY"];
       const X0 = 60, X1 = 740, Xm = m => X0 + (m + 60) / 120 * (X1 - X0);
       const cols = Math.min(6, syms.length), cw = (X1 - X0) / cols;
       // the straddle, in units of the market's typical move (ATR), from news_straddle in the settings
       const SD = NW.straddle, Yb = a => 140 - a * 42, Xs = m => X0 + (m + 10) / 20 * (X1 - X0);
       stage.innerHTML = `<svg class="lr-svg" viewBox="0 0 800 290" role="img" aria-label="The no-new-trades window around a news release, and the news straddle">
         <g class="v-a">
           <line x1="${X0}" x2="${X1}" y1="70" y2="70" class="ln faint"/>
           <rect x="${Xm(-NW.pre)}" y="52" width="${Xm(NW.during) - Xm(-NW.pre)}" height="36" class="zone bad"/>
           <text x="${(Xm(-NW.pre) + Xm(NW.during)) / 2}" y="44" class="tx c bad">no new trades</text>
           ${SD.on ? `<rect x="${Xm(-SD.arm)}" y="92" width="${Xm(SD.expire) - Xm(-SD.arm)}" height="6" rx="3" class="fl sky"/><text x="${Xm(SD.expire) + 6}" y="99" class="tx num">straddle</text>` : ""}
           ${[-60, -30, 0, 30, 60].map(m => `<text x="${Xm(m)}" y="116" class="tx num c">${m ? (m > 0 ? "+" : "−") + Math.abs(m) + " min" : "release"}</text>`).join("")}
           <line x1="${Xm(0)}" x2="${Xm(0)}" y1="50" y2="92" class="ln text"/>
           <g class="v-clock"><line y1="40" y2="100" class="ln wait"/><text y="30" class="tx c wait v-ct"></text></g>
           <g class="v-mk">${syms.map((s, i) => `<g class="nmk" data-s="${escH(s)}" transform="translate(${X0 + (i % cols) * cw + cw / 2},${150 + Math.floor(i / cols) * 58})">
             <rect x="${-cw / 2 + 6}" y="-18" width="${cw - 12}" height="44" rx="9"/><text y="0" class="tx c">${escH(s)}</text><text y="17" class="tx c num v-st"></text></g>`).join("")}</g>
         </g>
         <g class="v-b" opacity="0">
           <line x1="${Xs(0)}" x2="${Xs(0)}" y1="14" y2="270" class="ln faint"/><text x="${Xs(0)}" y="284" class="tx c num">release</text>
           <text x="${X0}" y="284" class="tx num">−10 min</text><text x="${X1}" y="284" class="tx num" text-anchor="end">+10 min</text>
           <g class="leg buy"><line x1="${Xs(-SD.arm)}" x2="${X1}" y1="${Yb(SD.entry)}" y2="${Yb(SD.entry)}" class="ln good" stroke-dasharray="7 5"/><text x="${Xs(-SD.arm)}" y="${Yb(SD.entry) - 8}" class="tx good v-lb">buy order, waiting</text></g>
           <g class="leg sell"><line x1="${Xs(-SD.arm)}" x2="${X1}" y1="${Yb(-SD.entry)}" y2="${Yb(-SD.entry)}" class="ln bad" stroke-dasharray="7 5"/><text x="${Xs(-SD.arm)}" y="${Yb(-SD.entry) + 18}" class="tx bad v-ls">sell order, waiting</text></g>
           <g class="v-prot" opacity="0"><line x1="0" x2="${X1}" class="ln good thin v-tp" stroke-dasharray="2 4"/><line x1="0" x2="${X1}" class="ln bad thin v-sl" stroke-dasharray="2 4"/>
             <text x="${X1}" class="tx num good v-tpt" text-anchor="end">target</text><text x="${X1}" class="tx num bad v-slt" text-anchor="end">stop</text></g>
           <polyline class="ln sky v-px" points=""/>
           <text x="${X0}" y="258" class="tx v-res"></text>
         </g></svg>
         <div class="sim-pre v-ccy"><span class="mut">Whose news?</span>${ccys.map(x => `<button class="btn small" data-c="${escH(x)}">${escH(x)}</button>`).join("")}</div>
         <div class="sim-pre v-sc" hidden><span class="mut">Replay the release:</span><button class="btn small" data-sc="up">Breaks up</button><button class="btn small" data-sc="down">Breaks down</button><button class="btn small" data-sc="flat">Goes nowhere</button></div>
         <p class="lr-note">${SD.on ? `Straddle distances from your settings: orders ${fmt(SD.entry, 1)}× the typical move from the price, stop ${fmt(SD.sl, 1)}×, target ${fmt(SD.tp, 1)}×. The price path is an example.` : "The market reactions are an example."}</p>`;
       const q = s => stage.querySelector(s), svg = stage.querySelector("svg");
       let cur = "USD", touched = false, scen = "up", sProg = 0, stopA = () => {};
       const setClock = m => {
         q(".v-clock").setAttribute("transform", `translate(${Xm(m).toFixed(1)},0)`);
         q(".v-ct").textContent = m < 0 ? `T−${Math.ceil(-m)} min` : `T+${Math.floor(m)} min`;
         const blocked = m >= -NW.pre && m < NW.during;
         svg.querySelectorAll(".nmk").forEach(g => {
           const hit = ccyOf(g.dataset.s).includes(cur), st = !hit ? "normal" : blocked ? "paused" : "watching";
           g.setAttribute("class", "nmk " + st); g.querySelector(".v-st").textContent = !hit ? "not affected" : blocked ? "paused" : `moves on ${cur}`;
         });
       };
       const pickC = c => { cur = c; stage.querySelectorAll("[data-c]").forEach(b => b.classList.toggle("on", b.dataset.c === c)); };
       // price path in ATR units over -10..+10 minutes
       const pathFor = sc => { const r0 = rng(sc === "up" ? 3 : sc === "down" ? 5 : 8);
         return Array.from({length: 201}, (_, i) => { const m = -10 + i / 10, n = (r0() - 0.5) * 0.12;
           if (m < 0) return Math.sin(i * 0.7) * 0.08 + n;
           // the break carries a little past the target, then settles back - the trade is already closed by then
           const reach = SD.entry + SD.tp + 0.25, jump = sc === "flat" ? 0.28 * Math.sin(m * 2.1)
             : (sc === "up" ? 1 : -1) * reach * (1 - Math.exp(-m * 1.6)) * (1 - 0.04 * Math.max(0, m - 3));
           return jump + n * 2; }); };
       const top = SD.entry + SD.tp + 0.6;
       const drawB = () => {
         const px = pathFor(scen), k = Math.round(sProg * 200), m = -10 + k / 10;
         q(".v-px").setAttribute("points", px.slice(0, k + 1).map((v, i) => `${Xs(-10 + i / 10).toFixed(1)},${Yb(clamp(v, -top, top)).toFixed(1)}`).join(" "));
         const armed = m >= -SD.arm;
         let fill = null; for (let i = 0; i <= k; i++) { const mm = -10 + i / 10; if (mm < -SD.arm) continue;
           if (px[i] >= SD.entry) { fill = {side: "buy", i}; break; } if (px[i] <= -SD.entry) { fill = {side: "sell", i}; break; } }
         const expired = !fill && m >= SD.expire;
         const leg = (cls, side) => { const g = q(".leg." + cls), filled = fill && fill.side === side, other = fill && fill.side !== side;
           g.setAttribute("opacity", !armed || expired || other ? 0.12 : 1);
           q(side === "buy" ? ".v-lb" : ".v-ls").textContent = filled ? `${side} order filled` : other ? "cancelled" : expired ? "withdrawn" : `${side} order, waiting`; };
         leg("buy", "buy"); leg("sell", "sell");
         const pr = q(".v-prot"); pr.setAttribute("opacity", fill ? 1 : 0);
         let res = expired ? "neither filled: both withdrawn, nothing traded" : "";
         if (fill) {
           const s = fill.side === "buy" ? 1 : -1, en = s * SD.entry, tp = en + s * SD.tp, sl = en - s * SD.sl, x = Xs(-10 + fill.i / 10);
           [[".v-tp", tp], [".v-sl", sl]].forEach(([c, v]) => { const e = q(c); e.setAttribute("x1", x); e.setAttribute("y1", Yb(v)); e.setAttribute("y2", Yb(v)); });
           q(".v-tpt").setAttribute("y", Yb(tp) - 5); q(".v-slt").setAttribute("y", Yb(sl) + 14);
           const hitTp = px.slice(fill.i, k + 1).some(v => s * v >= s * tp);
           res = hitTp ? `${fill.side} side filled, the other cancelled — target reached` : `${fill.side} side filled, the other cancelled`;
         }
         q(".v-res").textContent = res; q(".v-res").setAttribute("class", "tx v-res " + (fill ? "good" : ""));
       };
       const partB = on => { q(".v-a").setAttribute("opacity", on ? 0 : 1); q(".v-a").style.pointerEvents = on ? "none" : ""; q(".v-b").setAttribute("opacity", on ? 1 : 0);
         q(".v-ccy").hidden = on; q(".v-sc").hidden = !on || !SD.on; };
       stage.addEventListener("click", e => {
         const b = e.target.closest("button"); if (!b) return; touched = true; api.pause();
         if (b.dataset.c) { pickC(b.dataset.c); setClock(-NW.pre / 2); }
         if (b.dataset.sc) { scen = b.dataset.sc; stopA(); stopA = VZ.tween(0, 1, 4500, v => { sProg = v; drawB(); }); }
       });
       pickC("USD"); setClock(-60); drawB();
       return {tick(t) {
         if (touched) return;
         if (t < 31 || !SD.on) {
           partB(false);
           if (t >= 20 && t < 31) pickC(t < 25.5 ? "GBP" : "USD");
           setClock(REDUCED ? (t >= 10 ? -NW.pre / 2 : -45) : t < 2 ? -60 : t < 30 ? lerp(-60, 60, (t - 2) / 28) : 60);
         } else {
           partB(true); scen = t < 42 ? "up" : "flat";
           sProg = REDUCED ? 1 : t < 42 ? clamp((t - 32) / 9, 0, 1) : clamp((t - 42.5) / 8, 0, 1);
           drawB();
         }
       }, destroy() { stopA(); }};
     }},

    // ---------------------------------------------------------------- deeper 5
    {short: "One big bet?", title: "Not one big bet", secs: 46,
     beats: [
       [0, () => `Each trade risks only a small slice. But several trades on markets that move together are really one big bet.`],
       [10, L => `So all open trades together may risk at most <b>${pc(L.size.total)}</b> of the account${L.size.live ? ` — about ${money(L, L.size.equity * L.size.total, 0)} right now` : ""}, and no more than <b>${L.size.maxPos}</b> can be open at once.`],
       [20, L => `Markets that have moved together over the last ${L.size.lookback} days — a ${defn("CORRELATION", "correlation")} of ${fmt(L.size.corrThr, 1)} or more — share a smaller budget: ${pc(L.size.groupFrac)} of that, ${pc(L.size.total * L.size.groupFrac, 1)} of the account. A market it cannot measure yet counts as moving together, to be safe.`],
       [32, L => `Here every trade risks the full ${pc(L.size.cap, 1)} ceiling. <b>Tap a market</b> to open a trade on it and watch the limits fill. The lines join markets that move together in this example.`],
     ],
     mount(stage, L, api) {
       const Z = L.size, per = Z.cap, total = Z.total, group = Z.total * Z.groupFrac;
       // example correlations: the bot measures its own every hour from 30 days of prices
       const MK = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD", "ETHUSD", "NVDA", "MSFT", "US500"];
       const GROUPS = [["EURUSD", "GBPUSD"], ["BTCUSD", "ETHUSD"], ["NVDA", "MSFT", "US500"]];
       const corr = (a, b) => a === b ? 1 : GROUPS.some(g => g.includes(a) && g.includes(b)) ? 0.8 : 0.1;
       const cx = 220, cy = 160, P = MK.map((m, i) => { const a = -Math.PI / 2 + i * 2 * Math.PI / MK.length; return [cx + 175 * Math.cos(a), cy + 120 * Math.sin(a)]; });
       const links = []; MK.forEach((a, i) => MK.forEach((b, j) => { if (j > i && corr(a, b) >= Z.corrThr) links.push([i, j]); }));
       stage.innerHTML = `<svg class="lr-svg" viewBox="0 0 800 320" role="img" aria-label="Markets, which ones move together, and how much of the risk limits open trades use">
         ${links.map(([i, j]) => `<line x1="${P[i][0]}" y1="${P[i][1]}" x2="${P[j][0]}" y2="${P[j][1]}" class="ln wait thin"/>
           <text x="${(P[i][0] + P[j][0]) / 2}" y="${(P[i][1] + P[j][1]) / 2 - 5}" class="tx num c wait">0.8</text>`).join("")}
         ${MK.map((m, i) => `<g class="pmk" data-i="${i}" tabindex="0" role="button" aria-label="Open a trade on ${m}" transform="translate(${P[i][0]},${P[i][1]})">
           <circle r="30"/><text y="-2" class="tx c">${m}</text><text y="14" class="tx c num v-n"></text></g>`).join("")}
         <text x="470" y="34" class="tx">all open trades together</text>
         <rect x="470" y="44" width="300" height="22" rx="6" class="barbg"/><rect x="470" y="44" width="0" height="22" rx="6" class="sizebar good v-tot"/>
         <text x="470" y="86" class="tx num v-tott"></text>
         <text x="470" y="126" class="tx v-gl">moving together</text>
         <rect x="470" y="136" width="300" height="22" rx="6" class="barbg"/><rect x="470" y="136" width="0" height="22" rx="6" class="sizebar good v-grp"/>
         <text x="470" y="178" class="tx num v-grpt"></text>
         <text x="470" y="218" class="tx">trades open</text><g class="v-slots"></g></svg>
         <div class="lr-readout v-msg" aria-live="polite">Tap a market to open a trade on it.</div>
         <div class="sim-pre"><button class="btn small" data-a="clear">Close them all</button></div>
         <p class="lr-note">The correlations are an example. The limits are your settings: each trade at ${pc(per, 1)}, ${pc(total)} for all open trades, ${pc(group, 1)} for markets that move together, at most ${Z.maxPos} trades.</p>`;
       const q = s => stage.querySelector(s), svg = stage.querySelector("svg");
       let open = MK.map(() => 0), last = 0, touched = false;
       const usedTotal = () => open.reduce((a, n) => a + n * per, 0);
       const usedGroup = i => open.reduce((a, n, j) => a + (corr(MK[i], MK[j]) >= Z.corrThr ? n * per : 0), 0);
       const draw = () => {
         const tt = usedTotal(), gg = usedGroup(last), n = open.reduce((a, b) => a + b, 0);
         svg.querySelectorAll(".pmk").forEach((g, i) => { g.querySelector(".v-n").textContent = open[i] ? `${open[i]} open` : ""; g.classList.toggle("on", open[i] > 0); });
         q(".v-tot").setAttribute("width", 300 * clamp(tt / total, 0, 1)); q(".v-tot").setAttribute("class", "sizebar v-tot " + (tt >= total - 1e-9 ? "wait" : "good"));
         q(".v-tott").textContent = `${pc(tt, 1)} of the account · limit ${pc(total)}`;
         q(".v-gl").textContent = `moving together with ${MK[last]}`;
         q(".v-grp").setAttribute("width", 300 * clamp(gg / group, 0, 1)); q(".v-grp").setAttribute("class", "sizebar v-grp " + (gg >= group - 1e-9 ? "wait" : "good"));
         q(".v-grpt").textContent = `${pc(gg, 1)} of the account · limit ${pc(group, 1)}`;
         q(".v-slots").innerHTML = Array.from({length: Z.maxPos}, (_, i) => `<rect x="${470 + i * 30}" y="228" width="24" height="24" rx="5" class="${i < n ? "sizebar good" : "barbg"}"/>`).join("");
       };
       const add = i => {
         last = i;
         const n = open.reduce((a, b) => a + b, 0), m = q(".v-msg");
         const why = n >= Z.maxPos ? `${Z.maxPos} trades are already open — the most allowed at once.`
           : usedTotal() + per > total + 1e-9 ? `All open trades would then risk ${pc(usedTotal() + per, 1)}, over the ${pc(total)} limit.`
           : usedGroup(i) + per > group + 1e-9 ? `${MK[i]} moves with what is already open: together they would risk ${pc(usedGroup(i) + per, 1)}, over the ${pc(group, 1)} limit for markets that move together.` : null;
         if (why) {
           m.className = "lr-readout v-msg bad"; m.innerHTML = `<b>Refused.</b> ${why}`;
           const g = svg.querySelector(`.pmk[data-i="${i}"]`); g.classList.remove("no"); void g.getBoundingClientRect(); g.classList.add("no");
         } else { open[i]++; m.className = "lr-readout v-msg good"; m.innerHTML = `Opened a trade on ${MK[i]}, risking ${pc(per, 1)} of the account.`; }
         draw();
       };
       const pick = e => { const g = e.target.closest(".pmk"); if (!g) return; touched = true; api.pause(); add(+g.dataset.i); };
       svg.addEventListener("click", pick);
       svg.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e); } });
       stage.addEventListener("click", e => { const b = e.target.closest("button"); if (b && b.dataset.a === "clear") { touched = true; api.pause(); open = MK.map(() => 0); q(".v-msg").className = "lr-readout v-msg"; q(".v-msg").textContent = "All closed."; draw(); } });
       draw();
       // the story the autoplay tells: spread out, then double up on the pound/euro pair until it is refused,
       // then fill the account until the overall limit refuses one more
       const script = [[12, "EURUSD"], [14, "XAUUSD"], [16, "BTCUSD"], [22, "GBPUSD"], [24.5, "EURUSD"], [27, "GBPUSD"], [34, "NVDA"], [37, "ETHUSD"], [40, "MSFT"]];
       let done = 0;
       return {tick(t) {
         if (touched) return;
         while (done < script.length && t >= script[done][0]) add(MK.indexOf(script[done++][1]));
       }};
     }},
  ];

  // ================================================================ live data
  async function loadLive() {
    const get = u => fetch(u).then(r => r.ok ? r.json() : null).catch(() => null);
    const [state, cand, cfg, pf] = await Promise.all([get("/api/state"), get("/api/candidates/summary?days=1"),
      get("/api/config"), get("/api/analysis/portfolio?hours=720")]);
    const st = state || {}, a = st.account || {}, m = st.market || {};
    const rc = (cfg && cfg.app && cfg.app.risk) || {};
    const risk = {dd_reduced: rc.dd_reduced ?? 0.1, dd_restricted: rc.dd_restricted ?? 0.15, dd_no_new_trades: rc.dd_no_new_trades ?? 0.2,
      dd_emergency: rc.dd_emergency ?? 0.3, reduced_multiplier: rc.reduced_multiplier ?? 0.5, restricted_multiplier: rc.restricted_multiplier ?? 0.25,
      daily_loss_limit_fraction: rc.daily_loss_limit_fraction ?? 0.05, dd: Math.max(0, +((st.risk || {}).drawdown) || 0)};
    const multAt = dd => dd >= risk.dd_no_new_trades ? 0 : dd >= risk.dd_restricted ? risk.restricted_multiplier : dd >= risk.dd_reduced ? risk.reduced_multiplier : 1;
    const spreadLive = m.ask != null && m.bid != null && m.ask > m.bid ? m.ask - m.bid : null;
    // decimals follow the PRICE, as the broker quotes it: 5 for currencies, 3 for yen pairs and shares, 2 for gold
    const pxd = v => { const x = Math.abs(+v); return !isFinite(x) || !x ? 2 : x < 20 ? 5 : x < 500 ? 3 : 2; };
    const toll = spreadLive ? {sym: st.symbol || "this market", spread: spreadLive, atr: m.atr_m15, d: pxd(m.bid)}
                            : {sym: "an example market", spread: 0.2, atr: 1, d: 2};
    toll.maxFrac = +findKey(cfg && cfg.app, "max_spread_fraction_of_stop") || 0.1;
    let pfSentence = `Your bot has no finished trades in the last 30 days yet, so there is nothing to judge — which is the honest answer.`;
    if (pf && pf.n_trades) {
      const n = pf.n_trades, ci = pf.ci95;
      pfSentence = n < 30
        ? `Your bot so far: <b>${n}</b> finished trade${n === 1 ? "" : "s"} in 30 days at ${sgn(pf.mean_r, 3)}R each — far too few to judge either way. Expect around thirty before the range means much.`
        : `Your bot so far: <b>${n}</b> trades at ${sgn(pf.mean_r, 3)}R each${ci ? `, likely between ${sgn(ci[0], 3)} and ${sgn(ci[1], 3)}R` : ""} — ${ci && ci[0] > 0 ? "the whole range is above zero" : ci && ci[1] < 0 ? "the whole range is below zero" : "the range crosses zero, so it is not shown to make money yet"}.`;
    }
    // ---- going deeper
    const app = (cfg && cfg.app) || {}, ex = app.execution || {}, evc = app.events || {}, ns = app.news_straddle || {};
    const equity = +a.equity || +a.balance || 0;
    const size = {live: equity > 0, equity, research: rc.research_risk_fraction ?? 0.01, cap: rc.max_risk_fraction_per_trade ?? 0.015,
      total: rc.max_total_open_risk_fraction ?? 0.1, maxVol: rc.max_volume ?? 1, maxPos: rc.max_concurrent_positions ?? 10,
      corrThr: rc.correlation_threshold ?? 0.7, groupFrac: rc.correlation_group_fraction ?? 0.5, lookback: rc.correlation_lookback_days ?? 30};
    // breakeven at +0.5R is DEFAULT_BREAKEVEN in app/alpha/exits.py; the lock levels are the execution settings
    const exits = {be: 0.5, smallAct: ex.small_win_activation_r ?? 0.5, smallFloor: ex.small_win_floor_r ?? 0.1,
      ambAct: ex.ambition_activation_r ?? 0.8, ambFloor: ex.ambition_floor_r ?? 0.4, give: ex.profit_giveback_fraction ?? 0,
      costMult: ex.profit_lock_cost_multiple ?? 1.5};
    const nxt = m.next_event;
    let nextText = `The calendar has no upcoming high-impact release loaded right now.`;
    if (nxt && nxt.time) {
      const when = new Date(nxt.time), mins = Math.round((when - Date.now()) / 60000);
      const rel = mins < 0 ? "now" : mins < 60 ? `in ${mins} min` : mins < 48 * 60 ? `in ${Math.floor(mins / 60)} h ${mins % 60} min` : `in ${Math.round(mins / 1440)} days`;
      nextText = `Next on this bot's calendar: <b>${escH(nxt.title || "a release")}</b>, ${escH(when.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"}))} (${rel}).`;
    }
    const news = {pre: evc.pre_minutes ?? 30, during: evc.event_minutes ?? 15, currencies: evc.currencies || [], nextText,
      straddle: {on: !!ns.enabled, arm: ns.arm_minutes_before ?? 5, expire: ns.expire_after_event_minutes ?? 5,
                 entry: ns.entry_distance_atr ?? 0.5, sl: ns.sl_atr ?? 1, tp: ns.tp_atr ?? 2}};
    const mood = {sym: st.symbol || "the main market", label: m.regime || null,
      adx: (((m.regime_detail || {}).diagnostics) || {}).adx ?? null};
    const sl = cfg && cfg.strategies, list = Array.isArray(sl) ? sl : (sl && sl.strategies) || [];
    const rules = list.map(s => ({id: s.id, enabled: s.enabled !== false, regimes: s.allowed_regimes || []}));
    return {state: st, cand, cfg, pf, env: st.environment || "DEMO", balance: a.balance, ccy: a.currency || "USD",
            symbols: Object.keys(st.symbol_trading || {}), risk, multAt, toll, pfSentence, size, exits, news, mood, rules,
            funnel: typeof funnelModel === "function" ? funnelModel(cand || {})          // missing only on an old saved page
              : {steps: [], reasons: {}, looked: 0, on: 0, matched: 0, traded: 0}};
  }

  // ================================================================ the player
  const SERIES = {
    basics: {scenes: SCENES, label: "The basics", store: "qa_learn_at", aria: "How Quantara works — a five-minute walkthrough"},
    deeper: {scenes: DEEPER, label: "Going deeper", store: "qa_deeper_at", aria: "Going deeper — how Quantara sizes, protects and limits its trades"},
  };
  let series = "basics";
  const S = () => SERIES[series].scenes;
  let root = null, mode = "overlay", idx = 0, playing = true, elapsed = 0, lastTs = 0, raf = 0, scene = null, live = null, cap = -1;
  let els = {};
  const api = {
    get inline() { return mode === "inline"; },
    pause() { if (playing) { playing = false; syncPlay(); els.hint.hidden = false; } },
    seek(sec) { elapsed = sec; cap = -1; },
    close() { if (mode === "inline") location.href = "/"; else close(); },
    series(k) { switchSeries(k, 0); },
    tour() {
      if (mode === "overlay" && document.body.dataset.page === "overview") { close(); setTimeout(() => HELP.tour("overview"), 250); }
      else location.href = "/#tour";
    },
  };
  function syncPlay() {
    if (!els.play) return;
    els.play.textContent = playing ? "❚❚" : "▶";
    els.play.setAttribute("aria-label", playing ? "Pause" : "Play");
    if (playing) els.hint.hidden = true;
  }
  function build(container) {
    root = document.createElement("div");
    root.className = "learn " + mode;
    if (mode === "overlay") { root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true"); }
    root.setAttribute("aria-label", SERIES[series].aria);
    root.innerHTML = `<div class="lr-panel">
        <div class="lr-series">${Object.entries(SERIES).map(([k, v]) => `<button data-series="${k}" aria-pressed="false">${escH(v.label)} <span class="mut">· ${v.scenes.length} scenes</span></button>`).join("")}</div>
        <div class="lr-top"><span class="lr-n"></span><h2 class="lr-title">Loading…</h2>
          ${mode === "overlay" ? '<button class="lr-x" aria-label="Close the walkthrough">&times;</button>' : ""}</div>
        <div class="lr-stage"><div class="empty">Reading the bot's live numbers…</div></div>
        <p class="lr-cap" aria-live="polite"></p>
        <div class="lr-hint" hidden>Paused while you try it — press ▶ to carry on.</div>
        <div class="lr-prog" aria-hidden="true"><i></i></div>
        <div class="lr-ctrl">
          <button class="lr-b" data-a="prev" aria-label="Previous scene">←</button>
          <button class="lr-b lr-play" data-a="play" aria-label="Pause">❚❚</button>
          <button class="lr-b" data-a="next" aria-label="Next scene">→</button>
          <div class="lr-dots"></div>
        </div></div>`;
    container.appendChild(root);
    // the handlers keep their own reference: a button inside a scene can close the player (and null `root`)
    // before this panel's own click handler runs
    const me = root, q = s => me.querySelector(s);
    els = {n: q(".lr-n"), title: q(".lr-title"), stage: q(".lr-stage"), cap: q(".lr-cap"), prog: q(".lr-prog i"),
           play: q(".lr-play"), hint: q(".lr-hint"), dotbox: q(".lr-dots"), dots: []};
    renderDots();
    const stage = els.stage;
    me.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b || !me.contains(b) || stage.contains(b)) return;
      if (b.classList.contains("lr-x")) close();
      else if (b.dataset.a === "prev") go(idx - 1);
      else if (b.dataset.a === "next") go(idx + 1);
      else if (b.dataset.a === "play") { if (!playing && idx === S().length - 1 && elapsed >= S()[idx].secs) go(0); playing = !playing; syncPlay(); }
      else if (b.dataset.series) switchSeries(b.dataset.series);
      else if (b.dataset.i != null) go(+b.dataset.i);
    });
    // touching anything inside the scene pauses the narration, so it does not move on while you are trying it
    stage.addEventListener("pointerdown", e => { if (e.target.closest("input,button,[data-drag],canvas,.sym,.mm")) api.pause(); });
    // swipe between scenes, anywhere outside the scene itself
    let sx = null;
    me.addEventListener("pointerdown", e => { sx = stage.contains(e.target) ? null : e.clientX; });
    me.addEventListener("pointerup", e => { if (sx == null) return; const dx = e.clientX - sx; sx = null; if (Math.abs(dx) > 60) go(idx + (dx < 0 ? 1 : -1)); });
  }
  function renderDots() {
    els.dotbox.innerHTML = S().map((s, i) => `<button class="lr-dot" data-i="${i}" aria-label="Scene ${i + 1}: ${escH(s.short)}"><span>${i + 1}</span><em>${escH(s.short)}</em></button>`).join("");
    els.dots = [...els.dotbox.querySelectorAll(".lr-dot")];
    root.querySelectorAll("[data-series]").forEach(b => { const on = b.dataset.series === series; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
    root.setAttribute("aria-label", SERIES[series].aria);
  }
  const savedAt = () => { const n = S().length, v = +(store.get(SERIES[series].store) || 0); return v > 0 && v < n - 1 ? v : 0; };
  function switchSeries(k, at) {
    if (!SERIES[k] || !root) return;
    if (k !== series) { series = k; renderDots(); }
    playing = true; syncPlay();
    go(at != null ? at : savedAt());
  }
  function onKey(e) {
    if (!root) return;
    if (mode === "inline" && !root.contains(document.activeElement) && document.activeElement !== document.body) return;
    if (e.target.matches && e.target.matches("input,select,textarea,[role=slider]")) return;
    if (e.key === "Escape" && mode === "overlay") close();
    else if (e.key === "ArrowRight") go(idx + 1);
    else if (e.key === "ArrowLeft") go(idx - 1);
    else if (e.key === " " && !(e.target.closest && e.target.closest("button"))) { e.preventDefault(); playing = !playing; syncPlay(); }
  }
  function go(i) {
    if (!live) return;
    i = clamp(i, 0, S().length - 1);
    if (scene && scene.destroy) scene.destroy();
    idx = i; elapsed = 0; cap = -1;
    const s = S()[idx];
    els.n.textContent = `${idx + 1} of ${S().length}`; els.title.textContent = s.title;
    els.dots.forEach((d, k) => { d.classList.toggle("on", k === idx); d.classList.toggle("done", k < idx); d.setAttribute("aria-current", k === idx ? "step" : "false"); });
    els.stage.innerHTML = "";
    els.stage.classList.remove("in"); void els.stage.offsetWidth; els.stage.classList.add("in");
    scene = s.mount(els.stage, live, api) || {};
    // the opening caption is written now, not on the first animation frame: a background tab may not draw
    // frames for a while, and a scene should never sit there without its sentence
    cap = 0; els.cap.innerHTML = s.beats[0][1](live);
    if (scene.beat) scene.beat(0);
    store.set(SERIES[series].store, String(idx));
  }
  function loop(ts) {
    const dt = lastTs ? Math.min(0.1, (ts - lastTs) / 1000) : 0; lastTs = ts;
    if (live && scene) {
      const s = S()[idx];
      if (playing && !document.hidden) elapsed = Math.min(s.secs, elapsed + dt);
      let k = 0; s.beats.forEach((b, j) => { if (elapsed >= b[0]) k = j; });
      // reduced motion: each beat's picture appears in its finished state instead of animating towards it
      const tEff = REDUCED ? ((s.beats[k + 1] || [s.secs])[0] - 0.01) : elapsed;
      if (scene.tick) scene.tick(tEff, dt);
      if (k !== cap) {
        cap = k; els.cap.classList.remove("in"); void els.cap.offsetWidth; els.cap.classList.add("in");
        els.cap.innerHTML = s.beats[k][1](live);
        if (scene.beat) scene.beat(k);
      }
      els.prog.style.width = (elapsed / s.secs * 100).toFixed(2) + "%";
      if (playing && elapsed >= s.secs) {
        if (idx < S().length - 1) go(idx + 1);
        else { playing = false; syncPlay(); els.hint.hidden = true; store.set(SERIES[series].store, "0"); }
      }
    }
    raf = requestAnimationFrame(loop);
  }
  async function start(container, at, which) {
    series = SERIES[which] ? which : "basics";
    build(container);
    document.addEventListener("keydown", onKey);
    playing = true; syncPlay(); lastTs = 0;
    raf = requestAnimationFrame(loop);
    live = await loadLive();
    if (!root) return;
    go(at != null ? at : savedAt());
    root.querySelector('[data-a="next"]').focus({preventScroll: true});
  }

  function open(at, which) {
    if (root) { if (which) switchSeries(which, at); return; }
    mode = "overlay";
    document.documentElement.classList.add("learn-open");
    start(document.body, at, which);
  }
  function close() {
    if (!root || mode !== "overlay") return;
    cancelAnimationFrame(raf); raf = 0;
    if (scene && scene.destroy) scene.destroy();
    scene = null; live = null; root.remove(); root = null;
    document.removeEventListener("keydown", onKey);
    document.documentElement.classList.remove("learn-open");
    if (location.hash === "#learn" || location.hash === "#deeper") history.replaceState(null, "", location.pathname + location.search);
    const b = document.getElementById("learnbtn"); if (b) b.focus();
  }
  function inline(container, which) { mode = "inline"; start(container, 0, which); }

  // ---------------------------------------------------------------- how people arrive
  window.addEventListener("load", () => {
    const page = document.body.dataset.page;
    if (page === "learn") { const c = document.getElementById("learnInline"); if (c) inline(c, location.hash === "#deeper" ? "deeper" : "basics"); return; }
    if (location.hash === "#learn") open(0);
    else if (location.hash === "#deeper") open(0, "deeper");
    else if (location.hash === "#tour") setTimeout(() => { HELP.tour(); history.replaceState(null, "", location.pathname); }, 1500);
    else setTimeout(() => HELP.offer(), 1200);
  });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#learn") open(0, "basics");
    else if (location.hash === "#deeper") { if (mode === "inline" && root) switchSeries("deeper", 0); else open(0, "deeper"); }
  });

  return {open, close, SCENES, DEEPER};
})();
