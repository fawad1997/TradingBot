# The Quantara dashboard

Rewritten 16 Oct 2026 so that someone who does not trade for a living can open it and know, in one glance,
whether the bot is working and whether it is making money.

The engine keeps its technical vocabulary — `SPREAD_TOO_LARGE_VS_STOP` is still what goes in the database and
the logs, because that is a precise name and precise names matter in code. The *screen* no longer shows it.

---

## How the plain-language layer works

`app/dashboard/static/glossary.js` holds one entry per code the screen can show:

```js
SPREAD_TOO_LARGE_VS_STOP: {
  say:  "The broker's gap would eat the trade",
  mean: "The buy/sell gap is large compared with the stop distance. Trades like this lose money on costs
         alone, whatever the chart says.",
  tone: "good"
},
```

* **`say`** — what happened, in words. This is what you read.
* **`mean`** — why it matters, shown where there is room.
* **`tone`** — `good` / `bad` / `wait` / `info`, which drives the colour.

`tone` describes the **situation, not the severity of the log line.** A gate that refused a bad trade is
`good`, even though in the logs it is a rejection. That is the single most important rule in the file: the
dashboard should look calm when the bot is correctly refusing to trade, because that is the bot working.

Anything not in the glossary falls through to `humanise()`, which turns `SOME_NEW_CODE` into
"Some new code" rather than shouting an unknown token at you. **The raw code is always still there on hover**
— every translated label carries the original in its tooltip, so nothing is hidden, only moved.

### Adding a code

If you see a tooltip whose label looks like a mechanical de-underscoring, that code has no entry yet. Add one
to `GLOSSARY` in `glossary.js`. No other file needs changing.

---

## What each page answers

| Page | The question it answers |
|---|---|
| **Start here** (`/learn`, or the button on every page) | What is this bot, and how do I read this screen? |
| **Overview** | Is it working, is it making money, and why is it not trading more? |
| **Live** | What is happening this second on the symbol being watched? |
| **History** | What has it done, and what went wrong? |
| **Analysis** | Which rules actually work, and where is the money going? |
| **Settings** | What is it configured to do, and how do I change it safely? |

### The Overview, top to bottom

1. **A sentence.** Not a number — a sentence saying whether it is trading and why not, if not. Today's
   profit or loss sits beside it. This is the only thing you need to read if you are in a hurry.
2. **Account value over time**, including trades still open.
3. **Open right now** — each trade as a card, with a bar showing where price sits between the stop and the
   target. The bar always runs *losing end on the left, target on the right*, for buys and sells alike,
   so you never have to work out which way a sell is facing.
4. **Why it is not trading more** — the funnel. How many chances the bot looked at today, how many matched a
   rule, how many became trades, and then the actual reasons in plain words, biggest first. This is the panel
   that answers "why so few trades", and it is built from the real evaluation records, not an estimate.
5. **Symbols** — one tile each. Click to start or stop new trades on that symbol.
6. **Risk** and **skipped trades**.
7. **Collapsed technical detail** — system checks, news trading, self-adjusting settings, market hours,
   trading rules, order-flow measurements. All still there. None of it shouting.

---

## Notifications

The same rewrite applies to Discord (`app/notifications/trade_alerts.py`). Before:

```
CLOSE -1.00R  micro_scalper · XAUUSDm
BUY 0.07  4,138.882 → 4,135.44
P/L -18.96  ·  exit SL
held 0 bars · peak +0.02R
```

After:

```
Lost 18.96 on XAUUSDm
Bought 0.07 at 4,138.88, out at 4,135.44
The stop was hit.
That is the full amount it had put at risk.
Rule: micro_scalper
```

And when a trade was winning before it turned — the single most useful thing to know about a loss — it says
so outright: *"It was ahead by 3.65 times the risk before it turned."*

Opening messages give the money at stake rather than an R-multiple, because "1.5R" means nothing to most
people and "losing 19.80 if it goes wrong, making about 29.70 if it works" means everything.

