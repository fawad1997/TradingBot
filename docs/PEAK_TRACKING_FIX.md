# The peak-tracking fix, and what it means for older data

17 Oct 2026. Found by reading the trade timelines from 16 Oct with `tools/explain_trade.py`.

---

## What was wrong

`TradeManager.on_tick` updated the best and worst price a trade had seen, in memory, on every tick. It never
wrote them down. Only `on_bar_close` persisted them — once every fifteen minutes.

A trade makes its final run and closes **between** bar closes. That is not an edge case; it is what happens
every time price runs into a target. So the saved peak was whatever it had been up to a quarter of an hour
earlier.

The evidence that made it undeniable:

| Position | Closed at | Peak recorded |
|---|---|---|
| GBPJPYm 2069187018 | **+1.51R** (hit its target) | **+0.30R** |
| GBPUSDm (TP exit) | +1.49R | +1.47R |

A peak below the result is not imprecise. It is impossible — a trade cannot finish higher than the highest
point it reached.

The damage went past one wrong column. `Reconciler._finalise` prefers the in-memory state but falls back to
the stored value whenever that state is missing — after a restart, or when the position was briefly
unreadable at the broker. So a stale number could become the permanent record.

## What it invalidates

**Every MFE-based conclusion drawn from this database before 17 Oct could only understate.** That includes
one of mine, stated confidently and repeatedly:

> "Median MFE across the 45 losing trades was 0.04R — there was no profit to protect, so moving the
> breakeven trigger would not have helped."

That analysis was computed on numbers that could not go up. A loss that spiked to +0.8R and reversed inside
a single fifteen-minute bar would have been recorded as roughly flat. I do not yet know whether the
conclusion survives; I know it was not supported by the data I used.

**Historical rows cannot be repaired.** The ticks are gone. Any study that leans on MFE should start from
trades closed after this fix, and `mfe_r` on older rows should be read as a lower bound, never as the peak.

## The fix

`_persist_extremes` writes the extremes as soon as they move, throttled by `execution.peak_persist_step_r`
(default 0.02 — a fiftieth of the trade's own risk). A full 1R run costs about fifty writes against the
~1,100 ticks it would otherwise take. Setting it to 0 writes on every tick.

Writing is wrapped: recording how far a trade ran must never interfere with managing it.

---

## Two labelling fixes found alongside it

### Our own cooldown was blamed on the broker

`sl_modification_cooldown_sec` (5 s) spaces out stop moves. Refusals were reported with **retcode 10024,
`THROTTLED`** — which in MT5 means the *broker* is rate-limiting. The dashboard, the logs and a docstring in
`exits.py` all said Exness was throttling us.

On 16 Oct, **120 of 217 stop moves (55%) were refused this way.** One GBPUSD position attempted 65 and got
24 through. All of it was us.

The reason for the cooldown is sound — an attempt consumes the window, so a trivial nudge can block the
meaningful move behind it. Only the name was wrong. Refusals now carry `retcode 90024 / OWN_COOLDOWN`, a
code outside MT5's 10000–10046 range so it can never be confused for something the broker said.

The 5-second floor was hard-coded; it is now a floor on the *configured* value (`MIN_SL_COOLDOWN_SEC = 1.0`),
so the spacing can be shortened if measurement ever justifies it. **The default is unchanged at 5 s** — there
is no evidence yet that shortening it helps, and changing a number without evidence is how the last three
attempted improvements went wrong.

### "SL" was being read as "lost money"

`exit_reason` carries the broker's deal reason. `SL` means the stop **order** filled and says nothing about
profit. Once the ladder has trailed a stop past the entry, a winning trade also closes with reason `SL`.

On 16 Oct, **7 of 12 `SL` exits were profits** of +0.37R to +0.41R.

`exit_outcome()` now classifies by the money first:

| exit_reason | P/L | Reported as |
|---|---|---|
| SL | +5.41 | protected win (trailed stop) |
| SL | −18.96 | full loss (first stop) |
| SL | −9.00 (−0.45R) | partial loss |
| TP | +15.11 | reached target |

The losing-trades **selection** was already correct — `build_loss_rows` filters on `net_pnl < 0`, and always
did. Only the grouping and the labels were misleading. The Discord close message no longer says "The stop was
hit." on a trade that made money.

---

## What this does not change

No trading decision. The entry rules, the stops, the sizing and the gates are untouched. What changes is that
the record now says what happened, which matters because every judgement about exits was being made against a
record that quietly understated them.
