# What was built from the books, and how to check it

18 Oct 2026. Twelve findings implemented, plus self-research during market closure. Every item below is
`Resets? = no` — none of it changes what a trade does, so the live trade record is unaffected and the
200-trade readiness clock keeps running.

Verify with `python -m pytest tests/unit/test_book_findings.py -q` (54 tests).

---

## New files

| File | What it is |
|---|---|
| `app/execution/cost_audit.py` | Kissell's z-score, and Harris's three execution benchmarks |
| `app/ml/sampling.py` | Uniqueness weights, purge/embargo, triple-barrier labels |
| `app/research/lab/idle.py` | Deep research while the markets are shut |
| `tools/cost_model_audit.py` | Is the cost gate's estimate honest? |
| `tools/strategy_risk.py` | Implied Sharpe and how fragile it is |
| `tools/parameter_audit.py` | Free parameters per strategy against Chan's budget |
| `tools/lookahead_check.py` | Look-ahead bias by truncation |
| `tests/unit/test_book_findings.py` | 54 tests, grouped by source book |

## Changed files

| File | Change |
|---|---|
| `app/ml/filter.py` | Triple-barrier labels, uniqueness sample weights, purged/embargoed split; query now carries `closed_at`, `mfe_r`, `mae_r` |
| `app/research/lab/optuna_wf.py` | Purge gap before each test fold; random-search control arm; `baseline` in the result |
| `app/research/lab/data.py` | `despike()` and `despike_report()` |
| `app/research/lab/worker.py` | Records `data_quality` per symbol; runs idle research when markets are shut |
| `app/research/metrics.py` | `max_drawdown_duration()` |
| `app/reports_session.py` | Slippage in basis points and per symbol, not a pooled points figure |
| `app/config.py` | `research_lab.idle_research_enabled` |

---

## 1. The cost model can now be checked — Kissell ch.3

**The correction first.** An earlier note in `BOOK_FINDINGS.md` said `cost_r` was written as `None` and
discarded. That was wrong. `engine.py:1107` already persists both `cost_r` and `edge_r` immediately after
the cost gate runs. The historical estimates exist.

What was missing was the other half: the realised cost to compare them against, a denominator, and the
audit itself.

```
python tools/cost_model_audit.py
```

```
Z = (estimated cost − actual cost) / timing risk        should be N(0, 1)
```

Actual cost is measured from the **decision price** (`signals.entry`), not the fill price — Perold's
implementation shortfall benchmark — so it includes the round-robin wait and gate processing.

| Verdict | Meaning |
|---|---|
| `OVERESTIMATES` | The gate is refusing trades it should take. **Loosening it is justified by measurement.** |
| `UNDERESTIMATES` | Costs are understated; some trades that passed should not have |
| `UNSTABLE` | Unbiased but far less reliable than it claims |
| `HONEST` | Estimates match outcomes within noise |
| `TOO_FEW` | Under 30 fills. Refuses to judge. |

**One honest limitation.** Timing risk should come from price volatility over the horizon. ATR is not
stored on the signal row, so rather than invent a number the tool uses the *empirical* dispersion of
actual costs per symbol. That is a defensible denominator but it makes the variance test partly circular
— **read the mean of z, not its variance.** The docstring says so too.

**A guard found by running it.** On a synthetic set where realised costs barely varied, the empirical
timing risk collapsed toward zero and the mean z came back as **+174.9** — which looks like
overwhelming evidence and is an artefact of dividing by almost nothing. There is now a
`DENOMINATOR_TOO_SMALL` verdict for any `|mean z| > 10`, which says to compare the raw estimated and
actual costs directly instead of trusting the ratio. This is exactly the failure that would otherwise
have produced a confident, wrong recommendation to loosen the cost gate.

## 2. The ML filter's statistics — López de Prado ch.3, 4, 7

Three changes in `app/ml/filter.py`, with the machinery in `app/ml/sampling.py`.

**Uniqueness weights.** Thirteen symbols run concurrently with up to ten open positions, so trade
outcomes overlap heavily and the rows are not independent draws. Each row is now weighted by
`1/concurrency`, rescaled to average 1.0 so the weighting does not silently change regularisation
strength as well.

`status["sampling"]` now reports `effective_sample_size` — the number that actually matters for any
sample-size rule, and usually far below the row count.

**Purged and embargoed split.** Training rows whose outcome window overlaps the test window are dropped,
plus those immediately after it. If too few survive, training stops with `INSUFFICIENT_AFTER_PURGE`
rather than fitting on a leaky remainder.

**Triple-barrier labels.** `y = (r > 0)` replaced with which barrier was touched first, from `mfe_r` and
`mae_r`. Falls back to the sign rule where path data is missing. `labels_changed_vs_sign_rule` reports
how many labels actually moved — if zero, the change bought nothing on this data and says so.

## 3. The tuner now has a control group — Liu ch.8

`optuna_wf.py` runs a `RandomSampler` arm with the same trial budget on the same training window, trades
the same test window, and reports:

```json
"baseline": {"tuned_expectancy_r": ..., "random_expectancy_r": ..., "tuner_beat_random": true}
```

