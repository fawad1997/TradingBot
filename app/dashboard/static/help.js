/* Quantara - help that sits where the question is.
 *
 * Three things, all aimed at someone opening the dashboard for the first time:
 *   1. term cards     - every trading word on screen (R, stop, drawdown ...) explains itself on hover, tap or
 *                       keyboard focus. The words live in TERMS in glossary.js; a template marks one with defn().
 *   2. "Explain this page" - a spotlight walks over each panel with a sentence on what it is for. The steps are
 *                       TOURS below, one list per page, each pointing at an element id on that page.
 *   3. "Explanations on/off" - beginners see every explanatory line; an experienced reader can fold them away.
 *
 * Nothing here can change anything: it never calls the control API. It only reads the page and explains it.
 */

/* A trading word that explains itself. Use in templates: `${defn("R")}` or `${defn("STOP", "stop")}`. */
function defn(key, label) {
  const t = (typeof TERMS !== "undefined" && TERMS[key]) || null;
  const text = label == null ? (t ? t.title : key) : label;
  const safe = String(text).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  return t ? `<abbr class="term" data-term="${key}" tabindex="0">${safe}</abbr>` : safe;
}

const HELP = (() => {
  const PAGE = document.body.dataset.page;
  const store = {
    get: k => { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} },
  };
  const escH = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  // ---------------------------------------------------------------- small drawings for the term cards
  const PICS = {
    trade: `<line x1="4" y1="12" x2="116" y2="12" class="p-good" stroke-dasharray="4 3"/><text x="116" y="9" class="p-t" text-anchor="end">target</text>
      <line x1="4" y1="34" x2="116" y2="34" class="p-dim"/><text x="116" y="31" class="p-t" text-anchor="end">entry</text>
      <line x1="4" y1="54" x2="116" y2="54" class="p-bad" stroke-dasharray="4 3"/><text x="116" y="62" class="p-t" text-anchor="end">stop</text>
      <polyline points="4,34 18,30 28,38 40,28 52,32 64,22 76,26 88,14 96,12" class="p-sky"/>`,
    spread: `<rect x="4" y="22" width="112" height="14" class="p-sandfill"/>
      <line x1="4" y1="22" x2="116" y2="22" class="p-dim"/><text x="6" y="18" class="p-t">buy price</text>
      <line x1="4" y1="36" x2="116" y2="36" class="p-dim"/><text x="6" y="50" class="p-t">sell price</text>
      <text x="114" y="32" class="p-t p-sandtxt" text-anchor="end">the gap</text>`,
    equity: `<polygon points="4,54 20,48 34,50 50,40 66,42 82,30 98,26 116,16 116,60 4,60" class="p-goodfill"/>
      <polyline points="4,54 20,48 34,50 50,40 66,42 82,30 98,26 116,16" class="p-good"/>`,
    drawdown: `<polyline points="4,50 24,36 44,22 56,14 70,26 86,38 100,34 116,30" class="p-sky"/>
      <line x1="56" y1="14" x2="116" y2="14" class="p-dim" stroke-dasharray="3 3"/><text x="58" y="10" class="p-t">best so far</text>
      <line x1="90" y1="16" x2="90" y2="36" class="p-bad"/><polygon points="86,32 94,32 90,39" class="p-badfill"/>`,
    expectancy: `<line x1="4" y1="34" x2="116" y2="34" class="p-dim"/>
      ${[8, 18, 28, 38, 48, 58, 68].map(x => `<rect x="${x}" y="27" width="7" height="7" class="p-goodfill2"/>`).join("")}
      <rect x="82" y="34" width="9" height="24" class="p-badfill"/><rect x="100" y="34" width="9" height="24" class="p-badfill"/>
      <text x="8" y="20" class="p-t">many small wins</text><text x="116" y="64" class="p-t" text-anchor="end">few big losses</text>`,
    range: `<line x1="4" y1="36" x2="116" y2="36" class="p-dim"/><line x1="46" y1="24" x2="46" y2="48" class="p-text"/>
      <text x="46" y="60" class="p-t" text-anchor="middle">0</text>
      <rect x="30" y="31" width="64" height="10" rx="5" class="p-sandfill2"/><circle cx="62" cy="36" r="4" class="p-sanddot"/>
      <text x="62" y="20" class="p-t" text-anchor="middle">crosses zero = maybe luck</text>`,
    split: `<rect x="4" y="22" width="78" height="18" class="p-dimfill"/><rect x="84" y="22" width="32" height="18" class="p-goodfill2"/>
      <text x="43" y="54" class="p-t" text-anchor="middle">tuned here</text><text x="100" y="54" class="p-t" text-anchor="middle">judged here</text>`,
    r: `<line x1="40" y1="6" x2="40" y2="60" class="p-dim"/>
      <text x="34" y="10" class="p-t" text-anchor="end">+2R</text><line x1="38" y1="8" x2="116" y2="8" class="p-good" stroke-dasharray="2 3"/>
      <text x="34" y="27" class="p-t" text-anchor="end">+1R</text><line x1="38" y1="25" x2="116" y2="25" class="p-good" stroke-dasharray="2 3"/>
      <text x="34" y="45" class="p-t" text-anchor="end">0</text><line x1="38" y1="42" x2="116" y2="42" class="p-dim"/>
      <text x="34" y="62" class="p-t" text-anchor="end">−1R</text><line x1="38" y1="59" x2="116" y2="59" class="p-bad"/>
      <text x="78" y="53" class="p-t" text-anchor="middle">what it risked</text>`,
  };

  // ---------------------------------------------------------------- term cards
  let card = null, cardFor = null, pinned = false, hideTimer = 0;
  function showTerm(el) {
    const key = el.dataset.term, t = (typeof TERMS !== "undefined") && TERMS[key];
    if (!t) return;
    clearTimeout(hideTimer);
    if (!card) {
      card = document.createElement("div"); card.className = "termcard"; card.setAttribute("role", "tooltip");
      card.addEventListener("pointerenter", () => clearTimeout(hideTimer));
      card.addEventListener("pointerleave", () => { if (!pinned) hideSoon(); });
      document.body.appendChild(card);
    }
    cardFor = el;
    card.innerHTML = `${t.pic && PICS[t.pic] ? `<svg viewBox="0 0 120 66" class="tc-pic" aria-hidden="true">${PICS[t.pic]}</svg>` : ""}
      <b>${escH(t.title)}</b><p>${escH(t.short)}</p>${t.long ? `<p class="mut">${escH(t.long)}</p>` : ""}`;
    card.style.display = "block";
    const r = el.getBoundingClientRect(), c = card.getBoundingClientRect();
    let left = r.left + r.width / 2 - c.width / 2, top = r.bottom + 8;
    if (top + c.height > innerHeight - 8) top = r.top - c.height - 8;
    card.style.left = Math.max(8, Math.min(innerWidth - c.width - 8, left)) + "px";
    card.style.top = Math.max(8, top) + "px";
    card.classList.remove("in"); void card.offsetWidth; card.classList.add("in");
  }
  function hideTerm() { if (card) card.style.display = "none"; cardFor = null; pinned = false; }
  function hideSoon() { clearTimeout(hideTimer); hideTimer = setTimeout(hideTerm, 160); }
  document.addEventListener("pointerover", e => {
    const el = e.target.closest && e.target.closest(".term");
    if (el && e.pointerType === "mouse" && !pinned) showTerm(el);
  });
  document.addEventListener("pointerout", e => {
    const el = e.target.closest && e.target.closest(".term");
    if (el && e.pointerType === "mouse" && !pinned && !(card && card.contains(e.relatedTarget))) hideSoon();
  });
  document.addEventListener("click", e => {
    const el = e.target.closest && e.target.closest(".term");
    if (el) { e.preventDefault(); if (pinned && cardFor === el) hideTerm(); else { showTerm(el); pinned = true; } return; }
    if (card && !card.contains(e.target)) hideTerm();
  });
  document.addEventListener("focusin", e => { if (e.target.classList && e.target.classList.contains("term")) showTerm(e.target); });
  document.addEventListener("focusout", e => { if (e.target.classList && e.target.classList.contains("term") && !pinned) hideSoon(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") hideTerm(); });
  addEventListener("scroll", () => { if (!pinned) hideTerm(); }, {passive: true});

  // ---------------------------------------------------------------- explanations on / off
  const isExpert = () => store.get("qa_expert") === "1";
  function applyMode() {
    document.documentElement.classList.toggle("expert", isExpert());
    const b = document.getElementById("modebtn");
    // the button is a switch: its state lives in aria-pressed (which the knob is drawn from), not in its label
    if (b) { b.setAttribute("aria-pressed", String(!isExpert()));
      b.title = isExpert() ? "Explanations are hidden — click to show them again" : "Explanations are shown — click to hide them"; }
  }
  function toggleMode() { store.set("qa_expert", isExpert() ? "0" : "1"); applyMode(); }
  applyMode();

  // ---------------------------------------------------------------- "Explain this page"
  // [element id selector, title, what it is for]. A step whose element is missing or hidden is skipped, and a
  // test checks that every id here exists on its page, so a tour can never point at nothing.
  const TOURS = {
    overview: [
      ["#hero", "Start here: one sentence", "This line says whether the bot is trading and, if not, why — in plain words. On the right is what trading made or lost today. Money you added is shown separately, so it never looks like profit."],
      ["#pills", "Health lights", "One light per part of the machine: the link to MetaTrader, the price feed, the database, order sending, the broker check, the rules and risk. Seafoam means fine, sand means waiting, coral means look now."],
      ["#killbtn", "The stop button", "Stops NEW trades straight away. Trades already open keep their stop and target at the broker. It always asks you to confirm first."],
      ["#equity", "Account value over time", "Hover to read any moment. Drag across the chart to zoom in. Dots are finished trades — seafoam won, coral lost — click one for its story. Dashed sand lines mark balance changes that did not come from trading."],
      ["#openPositions", "Open right now", "Each bar runs from the stop on the left (the losing end) to the target on the right, for buys and sells alike. The white mark is where it got in; the coloured part is how far price has moved since. Hover the bar to see what any price would mean in money."],
      ["#funnel", "Why it is not trading more", "Every chance the bot looked at today flows through this pipe. Most drop out at the first step because the chart did not match any rule — that is normal. Click a gap in the pipe, or a step, to see the exact reasons."],
      ["#symbolTiles", "The markets it watches", "One tile per market. Clicking a tile asks before it starts or stops NEW trades there. Open trades are never touched."],
      ["#risk", "Seatbelts", "How much it may still lose today, and how far the account is below its best. Trade sizes shrink on their own as the account falls."],
      ["#cf", "Trades it skipped", "Blocked signals are still followed on paper. If they would have lost money, the checks that blocked them are earning their keep."],
      ["#techHealth", "Technical detail", "Folded away on purpose. You never need these for a daily check, but nothing is hidden."],
    ],
    realtime: [
      ["#top", "The numbers that matter now", "Balance, what the account is worth with open trades, profit banked today, and how quickly the broker answers."],
      ["#positions", "Trades open now", "Each bar runs from the stop (left, the losing end) to the target (right). Hover a bar to see what any price would mean in money."],
      ["#gapGauge", "The broker's gap right now", "How wide the buy/sell gap is against the most the bot accepts on this market. Past the limit, it will not trade."],
      ["#market", "What the market is doing", "Prices, how lively the market is, the trading session and the next big news release, for the market this screen follows."],
      ["#why", "Why it is or is not trading", "What each rule saw the last time it looked at the chart, in words."],
      ["#feed", "What just happened", "Events land here as they happen: orders, fills, stops moving, signals blocked."],
      ["#today", "Finished today", "Every trade that closed today. Click a row for the story of that trade."],
    ],
    logs: [
      ["#tabs", "Pick a record", "Everything the bot has ever recorded, one tab per kind. “Trades” is the place to start."],
      ["#tabnote", "What this tab shows", "One line on what the tab you picked contains and when it is useful."],
      ["#logtable", "The records", "Newest first. Hover a cell for its full value. On the Trades tab, click a finished trade to see its story."],
    ],
    analysis: [
      ["#lsum", "Where the money went", "Every losing trade, and how far it went the right way before it turned. A loser that was well ahead first points at the exit, not the entry. Click a row for its story."],
      ["#bsum", "Trades it decided not to take", "Signals stopped by a safety, cost or risk check. Tick the box to see only the ones the bot itself thought would win."],
      ["#sessbars", "Better at some hours?", "The average result per trade in each trading session. Grey bars have too few trades to mean anything yet."],
      ["#pfrange", "Is the whole book making money?", "The dot is the average per trade, the bar is where the true average probably lies. A bar that crosses zero means it has not been shown to make money yet — however good the headline looks."],
      ["#ruleCards", "Which rule is actually working", "Each rule's real trades and its signals followed on paper. Fewer than about thirty results is noise, not evidence."],
      ["#selForest", "How much each rule may trade", "Every rule on every market, scored from its own record after costs. Bars wholly right of zero earn full size; bars left of zero are benched."],
      ["#labForest", "Proven on unseen data?", "Backtest results on history the rules were not tuned on. Only the EDGE grade means the evidence is strong."],
    ],
    config: [
      ["#envinfo", "What it is set up to do", "Practice or real money, how many markets, and which optional parts are switched on."],
      ["#killstate", "Controls", "Stop or resume new trades, re-check and resume, close everything. Each one asks you to confirm, wants a reason, and is written to the audit trail."],
      ["#configStatusReasons", "Why it is or is not trading", "Every reason the system is pausing, in words, with how long it has been paused."],
      ["#integrity", "How strictly it judges itself", "Settings that decide what gets refused. A setting that limits nothing says so in sand."],
      ["#readiness", "Before real money", "Choices that are fine on a practice account and not on a funded one. Real money is a separate, later decision."],
      ["#cstrats", "Trading rules", "Switch a rule on or off. A rule's own logic can never be changed from a browser."],
      ["#params", "Settings you can change while it runs", "Every change needs a reason and is checked against hard safety ceilings first."],
      ["#hist", "Change history", "Who changed what, when, and why."],
    ],
  };

  let tourState = null;
  function tour(page = PAGE) {
    const steps = (TOURS[page] || []).filter(([sel]) => { const el = document.querySelector(sel); return el && el.getClientRects().length; });
    if (!steps.length) return;
    endTour();
    // A transparent sheet under the spotlight swallows clicks, so nothing on the page - a symbol tile, the stop
    // button - can be pressed by accident while the tour is pointing at it. Clicking it just ends the tour.
    const block = document.createElement("div"); block.className = "tour-block";
    const spot = document.createElement("div"); spot.className = "tour-spot";
    const bub = document.createElement("div"); bub.className = "tour-bubble"; bub.setAttribute("role", "dialog");
    bub.setAttribute("aria-label", "Explain this page");
    document.body.append(block, spot, bub);
    tourState = {steps, i: 0, spot, bub, block, box: null};
    const onKey = e => {
      if (!tourState) return;
      if (e.key === "Escape") endTour();
      else if (e.key === "ArrowRight") go(tourState.i + 1);
      else if (e.key === "ArrowLeft") go(tourState.i - 1);
    };
    const onMove = () => place();
    tourState.off = () => { document.removeEventListener("keydown", onKey); removeEventListener("scroll", onMove); removeEventListener("resize", onMove); };
    block.addEventListener("click", () => endTour());
    document.addEventListener("keydown", onKey);
    addEventListener("scroll", onMove, {passive: true}); addEventListener("resize", onMove);
    bub.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.a === "next") go(tourState.i + 1);
      if (b.dataset.a === "back") go(tourState.i - 1);
      if (b.dataset.a === "end") endTour();
    });
    go(0);
  }
  function boxFor(el) {
    if (el.closest("header")) return el;
    return el.matches(".card,.hero,details") ? el : (el.closest(".card,.hero,details.tech") || el);
  }
  function go(i) {
    const S = tourState; if (!S) return;
    if (i >= S.steps.length) { endTour(); return; }
    S.i = Math.max(0, i);
    const [sel, title, text] = S.steps[S.i];
    const el = document.querySelector(sel);
    S.box = boxFor(el);
    const last = S.i === S.steps.length - 1;
    S.bub.innerHTML = `<div class="tb-n">${S.i + 1} of ${S.steps.length}</div><b>${escH(title)}</b><p>${escH(text)}</p>
      <div class="tb-btns"><button class="btn small" data-a="end">Close</button><span class="spacer"></span>
        ${S.i ? '<button class="btn small" data-a="back">Back</button>' : ""}
        <button class="btn small green" data-a="next">${last ? "Done" : "Next"}</button></div>`;
    if (!S.box.closest("header")) S.box.scrollIntoView({block: "center", behavior: (typeof VZ !== "undefined" && VZ.REDUCED) ? "auto" : "smooth"});
    place(); setTimeout(place, 350); setTimeout(place, 700);
    S.bub.querySelector('[data-a="next"]').focus({preventScroll: true});
  }
  function place() {
    const S = tourState; if (!S || !S.box) return;
    const r = S.box.getBoundingClientRect(), p = 6;
    Object.assign(S.spot.style, {left: r.left - p + "px", top: r.top - p + "px", width: r.width + 2 * p + "px", height: r.height + 2 * p + "px"});
    const b = S.bub.getBoundingClientRect();
    let top = r.bottom + 14;
    if (top + b.height > innerHeight - 10) top = r.top - b.height - 14;
    if (top < 10) top = Math.min(innerHeight - b.height - 10, Math.max(10, r.top + 10));
    S.bub.style.top = top + "px";
    S.bub.style.left = Math.max(10, Math.min(innerWidth - b.width - 10, r.left)) + "px";
  }
  function endTour() {
    if (!tourState) return;
    tourState.off(); tourState.block.remove(); tourState.spot.remove(); tourState.bub.remove(); tourState = null;
  }

  // ---------------------------------------------------------------- the first-visit offer
  function offer() {
    if (PAGE === "learn" || store.get("qa_offered") === "1" || /learn|tour/.test(location.hash)) return;
    const box = document.createElement("div");
    box.className = "offer"; box.setAttribute("role", "dialog"); box.setAttribute("aria-label", "New here?");
    box.innerHTML = `<b>New here?</b><p>A 5-minute animated walkthrough explains what this bot does, how to read this
      screen and how to tell whether it is working. Nothing you do in it can change the bot.</p>
      <div class="tb-btns"><button class="btn small green" data-a="learn">Play the walkthrough</button>
        <button class="btn small" data-a="tour">Explain this page</button>
        <button class="btn small" data-a="no">Not now</button></div>`;
    document.body.appendChild(box);
    box.addEventListener("click", e => {
      const a = (e.target.closest("button") || {}).dataset; if (!a || !a.a) return;
      store.set("qa_offered", "1"); box.remove();
      if (a.a === "learn" && typeof LEARN !== "undefined") LEARN.open();
      if (a.a === "tour") tour();
    });
  }

  return {defn, tour, endTour, toggleMode, applyMode, isExpert, offer, TOURS, PICS, store};
})();
