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

## Design notes

Colours come from the Quantara mark: deep navy with a seafoam orbit. Seafoam means *running / up /
protected*, coral means *stopped / down*, sand means *waiting*. Nothing on the screen is coloured for
decoration — if something is coloured, the colour is carrying meaning.

Typography is system fonts only, deliberately: the dashboard is served from `127.0.0.1` on the trading
machine and must render identically with the network down. Numbers use a monospace face so columns line up
and digits do not jitter as they tick; labels never do, because monospaced prose is harder to read.