If `tuner_beat_random` is false, the TPE search is fitting noise and every parameter it has produced is a
coincidence. There was previously no way to tell.

The same function also gained the **purge gap** before each test fold.

## 4. Slippage is now a comparable quantity — Kissell ch.3

`"average slippage 4.2 pts"` pooled gold with EURUSD. Those are different units and the average meant
nothing.

`session review` now reports `avg_slippage_bp` (basis points, comparable across instruments) and
`slippage_by_symbol` (raw points, where they are meaningful). The phone message prefers basis points and
falls back to points with an explicit note.

`app/execution/cost_audit.py` adds Harris's three benchmarks — effective spread, realized spread,
implementation shortfall — and the two derived numbers worth watching:

- `adverse_selection_bp` = effective − realized — how far price kept moving against the fill
- `delay_bp` = shortfall − effective — the cost of the wait between decision and fill

## 5. The measurement tools

```
python tools/strategy_risk.py        # implied Sharpe, break-even precision, fragility
python tools/parameter_audit.py      # free parameters vs Chan's budget of five
python tools/lookahead_check.py --m15 <csv>    # look-ahead bias by truncation
```

### What the parameter audit already found

Run against the live config, **every strategy is over Chan's five-parameter budget**:

```
  !!! pullback_v1          32 free parameters    needs 8064 bars
  !!! mean_reversion_v1    22                    needs 5544
  !!! session_breakout_v1  11                    needs 2772
  !!! micro_scalper        10                    needs 2520
  !!! breakout_retest_v1    9 · momentum_trend_v1 9 · htf_continuation_v1 9 · vwap_reversion_v1 7
```

`pullback_v1` and `mean_reversion_v1` are the two full-size (`risk_x 1.0`) strategies and carry 32 and 22
tunable parameters. By Chan's rule that is overfitting by construction, whatever the walk-forward says,
because the walk-forward is itself choosing among too many degrees of freedom.

This is the clearest actionable result of the whole exercise and it needs no new data.

**A note on how this nearly went wrong.** The first version counted only Python `int`/`float` values. Every
config value arrives as a `ParamSpec`, so it found one parameter per strategy and reported everything as
fine — wrong *and* reassuring. It now uses the config's own declaration: a `ParamSpec` with both
`minimum` and `maximum` is tunable, one without is a fixed setting. A test pins that behaviour.

## 6. Bad-tick filtering — Chan ch.7

`despike()` in `lab/data.py` flags implausible jumps using the median absolute deviation of recent log
returns (a standard deviation would be inflated by the spike it is meant to catch). The lab records
`data_quality` per symbol beside each verdict.

Threshold is 8 sigma, deliberately permissive: real markets gap, and a filter tight enough to catch every
bad print would discard genuine moves and bias the research the other way.

This matters for mean-reversion specifically — a bad print makes a reversion strategy look brilliant,
which silently flatters `mean_reversion_v1` and `vwap_reversion` over the rest.

## 7. Drawdown duration — Chan ch.2

`max_drawdown_duration()` in `metrics.py`. Depth is what the risk tiers watch; duration is what makes
people switch a working system off. A drawdown still open at the end counts, since that is the one that
matters most.

---

## 8. Self-research when the markets are shut

`app/research/lab/idle.py`, wired into the lab loop. Enabled by
`research_lab.idle_research_enabled` (default on).

The FX week closes Friday ~21:00 UTC and reopens Sunday ~22:00 UTC — about **49 hours** of an idle
machine with four years of history on disk.

**How "closed" is detected.** By broker quote age from `system_health`, the same ground truth the trading
core uses — a shut market stops quoting, whereas a calendar is wrong on every holiday. The weekend
estimate is only a fallback.

**What it runs**, cheapest and most decisive first, so a window cut short still pays:

| Window | Jobs |
|---|---|
| any | data quality, cost model audit, strategy risk, parameter audit |
| ≥ 3 h | + ML refit (uniqueness weights, purged splits) |
| ≥ 6 h | + extended walk-forward *(hook reserved)* |
| ≥ 10 h | + full symbol sweep *(hook reserved)* |

Results are written to `research_lab_runs` with `kind = "IDLE_DEEP_RESEARCH"`.

**Safety.** It runs in the existing low-priority spawned process, shares nothing with trading but the
database, and will not start expensive work with under 90 minutes to the open. A failing job is recorded
and the rest continue. `TestIdleResearchOnlyMeasures` walks the module's call graph and fails if it ever
gains the ability to promote a strategy, change config or touch an order.

The last two jobs are deliberate placeholders — the scheduling, safety and reporting are built and
tested, but the extended searches themselves are not yet written. They return a note saying so rather
than pretending to have run.

---

## What was not built, and why

`LATER` in the backlog, all of them `Resets? = yes` — they change what a trade does and would restart the
23-trade record:

- **#9 + #10** half-life time stops replacing price stops on the reversion strategies
- **#15** limit orders for spread-gated signals (also blocked on #16 collecting data first)
- **#22** cost forecast as a distribution (blocked on #20's verdict)
- **#11** multi-strategy Kelly (needs a few hundred trades per strategy)
- **#17, #18, #14** volatility-regime split, volatility-predicted spread ceiling, cointegration

