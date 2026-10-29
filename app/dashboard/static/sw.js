/* Quantara dashboard service worker.
 *
 * It exists for one reason: a browser will only offer to install a page as an app
 * if a service worker with a fetch handler is registered. It is NOT here to make
 * the dashboard work offline in any meaningful sense, and it deliberately caches
 * no data at all.
 *
 * That restraint is the whole point. Every number on this dashboard — balance,
 * open positions, spread, risk state — is only useful if it is current. A cached
 * copy of a trading screen is worse than no screen, because it looks authoritative
 * while being wrong. So: API responses, WebSocket traffic, page HTML and the live
 * JavaScript all go straight to the network every time, and if the network is not
 * there the request simply fails, the same as it would without this file.
 *
 * The single exception is a plain "you are offline" page, cached as a string so a
 * failed navigation shows something readable rather than a browser error. It
 * contains no account data of any kind.
 */

const VERSION = "quantara-v1";
const OFFLINE_URL = "offline.html";

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Offline · Quantara</title>
<style>
  html,body{height:100%}
  body{margin:0;display:grid;place-items:center;background:#030F25;color:#E4ECF2;
       font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
       padding:max(24px,env(safe-area-inset-left)) 24px}
  .box{max-width:30rem;text-align:center}
  h1{font-size:1.35rem;margin:0 0 .5em;font-weight:600}
  p{margin:0 0 1em;color:#A3B4C1}
  button{font:inherit;font-size:.95rem;padding:10px 20px;border-radius:7px;cursor:pointer;
         border:1px solid #65CFC7;background:#0E2B2A;color:#65CFC7}
</style></head><body><div class="box">
<h1>Can't reach the dashboard</h1>
<p>Your phone isn't able to talk to the machine Quantara runs on. Check that Tailscale
is connected and that the computer is awake.</p>
<p>Nothing is shown here from memory on purpose — a stale trading screen is worse than none.
Trading itself is unaffected by this; it does not depend on the dashboard.</p>
<button onclick="location.reload()">Try again</button>
</div></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) =>
      cache.put(OFFLINE_URL, new Response(OFFLINE_PAGE, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      }))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  // Only navigations are handled, and only so that a failure shows the offline page
  // instead of a browser error. Everything else falls through to the network untouched.
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    // cache: "no-cache" makes the browser ask the server before reusing its own saved
    // copy of a page. Without it, a page saved before an update was shown again for
    // hours: on 20 Oct 2026 an old copy of the Live page loaded the new app.js without
    // the scripts it needs and showed nothing. An unchanged page still costs one 304.
    fetch(event.request.url, { cache: "no-cache", credentials: "same-origin" }).catch(() =>
      caches.open(VERSION).then((cache) => cache.match(OFFLINE_URL))
    )
  );
});