A test (`test_no_uppercase_codes_leak_into_a_message`) fails the build if any `SHOUTING_TOKEN` survives into
a message, so this cannot quietly regress.

---

## The learning layer

Added 20 Oct 2026 so that someone who has never traded can understand the whole screen in about five minutes.
The approach comes from the animated "explainer" pieces in `github.com/yihui-dev/awesome-opus5-5-videos`:
one idea per scene, a caption that changes as the picture does, and one thing to try with your own hands. No
code or library was taken from there. Everything is plain Canvas, SVG and CSS, so it still works with the
network down.

Three controls sit in the header on every page:

| Control | What it does |
|---|---|
| **▶ Start here** | Opens the five-minute walkthrough over the current page. `#learn` in the address does the same, and `/learn` shows it as its own page |
| **Explain this page** | A spotlight walks over each panel with a sentence on what it is for. `#tour` in the address starts it |
| **Explanations on / off** | Off hides every explanatory line (`.lede`) and the dotted underlines, for an experienced reader |

The first time someone opens the dashboard, a small card offers the walkthrough once. The choice is remembered
in the browser.

**None of it can change anything.** The new files never call the control API, and a test fails the build if one
does. While the tour is running, a transparent sheet catches clicks so a symbol tile or the stop button cannot
be pressed by accident. The stop button shown in the walkthrough is only a drawing.

### The files

| File | Holds |
|---|---|
| `viz.js` | The chart pieces: the account chart (hover, drag to zoom, trade dots, deposit flags), range "forests", bars around zero, gauges, the funnel flow and the trade-story modal |
| `help.js` | Term cards (`defn("R")`), the per-page tours (`TOURS`) and the explanations switch |
| `learn.js` | The seven scenes of the walkthrough and the player that runs them |
| `glossary.js` | `TERMS`: the trading words explained for a newcomer. It also holds `funnelModel()`, the shared count of how chances drop out, used by both the Overview funnel and scene 3 |

Every page loads `glossary.js → viz.js → help.js → learn.js → app.js`, in that order.

### Adding to it

* **A word.** Add an entry to `TERMS` in `glossary.js` with a `title`, a `short` explanation and, optionally, a
  `long` one and a `pic`. Then mark the word in a page with
  `<abbr class="term" data-term="KEY" tabindex="0">word</abbr>`, or in a template with `defn("KEY", "word")`.
  A test fails if any marked word has no entry.
* **A tour step.** Add `["#elementId", "Title", "What it is for"]` to that page's list in `TOURS` in `help.js`.
  A test fails if the id does not exist on the page.
* **A scene.** Add an object to `SCENES` in `learn.js`:
  * `short` and `title`: the scene's names;
  * `secs`: how long it runs;
  * `beats`: `[[second, live => caption HTML], …]`;
  * `mount(stage, live, api)`: draws the scene and returns `{tick(t), beat(k), destroy()}`.

  `live` holds the bot's numbers. When the visitor interacts with the scene, call `api.pause()` so the caption
  does not move on.

### Going deeper

Added 29 Oct 2026. A second series of five scenes (about four minutes) for after the walkthrough. It covers what
happens between "a rule matched" and "money banked". It lives in `DEEPER` in `learn.js` and plays in the same player:
a switch at the top of the player moves between **The basics** and **Going deeper**, the last basics scene has a
**Go deeper** button, and `#deeper` in the address (or `/learn#deeper`) opens it directly. The first series stays at
seven scenes and about five minutes; a test holds both lengths.