**#13** activity-based bars is the only `NO`: it would touch every indicator and strategy, and retail FX
gives tick volume rather than true volume.

---

# Logo refresh — 18 Oct 2026

The Quantara mark was replaced with the supplied artwork. The Q ring is now the system's mark
everywhere, and the palette is sampled from the artwork rather than picked by eye.

| Asset | Size | Where it shows |
|---|---|---|
| `quantara-logo.png` | 256 | dashboard header |
| `favicon.png` | 64 | browser tab |
| `icon-192.png` | 192 | installed app icon, app shortcuts |
| `icon-512.png` | 512 | install prompt, splash |
| `icon-maskable-512.png` | 512 | Android adaptive icon, art inside the 78% safe zone |
| `apple-touch-icon.png` | 180 | iOS home screen |
| `quantara-wordmark.png` | 640 | full lockup, for anywhere the wordmark is wanted |

All generated from the mark alone, not the full lockup — at 192px a wordmark is unreadable and the
ring is not.

## Palette

Both values are sampled from the artwork so the mark sits on the header with no seam around it:

| Token | Was | Now |
|---|---|---|
| `--ink` | `#0B1826` | **`#030F25`** |
| `--seafoam` | `#52B295` | **`#65CFC7`** |

Surfaces (`--surface`, `--raised`, `--rule`, `--rule-soft`) were re-stepped from the new ground in even
lightness increments rather than nudged by eye. `--seafoam-dim` moved to `#2F7E78` to stay in the same
relationship to the new accent.

Updated in: `style.css`, all five page `theme-color` metas, `manifest.json`, and the offline page inside
`sw.js`, which carries its own inline palette.

The two PWA tests that pin the theme colour to the logo were updated to the new value — they failed on
the change, which is what they are for.

---

# External review — 18 Oct 2026

An outside reviewer read the Field Manual and produced a P0/P1 list. Their claims were checked against the
code rather than accepted or dismissed. Four were confirmed, two were wrong, and several asked for things
that already existed. This section records the verdict on each and what was built in response.

## Confirmed, and fixed

### 1. The research gate fails open — their P0-4, the most serious finding

`app/research/lab/gate.py` `evaluate()` had eight return paths and **only one of them blocked**: a complete,
fresh, sufficiently large result whose pooled expectancy was below the limit. Gate off, no result, stale
result, incomplete numbers, too few backtest trades — all permitted the trade. A **ninth** path turned up
while fixing it: `TradingCore.research_gate()` catches any exception and returned `blocked: False`, which is
the reviewer's "database errors" case. The gate answered *"has research proved there is no edge?"*, never
*"has research proved there is one?"*

The Field Manual claimed it "refuses the signal if the verdict is negative **or has gone stale**". That
sentence was false: stale permitted.

Two changes:

- Every result now carries an **`evidence`** field — `OK`, `GATE_OFF`, `MISSING`, `STALE`, `INCOMPLETE` or
  `THIN` — so a permit says on what basis it permitted instead of passing silently.
- **`research_lab.gate_require_evidence`** (default `false`) inverts the policy. With it on, every
  no-evidence state blocks with `RESEARCH_LAB_NO_EVIDENCE` — including a result that carries no verdict at
  all, and an exception inside the gate itself. A verdict that is merely *not* `NOT_ROBUST_ON_TEST` (say
  `ROBUST`) still permits: the lab looked and did not condemn it, which is evidence.

The default stays fail-open **deliberately**. The lab is still being populated; flipping it now would
silence every symbol without a fresh result and stall the 200-trade clock. `tools/live_readiness.py` now
*requires* the flag before a real-money account counts as ready, which is the point at which "I don't know"
must mean "don't trade".

### 2. The adaptive spread cap never expired — found while disproving their claim C

Their stated mechanism was wrong (see below), but checking it exposed a real defect. `AdaptiveState.apply()`
returns early when a refresh cannot run, and `restore()` reloads the cap after a restart — neither checks
age, and the consumer never read `updated_at`. A tight ceiling computed in one quiet hour could therefore
outlive the conditions that produced it, and survive a weekend restart.

`AdaptiveState.effective_max_spread_points(max_age)` now discards a cap older than
`adaptive.max_spread_cap_age_minutes` (default 180) or one with no usable timestamp, and the spread gate
calls it instead of reading the field directly. The gate's `basis` string now reports the cap's age, so a
stale ceiling is distinguishable from a live one in every rejection.

### 3. `max_armed_symbols = 13` on 13 symbols — their claim B

A ceiling equal to the population refuses nothing. It was documented as preventing round-robin starvation
and could not do so.

The value itself is **not changed** — lowering it would alter trade behaviour and restart the trade record,
which is the operator's call. Instead it is now impossible to miss: `NewsStraddleCfg.ceiling_binds()` says
whether the ceiling can refuse anything, a startup validator logs a `WARNING` when it cannot,
`tools/doc_facts.py` prints it, and `tools/live_readiness.py` fails on it before real money.

### 4. "Fourteen checkpoints" — their claim F

`STARTUP_SEQUENCE` has **13** entries; the `StartupState` enum has **15** members, the difference being
`STOPPED` and `FAILED`, which are exits rather than steps. The documented "fourteen" matched neither.

