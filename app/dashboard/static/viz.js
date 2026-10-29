/* Quantara - pictures you can touch.
 *
 * Every interactive chart on the dashboard is built from the pieces in this file. Three rules, all taken from
 * docs/DASHBOARD.md:
 *   - colour carries meaning, and is read from the same CSS variables as the rest of the page, so a chart can
 *     never drift away from the cards around it (seafoam = up / working, coral = down / stopped, sand = waiting);
 *   - motion explains a change - a line drawing itself in, a bar growing to its value - and is never there for
 *     decoration. It plays when something new is shown, never on the 1.5 s refresh;
 *   - anyone whose system asks for reduced motion gets the final frame straight away.
 *
 * No libraries, deliberately: the dashboard is served from the trading machine and has to work with the network
 * down. Canvas where there are many points, SVG where there are few, plain HTML for bars.
 */
const VZ = (() => {
  const REDUCED = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const cssVar = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
  const COLOR = () => ({seafoam: cssVar("--seafoam"), seafoamDim: cssVar("--seafoam-dim"), coral: cssVar("--coral"),
    sand: cssVar("--sand"), sky: cssVar("--sky"), iris: cssVar("--iris"), text: cssVar("--text"), dim: cssVar("--dim"),
    faint: cssVar("--faint"), rule: cssVar("--rule"), ruleSoft: cssVar("--rule-soft"), raised: cssVar("--raised"),
    surface: cssVar("--surface"), ink: cssVar("--ink"), ui: cssVar("--ui"), num: cssVar("--num")});
  const TONE = {good: "seafoam", bad: "coral", wait: "sand", info: "sky", mut: "faint"};
  const toneColor = (c, tone) => c[TONE[tone] || "faint"];

  // small formatters of our own, so this file does not depend on app.js having loaded first
  // a value that rounds to zero prints as 0.00, never "-0.00"
  const zap = (v, d) => Math.abs(+v) < 0.5 * Math.pow(10, -d) ? 0 : +v;
  const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(+v)) ? "–"
    : zap(v, d).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
  const sgn = (v, d = 2) => (v === null || v === undefined || isNaN(+v)) ? "–" : (zap(v, d) > 0 ? "+" : "") + fmt(v, d);
  const escH = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = t => 1 - Math.pow(1 - t, 3);
  const rgba = (hex, a) => {
    const h = String(hex).replace("#", "");
    if (h.length !== 6) return hex;
    return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
  };

  // ---------------------------------------------------------------- motion
  /* from -> to over ms, eased. Returns a function that cancels it. Reduced motion: straight to the end. */
  function tween(from, to, ms, frame, done) {
    if (REDUCED || !(ms > 0)) { frame(to, 1); if (done) done(); return () => {}; }
    let raf = 0, t0 = null, stopped = false;
    const step = ts => {
      if (stopped) return;
      if (t0 === null) t0 = ts;
      const p = Math.min(1, (ts - t0) / ms);
      frame(from + (to - from) * ease(p), p);
      if (p < 1) raf = requestAnimationFrame(step); else if (done) done();
    };
    raf = requestAnimationFrame(step);
    return () => { stopped = true; cancelAnimationFrame(raf); };
  }

  /* A number that rolls to its new value, like a ticker, rather than jumping. */
  function countUp(el, to, format, ms = 700) {
    if (!el) return;
    if (to === null || to === undefined || isNaN(+to)) { el.textContent = format(to); el._v = null; return; }
    const from = el._v == null ? 0 : el._v;
    el._v = +to;
    if (el._stop) el._stop();
    el._stop = tween(from, +to, from === +to ? 0 : ms, v => { el.textContent = format(v); });
  }

  // ---------------------------------------------------------------- tooltip
  // One floating box for the whole page. Anything with a data-tip attribute gets it on hover and on keyboard focus.
  let tipEl = null;
  function tip(html, x, y) {
    if (!tipEl) {
      tipEl = document.createElement("div"); tipEl.className = "vz-tip"; tipEl.setAttribute("role", "tooltip");
      document.body.appendChild(tipEl);
    }
    tipEl.innerHTML = html; tipEl.style.display = "block";
    const r = tipEl.getBoundingClientRect();
    let left = x + 14, top = y + 16;
    if (left + r.width > innerWidth - 8) left = x - r.width - 14;
    if (top + r.height > innerHeight - 8) top = y - r.height - 14;
    tipEl.style.left = Math.max(8, left) + "px"; tipEl.style.top = Math.max(8, top) + "px";
  }
  function untip() { if (tipEl) tipEl.style.display = "none"; }
  const tipHTML = el => escH(el.dataset.tip).replace(/\n/g, "<br>");
  document.addEventListener("pointermove", e => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t) tip(tipHTML(t), e.clientX, e.clientY);
  });
  document.addEventListener("pointerout", e => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t && !t.contains(e.relatedTarget)) untip();
  });
  document.addEventListener("focusin", e => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t) { const r = t.getBoundingClientRect(); tip(tipHTML(t), r.left, r.bottom); }
  });
  document.addEventListener("focusout", untip);
  addEventListener("scroll", untip, {passive: true});

  // ---------------------------------------------------------------- modal
  /* A centred panel over a dimmed page. Esc, the close button or a click outside closes it. */
  function modal(html, {label = "Details", wide = false} = {}) {
    closeModal();
    const back = document.createElement("div");
    back.className = "vz-modal-back";
    back.innerHTML = `<div class="vz-modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${escH(label)}">
      <button class="vz-x" aria-label="Close">&times;</button><div class="vz-modal-body">${html}</div></div>`;
    document.body.appendChild(back);
    const prev = document.activeElement;
    const close = () => { back.remove(); document.removeEventListener("keydown", onKey); if (prev && prev.focus) prev.focus(); };
    const onKey = e => { if (e.key === "Escape") close(); };
    back.addEventListener("click", e => { if (e.target === back || e.target.closest(".vz-x")) close(); });
    document.addEventListener("keydown", onKey);
    back._close = close;
    back.querySelector(".vz-x").focus();
    return back.querySelector(".vz-modal-body");
  }
  function closeModal() { const m = document.querySelector(".vz-modal-back"); if (m && m._close) m._close(); }

  // ---------------------------------------------------------------- the line chart
  /* An account-value chart you can read with the mouse.
   *   hover        crosshair + the value, the time and the change since the start of what is on screen
   *   drag         zoom into a stretch of time; double-click (or the button) to zoom back out
   *   markers      finished trades as dots on the line - seafoam won, coral lost - hover to read, click for the story
   *   flags        balance changes no trade explains (a deposit or a top-up), so they never look like profit
   *   labels       the best and the lowest point on screen
   * set() redraws with the line drawing itself in; update() redraws in place, for the periodic refresh. */
  function lineChart(canvas, opts = {}) {
    let wrap = canvas.parentElement;
    if (!wrap.classList.contains("vz-wrap")) {
      wrap = document.createElement("div"); wrap.className = "vz-wrap";
      canvas.parentElement.insertBefore(wrap, canvas); wrap.appendChild(canvas);
    }
    const resetBtn = document.createElement("button");
    resetBtn.className = "btn small vz-reset"; resetBtn.textContent = "Zoom out"; resetBtn.hidden = true;
    wrap.appendChild(resetBtn);
    canvas.style.touchAction = "pan-y";
    const fx = opts.formatX || (t => new Date(t * 1000).toLocaleString());
    const fy = opts.formatY || (v => fmt(v));
    const S = {pts: [], key: "equity", markers: [], flags: [], view: null, hover: null, drag: null, prog: 1, hits: []};
    const pad = {l: 66, r: 14, t: 22, b: 26};
    let W = 0, H = 0, g = null, X = null, Y = null, Tx = null, vis = [], stopAnim = () => {};

    const valueAt = t => {           // the line's value at time t, from the nearest point
      const p = S.pts; if (!p.length) return null;
      let lo = 0, hi = p.length - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (p[mid].t < t) lo = mid; else hi = mid; }
      const q = Math.abs(p[lo].t - t) <= Math.abs(p[hi].t - t) ? p[lo] : p[hi];
      return q[S.key];
    };

    function draw() {
      const dpr = window.devicePixelRatio || 1;
      W = canvas.clientWidth; H = canvas.clientHeight;
      if (!W || !H) return;
      canvas.width = W * dpr; canvas.height = H * dpr;
      g = canvas.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
      const c = COLOR();
      const all = S.pts;
      const a = S.view ? S.view[0] : (all.length ? all[0].t : 0);
      const b = S.view ? S.view[1] : (all.length ? all[all.length - 1].t : 1);
      vis = all.filter(p => p.t >= a && p.t <= b && p[S.key] != null);
      resetBtn.hidden = !S.view;
      if (vis.length < 2) {
        g.fillStyle = c.dim; g.font = `13px ${c.ui}`;
        g.fillText(opts.empty || "The line appears after a few minutes of running", 12, 26); return;
      }
      const ys = vis.map(p => p[S.key]);
      let lo = Math.min(...ys), hi = Math.max(...ys);
      if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
      // padding never pushes an all-positive line (an account value) below zero
      const py = (hi - lo) * 0.1; lo = lo >= 0 ? Math.max(0, lo - py) : lo - py; hi += py;
      X = t => pad.l + (t - a) / ((b - a) || 1) * (W - pad.l - pad.r);
      Tx = x => a + (x - pad.l) / (W - pad.l - pad.r) * ((b - a) || 1);
      Y = v => pad.t + (hi - v) / (hi - lo) * (H - pad.t - pad.b);

      // grid and the value scale
      g.font = `11px ${c.num}`; g.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const v = lo + (hi - lo) * i / 4, y = Y(v);
        g.strokeStyle = c.ruleSoft; g.beginPath(); g.moveTo(pad.l, y); g.lineTo(W - pad.r, y); g.stroke();
        g.fillStyle = c.faint; g.fillText(fy(v), 4, y + 4);
      }
      g.fillStyle = c.faint;
      const l0 = fx(a), l1 = fx(b);
      g.fillText(l0, pad.l, H - 7);
      g.fillText(l1, W - pad.r - g.measureText(l1).width, H - 7);

      // the line, drawn up to how far the draw-in animation has got
      const upto = Math.max(2, Math.ceil(vis.length * S.prog));
      const shown = vis.slice(0, upto);
      const up = vis[vis.length - 1][S.key] >= vis[0][S.key];
      const col = up ? c.seafoam : c.coral;
      const line = new Path2D();
      shown.forEach((p, i) => i ? line.lineTo(X(p.t), Y(p[S.key])) : line.moveTo(X(p.t), Y(p[S.key])));
      const area = new Path2D(line);
      area.lineTo(X(shown[shown.length - 1].t), H - pad.b); area.lineTo(X(shown[0].t), H - pad.b); area.closePath();
      const grad = g.createLinearGradient(0, pad.t, 0, H - pad.b);
      grad.addColorStop(0, rgba(col, 0.24)); grad.addColorStop(1, rgba(col, 0));
      g.fillStyle = grad; g.fill(area);
      g.strokeStyle = col; g.lineWidth = 2; g.lineJoin = "round"; g.stroke(line);
      S.hits = [];
      if (S.prog < 1) return;

      // balance changes no trade explains. Flags close together on screen share one label with their total,
      // so two top-ups an hour apart do not print on top of each other; each line keeps its own tooltip.
      g.font = `11px ${c.ui}`;
      const shownFlags = S.flags.filter(f => f.t >= a && f.t <= b).sort((p, q) => p.t - q.t);
      const groups = [];
      shownFlags.forEach(f => {
        const x = X(f.t), last = groups[groups.length - 1];
        if (last && x - last.x1 < 150) { last.items.push(f); last.x1 = x; } else groups.push({x0: x, x1: x, items: [f]});
        g.save(); g.setLineDash([4, 4]); g.strokeStyle = rgba(c.sand, 0.8); g.beginPath();
        g.moveTo(x, pad.t - 4); g.lineTo(x, H - pad.b); g.stroke(); g.restore();
        S.hits.push({x, y: pad.t + 6, r: 9, html: f.label});
      });
      groups.forEach(gr => {
        const sum = gr.items.reduce((s, f) => s + (+f.amount || 0), 0);
        const txt = gr.items.length > 1 && gr.items.every(f => f.amount != null)
          ? `${sgn(sum, 0)} not from trading (${gr.items.length} changes)` : (gr.items[0].short || "not from trading");
        const tw = g.measureText(txt).width, mid = (gr.x0 + gr.x1) / 2;
        g.fillStyle = c.sand; g.fillText(txt, clamp(mid - tw / 2, pad.l, W - pad.r - tw), pad.t - 8);
      });

      // the best and the lowest point on screen
      let hiP = vis[0], loP = vis[0];
      vis.forEach(p => { if (p[S.key] > hiP[S.key]) hiP = p; if (p[S.key] < loP[S.key]) loP = p; });
      const note = (p, text, below) => {
        const x = X(p.t), y = Y(p[S.key]);
        g.fillStyle = c.text; g.beginPath(); g.arc(x, y, 3, 0, Math.PI * 2); g.fill();
        g.font = `11px ${c.ui}`; g.fillStyle = c.dim;
        const tw = g.measureText(text).width;
        g.fillText(text, clamp(x - tw / 2, pad.l + 2, W - pad.r - tw), clamp(below ? y + 16 : y - 8, pad.t + 10, H - pad.b - 4));
      };
      if (hiP !== loP) { note(hiP, `best ${fy(hiP[S.key])}`, false); note(loP, `lowest ${fy(loP[S.key])}`, true); }

      // finished trades
      S.markers.forEach(m => {
        if (m.t < a || m.t > b) return;
        const v = valueAt(m.t); if (v == null) return;
        const x = X(m.t), y = Y(v);
        g.beginPath(); g.arc(x, y, 5, 0, Math.PI * 2);
        g.fillStyle = toneColor(c, m.tone); g.fill(); g.lineWidth = 2; g.strokeStyle = c.surface; g.stroke();
        S.hits.push({x, y, r: 9, html: m.label, data: m.data});
      });

      // the crosshair
      if (S.hover && !S.drag) {
        const hx = clamp(S.hover.x, pad.l, W - pad.r);
        const t = Tx(hx);
        let near = vis[0];
        vis.forEach(p => { if (Math.abs(p.t - t) < Math.abs(near.t - t)) near = p; });
        const x = X(near.t), y = Y(near[S.key]);
        g.strokeStyle = rgba(c.dim, 0.5); g.lineWidth = 1; g.beginPath(); g.moveTo(x, pad.t); g.lineTo(x, H - pad.b); g.stroke();
        g.fillStyle = col; g.beginPath(); g.arc(x, y, 4, 0, Math.PI * 2); g.fill();
      }
      if (S.drag && S.drag.x1 != null) {
        const x0 = Math.min(S.drag.x0, S.drag.x1), x1 = Math.max(S.drag.x0, S.drag.x1);
        g.fillStyle = rgba(c.sky, 0.14); g.fillRect(x0, pad.t, x1 - x0, H - pad.t - pad.b);
      }
    }

    const hitAt = (x, y) => S.hits.find(h => Math.hypot(h.x - x, h.y - y) <= h.r);
    const local = e => { const r = canvas.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; };
    function hoverAt(e) {
      const p = local(e);
      S.hover = p;
      if (S.drag) { S.drag.x1 = p.x; draw(); return; }
      draw();
      const hit = hitAt(p.x, p.y);
      canvas.style.cursor = hit && hit.data ? "pointer" : (opts.zoom === false ? "default" : "crosshair");
      if (hit) { tip(hit.html, e.clientX, e.clientY); return; }
      if (!vis.length || !Tx) return;
      const t = Tx(clamp(p.x, pad.l, W - pad.r));
      let near = vis[0];
      vis.forEach(q => { if (Math.abs(q.t - t) < Math.abs(near.t - t)) near = q; });
      const ch = near[S.key] - vis[0][S.key];
      tip(`<b>${escH(fy(near[S.key]))}</b><br><span class="mut">${escH(fx(near.t))}</span><br>`
        + `<span class="${ch > 0 ? "good" : ch < 0 ? "bad" : "mut"}">${escH(sgn(ch))}</span> <span class="mut">since the left edge</span>`,
        e.clientX, e.clientY);
    }
    canvas.addEventListener("pointermove", hoverAt);
    canvas.addEventListener("pointerleave", () => { S.hover = null; if (!S.drag) { untip(); draw(); } });
    canvas.addEventListener("pointerdown", e => {
      if (e.pointerType !== "mouse" || e.button !== 0 || opts.zoom === false) return;
      S.drag = {x0: local(e).x, x1: null}; canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointerup", e => {
      const p = local(e);
      const d = S.drag; S.drag = null;
      if (d && d.x1 != null && Math.abs(d.x1 - d.x0) > 10 && Tx) {
        const t0 = Tx(Math.min(d.x0, d.x1)), t1 = Tx(Math.max(d.x0, d.x1));
        if (S.pts.filter(q => q.t >= t0 && q.t <= t1).length >= 2) S.view = [t0, t1];
        untip(); draw(); return;
      }
      const hit = hitAt(p.x, p.y);
      if (hit && hit.data && opts.onMarker) { untip(); opts.onMarker(hit.data); }
      draw();
    });
    canvas.addEventListener("dblclick", () => { S.view = null; draw(); });
    resetBtn.addEventListener("click", () => { S.view = null; draw(); });
    if (window.ResizeObserver) new ResizeObserver(() => draw()).observe(canvas);

    return {
      /* new data: the line draws itself in */
      set(o) {
        Object.assign(S, {pts: o.pts || [], markers: o.markers || [], flags: o.flags || []});
        if (o.key) S.key = o.key;
        S.view = null; stopAnim();
        stopAnim = tween(0, 1, 900, p => { S.prog = Math.max(0.02, p); draw(); });
      },
      /* same view, fresh numbers: no animation */
      update(o) {
        if (o.pts) S.pts = o.pts; if (o.markers) S.markers = o.markers; if (o.flags) S.flags = o.flags;
        if (o.key) S.key = o.key;
        if (S.prog >= 1) draw();
      },
      setKey(k) { S.key = k; stopAnim(); stopAnim = tween(0, 1, 600, p => { S.prog = Math.max(0.02, p); draw(); }); },
    };
  }

  // ---------------------------------------------------------------- ranges on a shared axis
  /* A "forest" of ranges: one row per thing being judged, a dot for the best estimate and a bar for where the
   * true value probably lies, all on one axis with zero marked. The single question it answers at a glance:
   * is the whole bar right of zero (shown to make money), left of it (shown to lose), or across it (could be
   * luck)? rows: [{label, sub, lo, mid, hi, chip: {text, tone}, tip, dim}] */
  function forest(el, rows, opts = {}) {
    if (!el) return;
    if (!rows || !rows.length) { el.innerHTML = `<div class="empty">${escH(opts.empty || "Nothing to show yet")}</div>`; return; }
    const unit = opts.unit ?? "R";
    const vals = rows.flatMap(r => [r.lo, r.mid, r.hi]).filter(v => v != null && isFinite(v));
    let min = opts.min ?? Math.min(0, ...vals), max = opts.max ?? Math.max(0, ...vals);
    if (max - min < 1e-9) { max += 0.5; min -= 0.5; }
    const padv = (max - min) * 0.06; min -= padv; max += padv;
    const P = v => clamp((v - min) / (max - min) * 100, 0, 100);
    const z = P(0);
    const toneOf = r => r.dim ? "mut" : (r.lo != null && r.lo > 0) ? "good" : (r.hi != null && r.hi < 0) ? "bad" : "wait";
    const verdict = t => t === "good" ? "whole range above zero" : t === "bad" ? "whole range below zero" : t === "mut" ? "too little data to judge" : "range crosses zero - could be luck";
    el.innerHTML = `<div class="forest${opts.big ? " big" : ""}">
      <div class="fx-axis"><span style="left:0">${escH(sgn(min, 2))}${unit}</span>
        <span class="zero" style="left:${z}%">0 = break even</span>
        <span style="right:0">${escH(sgn(max, 2))}${unit}</span></div>
      ${rows.map(r => {
        const t = toneOf(r);
        const has = r.lo != null && r.hi != null;
        const tipText = r.tip || `${r.label}${r.sub ? " · " + r.sub : ""}\nbest guess ${sgn(r.mid, 3)}${unit}`
          + (has ? `\nlikely between ${sgn(r.lo, 3)} and ${sgn(r.hi, 3)}${unit}\n${verdict(t)}` : "");
        return `<div class="fx-row" data-tip="${escH(tipText)}" tabindex="0">
          <div class="fx-label"><b>${escH(r.label)}</b>${r.chip ? ` <span class="chip ${r.chip.tone || "mut"}">${escH(r.chip.text)}</span>` : ""}
            ${r.sub ? `<small>${escH(r.sub)}</small>` : ""}</div>
          <div class="fx-track"><i class="fx-zero" style="left:${z}%"></i>
            ${has ? `<i class="fx-band ${t}" data-l="${P(r.lo)}" data-w="${P(r.hi) - P(r.lo)}" style="left:${P(r.mid ?? r.lo)}%;width:0"></i>` : ""}
            ${r.mid != null ? `<i class="fx-dot ${t}" style="left:${P(r.mid)}%"></i>` : ""}</div>
          <div class="fx-val ${t}">${r.mid == null ? "–" : escH(sgn(r.mid, opts.digits ?? 3)) + unit}</div></div>`;
      }).join("")}</div>`;
    // the bars grow out from the best guess to their full width, so the eye reads "a guess, and its uncertainty"
    requestAnimationFrame(() => requestAnimationFrame(() => el.querySelectorAll(".fx-band").forEach(b => {
      b.style.left = b.dataset.l + "%"; b.style.width = Math.max(0.6, +b.dataset.w) + "%";
    })));
  }

  // ---------------------------------------------------------------- horizontal bars
  /* Bars around zero: right for gains, left for losses. A row marked dim is shown greyed - an untrustworthy number
   * you can see is safer than one you cannot. rows: [{label, value, sub, tip, dim}] */
  function barChart(el, rows, opts = {}) {
    if (!el) return;
    if (!rows || !rows.length) { el.innerHTML = `<div class="empty">${escH(opts.empty || "Nothing to show yet")}</div>`; return; }
    const f = opts.fmt || (v => sgn(v, 3));
    const vals = rows.map(r => +r.value || 0);
    const min = Math.min(0, ...vals), max = Math.max(0, ...vals);
    const span = (max - min) || 1, z = (0 - min) / span * 100;
    el.innerHTML = `<div class="vbars">${rows.map(r => {
      const v = +r.value || 0;
      const left = v >= 0 ? z : z + v / span * 100, w = Math.abs(v) / span * 100;
      const t = r.dim ? "mut" : v > 0 ? "good" : v < 0 ? "bad" : "mut";
      return `<div class="vbar${r.dim ? " dim" : ""}" data-tip="${escH(r.tip || `${r.label}: ${f(v)}`)}" tabindex="0">
        <span class="vb-l">${escH(r.label)}${r.sub ? `<small>${escH(r.sub)}</small>` : ""}</span>
        <span class="vb-t"><i class="vb-z" style="left:${z}%"></i><i class="vb-f ${t}" data-l="${left}" data-w="${w}" style="left:${z}%;width:0"></i></span>
        <span class="vb-v ${t}">${escH(f(v))}</span></div>`;
    }).join("")}</div>`;
    requestAnimationFrame(() => requestAnimationFrame(() => el.querySelectorAll(".vb-f").forEach(b => {
      b.style.left = b.dataset.l + "%"; b.style.width = Math.max(0.4, +b.dataset.w) + "%";
    })));
  }

  // ---------------------------------------------------------------- gauge
  /* A semicircle meter that updates in place: the arc slides to its new value instead of being redrawn. */
  function gauge(el, o) {
    if (!el) return;
    const pctv = o.max > 0 ? clamp((o.value ?? 0) / o.max, 0, 1) : 0;
    const tone = o.tone || (pctv > 0.5 ? "good" : pctv > 0.2 ? "wait" : "bad");
    if (!el._g) {
      el.innerHTML = `<div class="gauge"><svg viewBox="0 0 160 92" role="img" aria-label="${escH(o.label || "")}">
          <path class="g-bg" d="M12 84 A68 68 0 0 1 148 84" pathLength="100"/>
          <path class="g-fg" d="M12 84 A68 68 0 0 1 148 84" pathLength="100" stroke-dasharray="100" stroke-dashoffset="100"/>
          <text class="g-v" x="80" y="70" text-anchor="middle"></text>
          <text class="g-s" x="80" y="88" text-anchor="middle"></text></svg>
        <div class="g-label"></div></div>`;
      el._g = {fg: el.querySelector(".g-fg"), v: el.querySelector(".g-v"), s: el.querySelector(".g-s"), l: el.querySelector(".g-label")};
    }
    const G = el._g;
    G.fg.setAttribute("class", "g-fg " + tone);
    requestAnimationFrame(() => G.fg.style.strokeDashoffset = String(100 - pctv * 100));
    G.v.textContent = o.text ?? fmt(o.value);
    G.s.textContent = o.sub || "";
    G.l.innerHTML = o.label || "";
  }

  // ---------------------------------------------------------------- the decision funnel, as a flow
  /* Particles stream left to right through the bot's decision steps and drop out where chances are refused.
   * Pipe widths are on a log scale - 30,000 chances and 3 trades cannot share a linear axis and both be seen -
   * and the real counts are printed at each step. Click a gap to see why chances fell out there.
   * steps: [{label, value, drop: {label, tone}}] - drop describes what falls out BEFORE this step. */
  function flow(canvas, opts = {}) {
    const S = {steps: [], parts: [], raf: 0, last: 0, visible: true, hover: -1, spawn: 0};
    const ctx = () => canvas.getContext("2d");
    let W = 0, H = 0, gx = [], half = [];
    canvas.style.touchAction = "pan-y";
    function geometry() {
      const dpr = window.devicePixelRatio || 1;
      W = canvas.clientWidth; H = canvas.clientHeight;
      canvas.width = W * dpr; canvas.height = H * dpr; ctx().setTransform(dpr, 0, 0, dpr, 0, 0);
      const n = S.steps.length;
      const top = Math.log10((S.steps[0] || {}).value + 1 || 1) || 1;
      gx = S.steps.map((_, i) => 24 + i * (W - 48) / Math.max(1, n - 1));
      // a band at the top for the counts and step names, one at the bottom for what dropped out
      const room = Math.max(10, (H - 40 - 22) / 2);
      half = S.steps.map(s => s.value > 0 ? Math.max(3, room * 0.92 * Math.log10(s.value + 1) / top) : 0);
    }
    const mid = () => 40 + Math.max(10, (H - 40 - 22) / 2);
    const halfAt = x => {                 // pipe half-height at x, interpolated between the steps
      for (let i = 1; i < gx.length; i++) if (x <= gx[i]) {
        const p = (x - gx[i - 1]) / (gx[i] - gx[i - 1]); return half[i - 1] + (half[i] - half[i - 1]) * p;
      }
      return half[half.length - 1] || 0;
    };
    function spawnOne() {
      if (!half[0]) return;
      S.parts.push({x: gx[0], y: mid() + (Math.random() * 2 - 1) * half[0] * 0.85, vx: 70 + Math.random() * 50, vy: 0,
        gate: 1, dead: false, alpha: 1, tone: null, off: Math.random()});
    }
    function step(dt) {
      S.spawn += dt * Math.min(40, 6 + S.steps.length * 4);
      while (S.spawn > 1) { spawnOne(); S.spawn -= 1; }
      S.parts.forEach(p => {
        if (p.dead) { p.vy += 260 * dt; p.y += p.vy * dt; p.x += p.vx * 0.3 * dt; p.alpha -= dt * 0.9; return; }
        p.x += p.vx * dt;
        const h = halfAt(p.x);
        p.y = mid() + clamp((p.y - mid()), -h * 0.9, h * 0.9);
        if (p.gate < gx.length && p.x >= gx[p.gate]) {
          const keep = half[p.gate - 1] > 0 ? half[p.gate] / half[p.gate - 1] : 0;
          if (Math.random() > keep) { p.dead = true; p.vy = 20; p.tone = (S.steps[p.gate].drop || {}).tone || "mut"; }
          p.gate += 1;
        }
        if (p.x > W - 10) { p.alpha -= dt * 2.5; }
      });
      S.parts = S.parts.filter(p => p.alpha > 0 && p.y < H + 10);
    }
    function render() {
      const g = ctx(), c = COLOR();
      g.clearRect(0, 0, W, H);
      if (!S.steps.length) return;
      // the pipe
      g.beginPath();
      gx.forEach((x, i) => i ? g.lineTo(x, mid() - half[i]) : g.moveTo(x, mid() - half[i]));
      for (let i = gx.length - 1; i >= 0; i--) g.lineTo(gx[i], mid() + half[i]);
      g.closePath(); g.fillStyle = rgba(c.seafoam, 0.07); g.fill(); g.strokeStyle = rgba(c.seafoam, 0.25); g.lineWidth = 1; g.stroke();
      // hovered gap
      if (S.hover > 0) {
        g.fillStyle = rgba(c.sky, 0.08); g.fillRect(gx[S.hover - 1] + 2, 0, gx[S.hover] - gx[S.hover - 1] - 4, H);
      }
      // the steps and their counts
      g.textAlign = "center";
      S.steps.forEach((s, i) => {
        const x = gx[i];
        g.strokeStyle = rgba(c.dim, 0.5); g.beginPath(); g.moveTo(x, mid() - half[i] - 6); g.lineTo(x, mid() + half[i] + 6); g.stroke();
        const tx = clamp(x, 40, W - 40);
        g.font = `600 13px ${c.num}`; g.fillStyle = i === S.steps.length - 1 ? c.seafoam : c.text;
        g.fillText(fmt(s.value, 0), tx, 16);
        g.font = `11.5px ${c.ui}`; g.fillStyle = c.dim; g.fillText(s.label, tx, 31);
      });
      g.textAlign = "start";
      // the particles
      S.parts.forEach(p => {
        g.globalAlpha = clamp(p.alpha, 0, 1);
        g.fillStyle = p.dead ? toneColor(c, p.tone) : (p.gate >= gx.length ? c.seafoam : c.sky);
        g.beginPath(); g.arc(p.x, p.y, p.dead ? 1.8 : 2.2, 0, Math.PI * 2); g.fill();
      });
      g.globalAlpha = 1;
      // drop labels under each gap
      g.font = `11px ${c.ui}`; g.textAlign = "center";
      S.steps.forEach((s, i) => {
        if (!i || !s.drop) return;
        const x = (gx[i - 1] + gx[i]) / 2, lost = S.steps[i - 1].value - s.value;
        g.fillStyle = toneColor(c, s.drop.tone); g.fillText(`${fmt(lost, 0)} ${s.drop.label}`, x, H - 8);
      });
      g.textAlign = "start";
    }
    function loop(ts) {
      const dt = Math.min(0.05, (ts - (S.last || ts)) / 1000); S.last = ts;
      if (S.visible && !document.hidden) { step(dt); render(); }
      S.raf = requestAnimationFrame(loop);
    }
    function staticFrame() {      // reduced motion: a still picture with particles already placed along the pipe
      S.parts = [];
      for (let k = 0; k < 140; k++) { spawnOne(); const p = S.parts[S.parts.length - 1]; if (!p) break; p.vx = 0;
        const target = 24 + Math.random() * (W - 48); while (p.x < target && !p.dead) { p.x += 6; step0(p); } }
      render();
    }
    function step0(p) {
      const h = halfAt(p.x); p.y = mid() + clamp(p.y - mid(), -h * 0.9, h * 0.9);
      if (p.gate < gx.length && p.x >= gx[p.gate]) {
        const keep = half[p.gate - 1] > 0 ? half[p.gate] / half[p.gate - 1] : 0;
        if (Math.random() > keep) { p.dead = true; p.y += 10 + Math.random() * 30; p.alpha = 0.6; p.tone = (S.steps[p.gate].drop || {}).tone || "mut"; }
        p.gate += 1;
      }
    }
    const gapAt = x => { for (let i = 1; i < gx.length; i++) if (x >= gx[i - 1] && x <= gx[i]) return i; return -1; };
    canvas.addEventListener("pointermove", e => {
      const r = canvas.getBoundingClientRect(), x = e.clientX - r.left;
      const i = gapAt(x); S.hover = i;
      if (i > 0) {
        const s = S.steps[i], lost = S.steps[i - 1].value - s.value;
        canvas.style.cursor = opts.onPick ? "pointer" : "default";
        tip(`<b>${fmt(lost, 0)}</b> ${escH((s.drop || {}).label || "dropped out")}<br><span class="mut">${escH((s.drop || {}).why || "")}${opts.onPick ? " Click to see the reasons." : ""}</span>`, e.clientX, e.clientY);
      } else untip();
      if (REDUCED) render();
    });
    canvas.addEventListener("pointerleave", () => { S.hover = -1; untip(); if (REDUCED) render(); });
    canvas.addEventListener("click", e => {
      const r = canvas.getBoundingClientRect(), i = gapAt(e.clientX - r.left);
      if (i > 0 && opts.onPick) opts.onPick(i);
    });
    if (window.IntersectionObserver) new IntersectionObserver(es => { S.visible = es[0].isIntersecting; }).observe(canvas);
    if (window.ResizeObserver) new ResizeObserver(() => { geometry(); if (REDUCED) staticFrame(); }).observe(canvas);
    return {
      set(steps) {
        const changed = JSON.stringify(steps.map(s => s.value)) !== JSON.stringify(S.steps.map(s => s.value));
        S.steps = steps; geometry();
        if (REDUCED) { if (changed) staticFrame(); return; }
        if (!S.raf) S.raf = requestAnimationFrame(loop);
      },
      destroy() { cancelAnimationFrame(S.raf); S.raf = 0; },
    };
  }

  // ---------------------------------------------------------------- one trade, told as a story
  /* Built from what the database actually records about a finished trade: its result in R, the furthest it went
   * in its favour (mfe_r) and against it (mae_r). It deliberately does NOT claim which of those came first -
   * the record does not say - so the picture shows how far it travelled each way, then where it finished. */
  function tradeStory(t) {
    const num = v => (v === null || v === undefined || isNaN(+v)) ? null : +v;
    const r = num(t.r_multiple), best = num(t.mfe_r), worstRaw = num(t.mae_r), money = num(t.net_pnl);
    const worst = worstRaw == null ? null : -Math.abs(worstRaw);
    const entry = num(t.entry_price), sl0 = num(t.initial_sl), tp = num(t.tp);
    const targetR = entry != null && sl0 != null && tp != null && Math.abs(entry - sl0) > 0
      ? Math.abs(tp - entry) / Math.abs(entry - sl0) : null;
    const side = String(t.direction || t.side || "").toLowerCase();
    const who = `${t.symbol || ""} ${side === "buy" ? "buy" : side === "sell" ? "sell" : ""}`.trim();
    const rule = t.strategy_id || t.strategy || "";
    const lines = [];
    if (r != null) {
      lines.push(r > 0.02 ? `It made <b class="good">${sgn(r)}R</b>${money != null ? ` (${sgn(money)})` : ""} — ${r >= 0.95 ? "at least what it risked" : "less than it risked, but a win"}.`
        : r < -0.02 ? `It lost <b class="bad">${fmt(Math.abs(r))}R</b>${money != null ? ` (${sgn(money)})` : ""}${r <= -0.95 ? " — the full amount it had put at risk." : " — less than the full amount it risked."}`
        : `It closed about even${money != null ? ` (${sgn(money)})` : ""}.`);
    }
    if (best != null && r != null) {
      if (r <= 0 && best >= 0.3) lines.push(`It was ahead by <b class="wait">${fmt(best)}R</b> at one point before it turned. When a loser was well ahead first, the exit is the part to look at.`);
      else if (r > 0 && best - r >= 0.3) lines.push(`At its best it was ${fmt(best)}R ahead, so it gave back ${fmt(best - r)}R before it closed.`);
      else if (r <= 0) lines.push(`It never got meaningfully ahead — its best was ${sgn(best)}R.`);
      else lines.push(`It closed near the best level it reached (${sgn(best)}R).`);
    }
    if (worst != null) lines.push(`Its worst moment was ${sgn(worst)}R — the stop sits at −1R.`);
    const lo = Math.min(-1.1, worst ?? 0, r ?? 0), hi = Math.max(1.1, targetR ?? 0, best ?? 0, r ?? 0) + 0.1;
    const P = v => (v - lo) / (hi - lo) * 100;
    const mark = (v, label, cl, below) => v == null ? "" :
      `<div class="ts-mark ${cl}${below ? " below" : ""}" style="left:${P(v)}%"><i></i><span>${label}<b>${sgn(v)}R</b></span></div>`;
    const body = modal(`<h3 class="ts-title">${escH(who)}${rule ? ` <span class="mut">· ${escH(rule)}</span>` : ""}</h3>
      <p class="ts-sub mut">${escH(!t.closed_at ? "" : "Closed " + (typeof tm === "function" && typeof tzLabel === "function"
          ? `${tm(t.closed_at).slice(0, 16)} ${tzLabel()}` : new Date(t.closed_at).toLocaleString()))}${t.bars_held != null ? ` · held ${escH(t.bars_held)} candle${+t.bars_held === 1 ? "" : "s"}` : ""}${t.exit_reason ? " · " + escH(typeof say === "function" ? say(t.exit_reason) : t.exit_reason) : ""}</p>
      <div class="ts-axis" aria-hidden="true">
        <div class="ts-zone bad" style="left:${P(lo)}%;width:${P(0) - P(lo)}%"></div>
        <div class="ts-zone good" style="left:${P(0)}%;width:${P(hi) - P(0)}%"></div>
        <div class="ts-travel" style="left:${P(0)}%;width:0" data-l="${P(worst ?? 0)}" data-w="${P(best ?? 0) - P(worst ?? 0)}"></div>
        ${mark(-1, "stop", "stop", true)}${mark(0, "entry", "entry", true)}${targetR != null ? mark(targetR, "target", "target", true) : ""}
        ${mark(r, "finished", "result " + (r > 0 ? "good" : r < 0 ? "bad" : ""), false)}
      </div>
      <div class="ts-legend"><span><i class="sw travel"></i>how far it travelled each way</span><span><i class="sw dot"></i>where it finished</span></div>
      <div class="ts-lines">${lines.map(l => `<p>${l}</p>`).join("")}</div>
      <p class="note">“R” is what the trade put at risk: −1R means the stop was hit, +1R means it won as much as it risked.</p>`,
      {label: "The story of this trade"});
    const tr = body.querySelector(".ts-travel"), res = body.querySelector(".ts-mark.result");
    if (res) res.style.opacity = REDUCED ? 1 : 0;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      tr.style.left = tr.dataset.l + "%"; tr.style.width = Math.max(0.5, +tr.dataset.w) + "%";
      if (res) setTimeout(() => { res.style.opacity = 1; }, REDUCED ? 0 : 750);
    }));
  }

  return {REDUCED, COLOR, rgba, fmt, sgn, escH, clamp, tween, countUp, tip, untip, modal, closeModal,
          lineChart, forest, barChart, gauge, flow, tradeStory};
})();
