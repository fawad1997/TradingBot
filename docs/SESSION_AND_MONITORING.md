# Sessions, channels and the daily routine

17 Oct 2026. The three genuine gaps from the six-point plan. The other three points — storing every
candidate, controlled experiments, and the per-trade feedback loop — were already built and running; this
covers what was actually missing.

---

## 1. Does it work better at some hours than others?

*Analysis → "Does it work better at some hours than others?"*, or `/api/analysis/sessions`.

Every finished trade, grouped by trading session: expectancy in R, total, money, win rate, average win,
average loss, how often it hit the **first** stop (never got protected), and average fill slippage. There's a
by-hour breakdown underneath for a finer grain.

Sessions were already tagged on every trade, signal and evaluation, and the backtester has reported
`by_session` for a long time. Nothing had ever grouped the **real** trades that way, so the one question
worth asking of live results had no answer on any screen.

This is the cheapest remaining idea with a real chance of finding an edge, because it filters rules that
already exist rather than inventing another one.

**It refuses to call anything a finding below 30 trades.** On your 16–17 October data it says exactly that:

```
Not enough finished trades to judge any session yet — 23 so far, and a session
needs 30 before its number means anything.

  session                trades  per trade    total    money    won  full stops  judged?
  ASIA                        2     +1.505    +3.01   +29.82   100%          0%  too few
  LONDON                      8     +0.046    +0.37    +7.61    75%         25%  too few
  LONDON_NY_OVERLAP          10     +0.243    +2.43   +16.63    70%         30%  too few
  NEW_YORK                    3     +0.427    +1.28   +15.01   100%          0%  too few
```

Two trades in Asia at +1.5R each is not evidence that Asia is good. The panel shows the number and says so.

The plan's instinct about New York is reasonable and worth testing — but the transcript it came from doesn't
establish it, and neither does this data yet. Come back when a session has thirty trades.

---

## 2. Alerts split across channels

`app/notifications/routing.py`. Five optional webhooks in `.env`:

| Variable | What lands there |
|---|---|
| `NOTIFY_URL_HEALTH` | MetaTrader, database, connection — "is it alive" |
| `NOTIFY_URL_RISK` | refused orders, risk limits, emergency stops — things to act on |
| `NOTIFY_URL_TRADES` | fills and closures, the ordinary flow of the day |
| `NOTIFY_URL_RESEARCH` | experiment results, promotion decisions |
| `NOTIFY_URL_REVIEW` | the daily summary |

Any you leave empty fall back to `NOTIFY_URL`, so **configuring none behaves exactly as one channel did** and
configuring one splits only that stream out.

**Nothing is ever lost.** A category with no webhook uses the default; a dead webhook falls back to the
default; a routing mistake of any kind still delivers the message somewhere. Worst case it reaches the log.

The sharpest bug found while building this is worth recording. Categorisation first used keywords, and
`"lost "` matched **"Lost the connection to MetaTrader"** — filing a broker outage under `#trades`, the one
category where nobody would think to look. Trade messages are now matched on the exact shape of their first
line, which is precisely what `format_open` and `format_close` emit. A message either is a trade report or it
is not; nothing is inferred.

Routing is a routing layer and nothing else — it cannot send an order, change a limit or resume trading, and
a test enforces that by walking the module's call graph. A monitoring surface that could bypass the risk
controls would be a liability dressed as a convenience.

---

## 3. Before and after the session

```
python -m app.main session check      # before trading
python -m app.main session review     # after, --hours to widen
```

**`check`** asks the running bot for the same state the dashboard renders, so it cannot disagree with the
screen, and tests the things that silently stop trades. It separates what blocks a trade from what is merely
worth knowing:

```
NOT ready: Order sending (DEGRADED) Worth noting: Buy/sell gap.

   ok MetaTrader link         HEALTHY
   ok Price feed              HEALTHY
   ok Database                HEALTHY
  !!! Order sending           DEGRADED
   ok Emergency stop          off
   ok Daily loss budget       59.20 left today
   ok Symbols running         11 of 13
    ~ Buy/sell gap            340 of 300 allowed on XAUUSDm
```

A momentarily wide spread is not a broken bot, and trading at half size is the system working as designed —
neither blocks. It exits non-zero when it isn't ready, so it drops straight into a scheduled task.

**`review`** reports realised R, money, win rate, slippage, signals not taken and why, orders the broker
refused, stop moves **we** spaced out, a per-session split, and — most usefully — any trade that was well
ahead and finished behind:

```
Session review: 23 trades, +7.09R, +69.07
78% won, average slippage 4.2 pts
Worth a look: XAUUSDm reached +3.65R and finished -1.00R (micro_scalper)
```

Both read only. A test walks the module's call graph and fails if it can `update`, `set_flag`, `insert`,
`set_config` or touch an order. A routine that quietly retuned production while reporting on it would turn
every morning check into an unlogged configuration change.

---

## What this does not do

It finds no edge. It measures better, which is the precondition for finding one but not the same thing. The
session panel is the only item here with a real chance of turning into an edge, and it currently says it
doesn't have enough data to speak — which is the correct answer and the reason to trust it when it eventually
says something else.