Their recommended fix was the right one — generate the number instead of typing it. **`tools/doc_facts.py`**
prints the startup counts, trade and risk state counts, risk limits, strategy table, straddle ceiling,
research-gate policy and commission setting straight from the code and live config.
`tests/unit/test_review_fixes.py` fails if the two counts drift.

## Checked and wrong

**Claim C — "the adaptive ceiling may only ratchet downward."** It does not. `compute()` recalculates the
cap from scratch each cycle off the recent spread distribution, and `apply()` **assigns** it rather than
taking a minimum with the previous value. When spreads widen the cap widens with them, and reverts to `None`
— the global ceiling — once it is no longer tighter. "Tighten only" means *tighter than the global ceiling on
this cycle*, not monotone decreasing. Two tests pin the recovery behaviour. Their suggested test plan is
still what found the staleness bug above.

**Claim A — "cap risk across correlated positions" and "never round volume upward."** Both already existed.
`app/risk/correlation.py` computes rolling H1 correlations and sizing applies a group cap via
`risk.correlation_group_fraction` and `risk.correlation_threshold`. Sizing never rounds up: below the broker
minimum it returns `POSITION_SIZE_INFEASIBLE` and refuses the trade.

**Claim B's execution half.** `app/execution/straddle.py` already runs OCO, counts consecutive cancel
failures, re-checks whether a "vanished" order actually triggered, cancels leg one when leg two cannot be
placed, and stands down on a pre-release spread shock.

**Claim P0-2 — "the cost model omits commission, swaps, slippage."** It does not; `app/edge/cost.py` models
all of them, taking swap direction and mode from the broker's own symbol specification. The live question is
narrower: `commission_per_lot_round_trip = 0.0` is correct on an Exness **Standard** account and wrong on
**Raw Spread / Zero**, where every backtest and every cost-gate decision would understate the true cost.
`tools/doc_facts.py` and `tools/live_readiness.py` now both flag the zero so it cannot pass unnoticed.

## Right, and already known

P0-1 (no validated edge) is correct and **understates** the problem — the 119-trade record is **−3.98R**, not
the +7.09R over 23 trades the manual quotes. P0-3 (overfitting) is correct, and `tools/parameter_audit.py`
quantifies it: `pullback_v1` carries 32 free parameters against Chan's budget of five. P1 (exposed
credentials) is correct and **still outstanding**. E (the ML component has not demonstrated value) is correct
and already the case — it is record-only.

P0-5, D and the profit-protection item are not decidable by reading code. They are test plans, and good ones.

## Settings that must change before real money

`python tools/live_readiness.py` now fails on all three against the current configuration:

| Check | Now | Needed |
|---|---|---|
| Research gate fails closed | `gate_require_evidence=False` | `true` |
| News straddle ceiling binds | 13 symbols, ceiling 13 | ceiling below the symbol count |
| Commission matches account type | `0.0`, no overrides | confirmed against the live account |

---

# Research for all eight strategies — 18 Oct 2026

## The gap

`gate_strategies` was `["micro_scalper"]`. The research lab studied one strategy out of eight. The other
seven — `pullback_v1`, `mean_reversion_v1`, `breakout_retest_v1`, `momentum_trend_v1`,
`session_breakout_v1`, `vwap_reversion_v1`, `htf_continuation_v1` — traded with **no research behind them
at all**. Not blocked by a fail-open policy: never examined.

This was never an architectural limit. Every strategy implements the same contract, and
`app/research/backtester.py` already replays any of them against it, **H4 included** — which the
micro_scalper fast path explicitly cannot do (*"the H4 macro channel is NOT replayed"*). The lab simply
took a private shortcut written for one strategy's candle trigger.

## What was built

**`app/research/lab/strategy_research.py`** drives the existing generic backtester per strategy per symbol
and shapes the output into the schema `lab/gate.py` already reads, so one gate serves both sources.

**No parameter sweep, and that is the design.** The micro_scalper path sweeps a stop/reward grid and keeps
the best on the training window. Doing that for 8 strategies × 13 symbols would be **104 cells of
searching**, and picking winners from 104 noisy cells manufactures findings that die on a live account —
the central warning in Chan ch.3 and López de Prado ch.11. So each strategy runs at its **live configured
parameters** and nothing else. The lab chooses nothing, so no selection bias can inflate the result. A
test asserts the module never imports `optuna`, `run_sweep`, `vbt_sweep` or `walk_forward`.

**What train/test means here, honestly.** With no parameters fitted, the split is *not* out-of-sample
validation — there is no in-sample fitting for it to validate. It is an out-of-time **consistency check**:
did the second period resemble the first, or did the result come from one unrepeatable stretch? That is a
weaker claim than the micro_scalper sweep makes, and it is the accurate one. Trades straddling the split
are purged, plus a 24-hour embargo after it.

## Nothing newly blocks

| | Before | After |
|---|---|---|
| Strategies researched | 1 | **8** |
| Strategies that can block a trade | 1 | **1** |