| Scene | What it shows | Where the numbers come from |
|---|---|---|
| **How big each trade is** | Drag the stop: the trade size changes so the money at risk stays the same slice; the size is rounded down, capped at the largest size, and there is no trade when even 0.01 lots is too much | `risk.research_risk_fraction`, `max_risk_fraction_per_trade`, `max_total_open_risk_fraction`, `daily_loss_limit_fraction`, `max_volume`; the account's equity (`app/risk/sizing.py`) |
| **Locking in a winner** | The stop moving to break even, then the profit lock trailing the best price; the same path ends three ways: with the lock, break even only, stop never moved | `execution.small_win_*_r`, `ambition_*_r`, `profit_giveback_fraction` (`app/alpha/exits.py`) |
| **Reading the market's mood** | An example market labelled hour by hour with the same rule as `app/regime/detector.py`, and which rules may trade each mood | the `RegimeParams` defaults; `allowed_regimes` in `strategies.json`; the live label of the main symbol |
| **When the news hits** | The no-new-trades window around a release, which markets each currency pauses, and the news straddle filling one side and cancelling the other | `events.pre_minutes`, `event_minutes`, `currencies`; `news_straddle.*`; the next event on the live calendar |
| **Not one big bet** | Tap markets to open trades until a limit refuses one: all open trades together, markets that move together, the number open | `risk.max_total_open_risk_fraction`, `correlation_threshold`, `correlation_group_fraction`, `max_concurrent_positions` (`app/storage/repo.py`, gross mode) |

Where a picture needs a market, a price path or correlations that are not the bot's own, the scene says it is an
example. The news window shows only the minutes the engine actually blocks, before and during a release (`PRE_EVENT`
and `EVENT`). The 30 minutes after it do not block new trades, and a test fails if the scene starts reading
`post_minutes`.

### After changing a stylesheet or script

Run `.venv\Scripts\python.exe tools\bump_dashboard_version.py`. It gives every page's stylesheet and scripts a
new `?v=`, so no browser can combine new scripts with an old saved stylesheet. On 20 Oct 2026 that mix made the
new header buttons show as bare grey browser buttons. A test fails if the pages disagree on the version.

The server also sends `Cache-Control: no-cache` on pages and static files (`NoCacheStaticFiles` in `api.py`), so
a browser checks before reusing a saved copy. That costs one 304 response when nothing has changed. It takes
effect from the bot's first restart after 20 Oct 2026.

Two more safeguards need no restart:
* **The service worker** (`sw.js`) fetches every page with `cache: "no-cache"`. The worker file is never cached,
  so a change to it reaches each browser on its next visit.
* **An old saved page repairs itself.** `start()` at the bottom of `app.js` checks that the page loaded
  everything it needs. On 20 Oct 2026 an old copy of the Live page loaded the new `app.js` without `help.js`,
  failed with "HELP is not defined" and stayed blank. Now such a page reloads once, which makes the browser ask
  the server again. If files are still missing, it fetches them itself and starts. A `sessionStorage` flag stops
  any reload loop.

### Dark on purpose

Every page declares `<meta name="color-scheme" content="dark">`, and the stylesheet sets `color-scheme: dark`.
This does two things:
* The browser draws its own controls dark: dropdown lists, scrollbars, checkboxes and sliders.
* The "force dark" modes in Chrome, Edge and Samsung Internet leave the page alone. Without it they re-tint a
  page that is already dark.

Any `<button>` without a class still gets the dashboard's style, never the browser's grey default.

### Honesty rules it follows

* **Money added is not money made.** The hero, the account figures and the risk card show what trading did
  today (banked plus open) apart from any balance change no trade explains. The tolerance is the same as in
  `tools/daily_review.py`. The account chart flags each such change as "not from trading".
* **No range is drawn without the data for one.** The research chart shows a range only for results the lab
  has graded. A result that carries only an average gets a dot, never an invented range.
* **Less motion when asked.** With reduced motion switched on in the system, every animation shows its final
  frame instead.

---

## Design notes

Colours come from the Quantara mark: deep navy with a seafoam orbit. Seafoam means *running / up /
protected*, coral means *stopped / down*, sand means *waiting*. Nothing on the screen is coloured for
decoration — if something is coloured, the colour is carrying meaning.

Typography is system fonts only, deliberately: the dashboard is served from `127.0.0.1` on the trading
machine and must render identically with the network down. Numbers use a monospace face so columns line up
and digits do not jitter as they tick; labels never do, because monospaced prose is harder to read.
