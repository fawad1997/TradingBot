# Reaching the dashboard from your phone

17 Oct 2026. How to see the dashboard when you are not at the machine, and how to make it behave like
an installed app rather than a bookmark.

---

## Why not just put it on the internet

The dashboard binds to localhost and refuses to bind anywhere else. `app/config.py`:

```python
if v not in ("127.0.0.1", "localhost", "::1"):
    raise ValueError("dashboard must bind to localhost only")
```

That validator is deliberate, because **only one route checks the token**. `/api/control/*` requires
`X-Dashboard-Token`; everything else does not:

```
/  ·  /realtime  ·  /analysis  ·  /config  ·  /logs
/api/state  ·  /api/equity  ·  /api/trades/today  ·  /api/config  ·  /ws
```

No login page exists anywhere in the application. That is the correct design for something only
reachable from the machine it runs on, and the wrong design for anything reachable from the internet.

So a Cloudflare quick tunnel (`trycloudflare.com`) is not an option. Those URLs are public — no account
check, no allowlist — and anyone holding one would see the balance, equity curve, open positions, full
configuration and the live WebSocket feed, then find `close_all` and `halt` behind a single static token.
Router port forwarding is the same exposure with the router included.

**Tailscale instead.** It builds an encrypted private network between your own devices. Nothing is
published to the public internet and there is no port to scan.

---

## 1. Rotate the dashboard token first

Do this before anything else is reachable from another device.

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the result in **`.env`** — the real one, not `.env.example` — as `DASHBOARD_TOKEN=...`, then restart
the bot. If `DASHBOARD_TOKEN` is unset the dashboard generates a random one per session and logs it,
which is safe but means the control buttons stop working after every restart.

---

## 2. Install Tailscale on the PC

```
winget install Tailscale.Tailscale
```

Then sign in, with the same account you will use on the phone:

```
tailscale up
```

Confirm it is connected and note the machine name:

```
tailscale status
```

---

## 3. Publish the dashboard to your tailnet

`tailscale serve` proxies from the tailnet to `127.0.0.1:8787`. The application keeps binding to
localhost, so the validator above stays satisfied and nothing in the codebase changes.

```
tailscale serve --bg --https=8787 127.0.0.1:8787
```

The first run may ask you to enable HTTPS certificates for the tailnet; accept, it is free and it is
what provides the certificate.

Check what is published:

```
tailscale serve status
```

It prints the URL, of the form `https://<machine>.<tailnet>.ts.net:8787`. That is the address to use
on the phone.

`--bg` keeps it running across reboots. To stop publishing:

```
tailscale serve --https=8787 off
```

> **Never run `tailscale funnel`.** Funnel is the command that *does* expose a service to the public
> internet, which is the exact thing this document exists to avoid.

---

## 4. Install Tailscale on the phone

Install the app, sign in with the same account, and leave it connected in the background. Open the
`https://…ts.net:8787` URL from step 3 in the phone's browser. It works on mobile data, on someone
else's Wi-Fi, anywhere — the connection is to your tailnet, not to your house.

---

## 5. Install it as an app

The repository already carries everything needed: `static/manifest.json`, a root-scoped service worker
at `/sw.js`, the icons and the iOS meta tags.

**Android, Chrome** — menu (⋮) → *Install app* → *Install*.
**iPhone, Safari** — Share → *Add to Home Screen* → *Add*.

Either way you get a Quantara icon that opens full screen with no address bar. On Android, long-pressing
the icon offers shortcuts straight to Live, Analysis and History.

### The HTTPS detail that decides whether this works

Chrome only offers *Install app* on a secure context, and service workers are refused anywhere else.
`http://100.x.x.x:8787` — the raw Tailscale IP — is **not** a secure context, so that address gives you a
plain bookmark and the manifest is ignored. The `https://…ts.net` URL from `tailscale serve` is what makes
the install real. Use it, not the IP.

---

## What the service worker does and does not do

It exists so the browser offers to install the page. It caches **no dashboard data at all** — no API
responses, no page HTML, no WebSocket traffic. Everything goes to the network on every request.

This is not an oversight. Every figure on the dashboard is only useful if it is current, and a cached
trading screen is worse than a blank one because it looks authoritative while being wrong. The single
cached item is a plain "can't reach the dashboard" page containing no account data, shown instead of a
browser error. `tests/unit/test_dashboard_pwa.py` fails if a future change caches a network response.

If trading continues while your phone cannot reach the dashboard, nothing is wrong. The dashboard is a
window, not a dependency.

---

## Troubleshooting

| What you see | Usually means |
|---|---|
| URL does not load on the phone | Tailscale disconnected on the phone, or the PC asleep. Check `tailscale status` on both. |
| Loads but no *Install app* option | You used the `http://100.x.x.x` address. Switch to the `https://…ts.net` one. |
| Installs but shows the address bar | The manifest did not load. Open the URL in the browser and check `/static/manifest.json` returns JSON. |
| Control buttons return 403 | `DASHBOARD_TOKEN` in `.env` does not match what the browser is sending. Re-enter it on the settings page. |
| Header sits under the phone's clock | A stale cached stylesheet. Pull to refresh, or uninstall and reinstall the icon. |
| `tailscale serve` says certificates are not enabled | Enable HTTPS certificates for the tailnet in the Tailscale admin console, then re-run the command. |