`research_all_strategies` defaults **on**; `gate_strategies` stays `["micro_scalper"]`. Research is
recorded and visible for all eight, and a strategy only becomes blockable when you add it to
`gate_strategies` — which changes live behaviour and restarts the trade record, so it stays your decision
after reading what the research says.

Per-strategy research replays the full M15/H1/H4 path, so it is far slower than the micro_scalper fast
path. `research_all_in_normal_cycle` is **off**; the weekend idle window runs it
(`all_strategy_research`, scheduled once ≥120 minutes are available, before the expensive searches
because it needs no searching).

## Wiring

- `TradingCore.research_gate(strategy_id)` — micro_scalper reads `lab_hint` exactly as before, everything
  else reads `strategy_lab_hints`. A strategy can no longer inherit another's verdict.
- The alert loop now reports each gated strategy's own verdict instead of relabelling one verdict for all.
- `_research_blocked_last` became a per-strategy dict, tolerating the old bare-bool state on upgrade.
- The dashboard's `research_block` lists the strategies actually blocked, each with its own reason.
- `store.py` persists a `strategies` block keyed `{strategy_id: {symbol: entry}}` plus verdict counts.

## `tools/live_breakdown.py`

For any strategy that has already traded, **live evidence beats backtest evidence** — the lab replays
history under modelled costs, this reads what the account really did.

```
python tools/live_breakdown.py --min-trades 10
```

Per strategy and per strategy+symbol, with a 95% interval on every row and a `NOT_PROVEN` verdict whenever
that interval contains zero, however good the mean looks. On the shape of the 119-trade record it reports
`micro_scalper` as the only cell clearly negative and everything else as not proven in either direction —
which is the honest reading of that sample.

Its regression test encodes the founding error of the project: 23 trades, 78% won, mean +0.33R, interval
spanning zero → `NOT_PROVEN`.

---

# Each strategy judged on its own bar — 18 Oct 2026

## The flaw

Extending research to all eight strategies fixed *what was studied* but not *how it was judged*. Every
strategy was held to the same `gate_min_trades = 30` and the same `gate_block_below_r = -0.3R`. The
strategies are not comparable to each other, so one shared bar is wrong in two directions at once.

**Trade frequency.** On identical history the eight produced wildly different trade counts:

| Strategy | Train trades on the same 4000 M15 bars |
|---|---|
| `breakout_retest_v1` | 34 |
| `momentum_trend_v1` | 21 |
| `pullback_v1` | 9 |
| `mean_reversion_v1` | 5 |

With a shared bar of 30, the first is judged and the last three are **permanently unjudged**. That reads
as caution and is actually a blind spot — a strategy that never reaches the bar never gets a verdict, so
it can never be blocked no matter how it performs.

**Outcome dispersion.** A −0.3R average is a catastrophe for a scalper taking many small trades, and
ordinary variation for a strategy taking few large ones. One line cannot mean the same thing for both.

## The fix

`StrategyGateCfg` in `research_lab.gate_overrides`, per strategy: `block_below_r`, `min_trades`,
`max_age_hours`, `require_evidence`, `unseen_alone`, plus a `note` saying why this strategy's bar differs.
**Every field is optional and falls back to the global value independently**, so setting one does not
silently reset the others, and an empty map reproduces today's behaviour exactly.

`gate.Thresholds` resolves the bar for one strategy and is now the only path by which the gate reads a
threshold — no `cfg.gate_*` read survives in the decision code. Every result carries a `thresholds` block,
so a block can always be traced to the bar that produced it. The per-strategy research uses the same
resolution for its own `min_trades`, so a strategy is studied and judged against one consistent bar.

Demonstrated on identical research (40 trades at −0.5R per trade):

| Strategy | Its bar | Outcome |
|---|---|---|
| scalper | 100 trades, −0.15R | `THIN` — 40 is below its own trade bar |
| slow | 12 trades, −0.80R | **permitted** — −0.5R is inside its own tolerance |
| ordinary | global 30, −0.30R | **blocked** |

## `tools/gate_thresholds.py`

Thresholds are set from each strategy's **own** record, never from a constant:

- **`min_trades`** scaled by how often the strategy trades relative to the busiest one, so every strategy
  reaches a verdict after comparable *calendar time* rather than a comparable trade count. Floored at 10.
- **`block_below_r`** scaled by the strategy's own standard deviation of R relative to the median
  strategy. Results that naturally swing twice as wide need a line twice as far from zero before a bad
  run counts as proof of no edge.

A strategy with fewer than 5 live trades gets **no recommendation at all** — inventing a threshold from 3
trades is worse than leaving the default. Output carries a `confidence` grade and the reasoning for every
number, plus a ready-to-paste config block.

## Safety

`gate_overrides` ships **empty**. Nothing changes until an override is set. A test asserts that verdicts
with and without a `strategy_id` are identical while the map is empty.

`gate.FALLBACKS` duplicates the config defaults so that resolving a threshold can never raise — an
exception inside the gate is caught upstream and becomes "not blocked", which would be a silent
fail-open. A test pins `FALLBACKS` against `ResearchLabCfg` so the two copies cannot drift.

`tools/doc_facts.py` now prints every strategy's bar and marks each one still on the global default, so
the shared-bar problem is visible in the system's own report instead of having to be noticed by a reader.

---

# Portfolio risk and portfolio measurement — 18 Oct 2026

## Two defects in how open positions were added up

**1. Risk was summed arithmetically.** `existing_total = sum(exposure.values())`. Two uncorrelated 1%
positions consumed 2% of the budget even though their combined risk is about 1.41%, so the second
strategy to produce a signal was refused on budget the first was not really using. A second signal was
automatically independent risk.

**2. The correlated-group check used `abs(correlation)`.** A correlation of −0.9 (positions that partly
offset) was treated exactly like +0.9 (positions that double up). Direction was never consulted at all,
so the model could not tell the two apart.

## The model: `app/risk/portfolio.py`

Portfolio variance over the risk amounts, with direction folded in as a sign:

```
combined² = Σᵢ Σⱼ rᵢ · rⱼ · pair(i,j)       pair(i,j) = max(dᵢ·dⱼ·ρᵢⱼ, floor),  pair(i,i) = 1
```

| Book | Gross | Combined | Credit |
|---|---|---|---|
| 2 × 1.0, perfectly correlated, same direction | 2.00 | **2.00** | 0% — *unchanged from before* |
| 2 × 1.0, uncorrelated | 2.00 | **1.41** | 29% |
| 4 × 1.0, uncorrelated | 4.00 | **2.00** | 50% |
| 2 × 1.0 at ρ=+0.9, same direction | 2.00 | 1.95 | 3% |
| 2 × 1.0 at ρ=+0.9, **opposite** direction | 2.00 | **1.41** | 29% |
| 2 × 1.0, **no correlation history** | 2.00 | **2.00** | 0% |

It is identical to the old arithmetic sum in the one case the old sum was right about, and more
permissive exactly to the extent the positions are genuinely independent.

### Why the floor is zero

Taking hedge credit at face value is how portfolio risk models kill accounts. A pair that has been −0.9
correlated for months can go to +1 in the hour that matters, and both legs can be stopped out in one
whipsaw even when the instruments really are opposed. `diversification_floor` defaults to **0.0**: full
credit for being *uncorrelated*, none at all for being *negatively correlated*. Low correlation is a
measurable property of the data; a hedge holding through the event you need it for is a hope.

An unknown pair is assumed **fully correlated** (`missing_correlation_value = 1.0`), so missing history
never earns a benefit the data cannot support.

### Three ceilings, not one

Diversification only ever loosens, so it is bounded on both sides:

1. **diversified** — the model above, against the normal budget. The check that permits more.
2. **gross** — the plain arithmetic sum against `max_gross_open_risk_fraction` (default 0.20). However
   well diversified the book looks, total money at risk stays bounded. This is the backstop for the
   correlation model simply being wrong.
3. **stressed** — the model again with every pair raised to `stress_correlation` (default 0.80), which
   is what a crisis does to a correlation matrix. A book that only fits while markets are calm does not
   fit.

A position is admitted only if all three pass, and each refusal names the ceiling and both figures.

### Switching it on

`risk.portfolio_risk_mode` defaults to **`"gross"`** — the existing behaviour, byte-identical. Setting
it to `"diversified"` is what permits simultaneous trades, and it **changes live trade behaviour, so it
restarts the trade record**. A config validator refuses a gross ceiling below the ordinary budget, which
would make the new mode stricter than the old one.

## `tools/portfolio_report.py`

A per-strategy win-rate table answers the wrong question. A 70% strategy that loses on exactly the days
everything else loses is worth less than a 45% strategy that wins when the rest are down, and no column
in a per-strategy table can show that.

- **Total net R** — the only figure that pays, with a 95% interval and an explicit callout when it spans
  zero.
- **Drawdown** — depth and duration, counting a drawdown still open at the end.
- **Correlated losses** — how often two or more strategies lost in the same day or week, and which pairs
  do it most, as a rate over the periods both actually traded. Above 50% the report says plainly that
  the strategies are less independent than their count suggests.
- **Marginal contribution** — leave-one-out. `marginal_r` is the strategy's own total; the number that
  is *not* obvious is `drawdown_delta`. A strategy can lose money overall and still have made the worst
  stretch shallower by winning while the others lost. The verdict threshold scales with the drawdown
  being measured (5%, floor 0.25R), so a 0.04R shift in a 15R drawdown is not reported as a finding.

---

# The changes made visible on the dashboard — 18 Oct 2026

Every defect found in this round had the same shape: **a setting that silently did nothing while reading
like a working control.** A straddle ceiling equal to the number of symbols. A research gate that
permitted whenever evidence was missing, while the manual said it refused. One evidence bar shared by
eight rules that behave nothing alike. A spread ceiling that could not expire. A commission of zero that
is right on one Exness account type and wrong on another.

None of them announced itself. That is the argument for putting them on screen: not more numbers, but
the specific settings whose silence was the problem.

## Settings page — "How strictly it is judging itself"

New endpoint `/api/integrity`, rendered by `loadIntegrity()`. Against the current configuration it reads:

| | |
|---|---|
| Rules researched | 8 of 8 |
| Rules that may refuse a trade | micro_scalper |
| Researched, cannot refuse | 7 rules |
| When research is missing or stale | **allow the trade** |
| Evidence bar per rule | **the same for every rule** |
| Several trades at once | **each counted as separate risk** |
| News trades armed at once | **13 of 13 — limits nothing** |
| Commission per lot | **zero — correct only on an Exness Standard account** |
| Hourly spread limit expires after | 180 min |

Anything not actually restricting something is marked, in the sand warning colour, with what it would
take to make it bind. The sub-card **"Before real money"** renders
`tools.live_readiness._config_checks` — the same three checks the CLI runs, each with its reason — so
the dashboard and the command line cannot disagree.

## Analysis page — "How the whole book is doing"

New analysis kind `portfolio`, reusing `tools.portfolio_report.build`, rendered by `loadPortfolio()`:

- **Total net R** with its 95% interval, and an explicit red note when that interval crosses zero —
  "the headline figure is consistent with having none".
- **Drawdown** depth, longest stretch, and whether it is still open.
- **Correlated losses** per day or week, with the plain statement when most losing periods hit several
  rules at once: *they are less independent than their number suggests, and any risk budget that treats
  them as separate is sized on an assumption that does not hold.*
- **Marginal contribution** per rule — its own R, the book without it, the drawdown without it, and a
  verdict. "Cushions drawdown" marks a rule that may lose money overall and still be worth keeping
  because it won while the others lost, which no per-rule table can show.

## Existing panels that now carry the new diagnostics

- **Spread row**: added *"Where that limit comes from"* — which of the three ceilings is binding and,
  for the adaptive one, how old it is. The reviewer asked that every rejection name its threshold; it
  does now, on screen as well as in the log.
- **Research block**: shows each blocked rule's **own reason** rather than one verdict relabelled for
  the whole gate list.
- **`/api/research/lab`**: now carries `per_strategy`, every rule's research evaluated through the gate
  against **its own thresholds**, plus `verdict_counts`.

`tests/unit/test_dashboard_integrity.py` (22 tests) covers the endpoints, and asserts the pages contain
the anchors and that `app.js` defines and calls the renderers — a renderer nothing calls is the same as
no renderer.

---

# Answering "it got worse after the changes" — 19 Oct 2026

A losing day after a deploy is the easiest wrong conclusion in this whole project to reach, and the
hardest to argue someone out of without data. `tools/deploy_impact.py` compares equal windows either
side of a deploy, from the account's own record.

## What it checks

**1. Results.** Net R, per-trade expectancy and win rate each side, with Welch's standard error on the
difference. It reports `NOT_CONCLUSIVE` for anything inside two standard errors, and refuses outright
when either side has fewer than 20 trades. It will answer `NOT_CONCLUSIVE` most of the time, and that is
the honest answer rather than a failure: a few trades each side cannot distinguish a change from a run
of luck, and a tool that pretended otherwise would be worse than none.

**2. Did the spread gate loosen?** The one October change that could admit more trades. Its signature is
spread refusals falling **and** accepted spreads widening **together** — either alone is ordinary market
variation and is explicitly not reported as a signature.

**3. Could the change have mattered at all?** The decisive check. The adaptive refresh runs hourly and
the cap now expires at three hours, so **three consecutive failed refreshes** are required before the
change alters anything. Every cycle is already recorded in `adaptive_param_history` with `updated` and
`reason`. Where that run never happened, the tool says so plainly: *the October build did not touch your
spread gate at all.* Consecutive failures are counted per symbol, so two symbols failing once each is
not mistaken for one symbol failing twice.

## Why this one is worth having

Of everything shipped in this round, exactly one change could alter a live trading decision:

| Change | Live effect |
|---|---|
| `gate_require_evidence` | off — gate unchanged |
| `gate_overrides` | empty — every rule on the old shared bar |
| `gate_strategies` | still only `micro_scalper` |
| `portfolio_risk_mode` | `gross` — unchanged summing |
| per-strategy research | weekend only, blocks nothing |
| **adaptive spread cap expiry** | **the one exception — and it makes the gate LOOSER** |

Everything else was measurement and visibility. Naming the single exception, and giving a way to test
whether it fired, is more useful than asserting that nothing changed.

---

# micro_scalper disabled, research gate tightened — 19 Oct 2026

## 1. micro_scalper off

| | Observations | Per trade | Won |
|---|---|---|---|
| Live | 198 | **−0.472R** | 32% |
| Shadow (paper) | **2,642** | **−1.023R** | 24% |

~2,840 observations. Not a selection effect and not a small sample: a strategy does not lose 1.02R per
signal across 2,642 paper signals by luck. A 24% win rate against a ~1.5:1 target means the entry
triggers are wrong about direction three times in four.

The portfolio **without** it is **+8.22R**; with it, **−85.27R**. The strategy-health monitor had already
been restricting it on its own — most "STRATEGY HEALTH RESTRICTED" refusals in the blocked-signal log
were micro_scalper. Disabling it finishes what the system had started.

`config/strategies.json`: `enabled: false`, with `disabled_reason` recording the evidence.

## 2. `gate_block_below_r` −0.30 → −0.05

The old limit permitted every one of these measured losses:

```
AAPLm   -0.29R per trade   allowed - "above the -0.30R limit"
MSFTm   -0.25R             allowed
GBPUSDm -0.18R             allowed
BTCUSDm -0.09R             allowed
```

A strategy losing 0.29R per trade at twenty trades a day burns 5.8R daily. The gate called that fine.
The −0.30 figure was set describing the gate as a veto on "proven absence of edge"; that calibration was
wrong — it caught catastrophes and waved through the merely ruinous. On *unseen* data any reliable
negative is a reason not to trade.

Replayed against the 19 Oct lab run, the new limit blocks **5 of 11 symbols** that were previously
allowed: AAPLm, MSFTm, BTCUSDm, EURUSDm, GBPUSDm. (EURUSDm is pooled +0.02R but its unseen window is
−0.10R over 93 trades, so the unseen-alone rule refuses it.)

## The catch: the new limit is currently dormant

`gate_strategies` is `["micro_scalper"]`, and micro_scalper is now off. **No enabled strategy is gated,
so no research verdict can refuse anything.** Tightening the number changes nothing until either a gated
strategy is enabled again, or the other strategies are added to `gate_strategies` — and the second only
helps once per-strategy research exists for them to be judged against.

## 3. `research-lab --all-strategies`

Per-strategy research previously ran only in the weekend idle window, which is five days away. The CLI
can now populate it on demand:

```
python -m app.main research-lab --all-strategies
```

That is the prerequisite for adding the remaining strategies to `gate_strategies`.

---

# Leaving switched-off rules out of the session tables — 19 Oct 2026

A session table is a weighted average of whatever was trading. On 19 Oct micro_scalper supplied **198 of
279 trades — 71%** — and was then disabled. Everything those tables said about "which hours work" was
really a description of one rule's habits, and acting on it after switching that rule off would mean
acting on a picture of something that no longer trades.

The clearest case: **09:00 UTC, 24 trades, 4% won, 96% hit the first stop, −1.379R each.** One win in 24
has a probability of about 1.5 in a million if the entries were coin flips, so it is not a property of
the hour — it is a property of whatever was trading in it.

`session_breakdown()` and `hour_breakdown()` now take `exclude_strategies`, the endpoint takes
`?exclude=a,b`, and it also returns `disabled_strategies` so the page can offer the right default. The
Analysis page shows a checkbox — hidden entirely until something is actually disabled — that reads
"leave out micro_scalper (switched off)", plus a warning when switched-off rules are being included:

> **Including rules that are switched off.** Those trades still sit in the record, so these numbers
> describe what used to trade, not what trades now.

This turns a question that needed a month of fresh data into one the existing record already answers.

---

# Measuring the exit geometry instead of guessing it — 19 Oct 2026

## The finding that prompted it

Weighted across sessions: average win **+0.405R**, average loss **−0.859R**, a payout of **0.47 : 1**.
Break-even at that payout needs a **68%** win rate. A perfect coin flip gives 50%, so the geometry loses
**−0.227R per trade before any indicator is consulted**. The measured result is −0.309R, so roughly three
quarters of the loss is the shape of the exits rather than the quality of the entries.

The obvious reaction — pick a smaller target, since no recent trade reached even 0.82R against targets
near 1.5R — is curve fitting against the same trades that produced the observation.

## `tools/exit_geometry.py`

Replays a grid of stops and targets over the trades already taken, using the `mfe_r` / `mae_r` recorded
on every trade, split in time so a geometry has to hold on a half it was not chosen on.

**Two refusals, both deliberate.**

*It will not simulate wider stops.* The recorded excursion ends when the trade closed, so for anything
stopped at −1R there is no record of where price went next. "Wider stops would have worked" is the most
tempting wrong conclusion available from this data, and the tool skips those cells rather than inventing
them (`MAX_SIMULABLE_STOP`).

*It reports two bounds.* `mfe` and `mae` do not say which came first, and for any geometry where both
barriers were reachable that ordering decides the outcome. Results are given optimistically and
pessimistically; a conclusion counts only when it survives the worse ordering
(`POSITIVE_BOTH_WAYS` / `NEGATIVE_BOTH_WAYS` / `DEPENDS_ON_ORDER`).

## What it says, and a correction

On synthetic data shaped like the live record the verdict is **NOTHING SURVIVES** — the best geometries
improve the result from −0.33R to about −0.17R and never cross zero.

So the earlier claim that "three quarters of the loss is geometry" is true as accounting and misleading
as a plan. Fixing the geometry reduces the bleed; it does not produce an edge. The arithmetic:

```
at a 43.5% win rate, break-even needs a payout of 1.30 : 1
with a 1.0R stop that means an average win of 1.12R
the best single excursion across seven recent trades was 0.82R
```

The entries do not travel far enough to earn the payout their win rate requires. Tightening the stop to
0.6R would make an 0.80R move worth 1.33R and clear the bar — but tightening also converts some current
winners into losers, which is exactly what the arithmetic cannot see and the replay can.

**The constraint is entry quality, not exit shape.** No geometry turns 43.5%-accurate entries that stop
travelling near 0.8R into a profitable system. The codebase can now say whether geometry is worth
changing; it cannot manufacture an edge the entries do not have.
