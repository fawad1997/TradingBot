# Book findings backlog

Ideas taken from trading literature, checked against this codebase, and kept here until they are either
built or rejected. Each one records where it would go, what it would touch, and whether applying it
restarts the live trade record.

---

## How to use this file

**Edit the `Do?` column.** Change `TODO` to one of:

| Value | Meaning |
|---|---|
| `YES` | Build it in the next pass |
| `NO` | Rejected — put the reason in the notes under the finding |
| `LATER` | Good idea, blocked on something (usually trade count) |

Then hand the file back and everything marked `YES` gets built together.

**Before marking anything `YES`, read the `Resets?` column.** `Resets? = yes` means the change alters what
a trade does, so trades recorded before it cannot be pooled with trades after it. The 23-trade record
restarts from zero and the 200-trade readiness gate restarts with it. `Resets? = no` means the change is
measurement, research or tooling only and costs nothing.

As of 18 Oct 2026 the record stands at 23 trades. Until that number is in the hundreds, the sane default
for anything marked `Resets? = yes` is `LATER`.

---

## The backlog

**BUILT 18 Oct 2026** — everything marked `DONE` below is implemented and tested. See
`docs/BOOK_CHANGES.md` for what each change touched and how to verify it.

The remaining `Do?` values are **my recommendation, not your decision.** Change any of them.

| # | Finding | Source | Impact | Resets? | Do? |
|---|---|---|---|---|---|
| 20 | Validate the cost model — the z-score test | Kissell ch.3 | **High** | no | **DONE** |
| 1 | Sample uniqueness weighting for the ML filter | LdP ch.4 | **High** | no | **DONE** |
| 2 | Purged + embargoed cross-validation | LdP ch.7 | **High** | no | **DONE** |
| 21 | Normalise slippage by volatility, not points | Kissell ch.3 | **High** | no | **DONE** |
| 19 | Random-search baseline against the tuner | Liu ch.8 | **High** | no | **DONE** |
| 16 | Implementation shortfall + effective/realized spread | Harris ch.21 | **High** | no | **DONE** |
| 3 | Strategy-risk calculator (implied Sharpe / implied precision) | LdP ch.15 | **High** | no | **DONE** |
| 4 | Triple-barrier labelling | LdP ch.3 | Medium | no | **DONE** |
| 7 | Look-ahead bias detection by truncation | Chan ch.3 | Medium | no | **DONE** |
| 6 | Outlier / bad-tick filtering in research data | Chan ch.7 | Medium | no | **DONE** |
| 5 | Drawdown *duration*, not just depth | Chan ch.2 | Medium | no | **DONE** |
| 8 | Free-parameter audit (≤5 per strategy) | Chan ch.3 | Low | no | **DONE** |
| 9 | Half-life holding period (Ornstein-Uhlenbeck) | Chan ch.7 | **High** | yes | LATER |
| 10 | Drop price stops on mean-reversion strategies | Chan ch.6/7 | **High** | yes | LATER |
| 15 | Limit orders instead of discarding spread-gated signals | Harris ch.18 | **High** | yes | LATER |
| 22 | Cost forecast as a distribution, not a point | Kissell ch.3 | Medium | yes | LATER |
| 11 | Multi-strategy Kelly allocation `F* = C⁻¹M` | Chan ch.6 | Medium | yes | LATER |
| 17 | Fundamental vs transitory volatility split | Harris ch.20 | Medium | yes | LATER |
| 18 | Predict spread ceiling from volatility | Harris ch.14 | Medium | yes | LATER |
| 14 | Cointegration / pair trading | Chan ch.7 | Medium | yes | LATER |
| 12 | Sequential bootstrap for the ML filter | LdP ch.4 | Low | no | LATER |
| 13 | Activity-based bars (tick / volume / dollar) | LdP ch.2 | Medium | yes | **NO** |
| R1 | Research gate fails open — add an evidence requirement | Review P0-4 | **High** | no | **DONE** |
| R2 | Adaptive spread cap never expired | Review C | **High** | no | **DONE** |
| R3 | `max_armed_symbols` ceiling that refuses nothing | Review B | Medium | **operator** | **DONE** (detection) |
| R4 | Generate doc counts from code, not by hand | Review F | Medium | no | **DONE** |
| R5 | Confirm commission against the live Exness account type | Review P0-2 | **High** | no | **YOU** |
| R6 | Recovery tests: order timeout, crash after submit, partial fill, rejected stop, MT5 drop, DB outage | Review P0-5 | **High** | no | LATER |
| R7 | Replay the +3.65R / +3.90R MFE trades against real tick paths | Review P1 | Medium | no | LATER |
| R8 | Compare the exit logic against a frozen fixed-stop baseline | Review D | Medium | no | LATER |
| R9 | Rotate every exposed credential | Review P1 | **High** | no | **YOU — still open** |
| R10 | Research all 8 strategies, not just micro_scalper | Review P0-4 follow-on | **High** | no | **DONE** |
| R11 | Live-record breakdown per strategy+symbol with intervals | Review P0-1 | **High** | no | **DONE** |
| R12 | Decide which strategies join `gate_strategies` | — | **High** | **yes** | **YOU** |
| R13 | Extend the sweep-based lab beyond micro_scalper | — | Low | no | **NO** — 104 cells of searching |
| R14 | Per-strategy gate thresholds instead of one shared bar | Asad | **High** | no | **DONE** |
| R15 | Set `gate_overrides` from each strategy's own record | Asad | **High** | **yes** | **YOU** — run tools/gate_thresholds.py |
| R16 | Correlation-aware portfolio risk (permit justified simultaneous trades) | Asad | **High** | no (opt-in) | **DONE** |
| R17 | Direction counts in correlated risk — `abs(rho)` could not tell hedge from doubling up | Asad | **High** | no (opt-in) | **DONE** |
| R18 | Portfolio report: net R, drawdown, co-losses, marginal contribution | Asad | **High** | no | **DONE** |
| R19 | Switch `portfolio_risk_mode` to `diversified` | Asad | **High** | **yes** | **YOU** |
| R20 | Surface the integrity settings and the real-money checks on the Settings page | Asad | **High** | no | **DONE** |
| R21 | Portfolio view on the Analysis page (net R, drawdown, co-losses, marginal) | Asad | **High** | no | **DONE** |
| R22 | Spread rejections name their ceiling and its age on screen | Review C | Medium | no | **DONE** |
| R23 | Before/after deploy comparison from the account's own data | Asad | **High** | no | **DONE** |
| R24 | Disable micro_scalper (198 live + 2,642 shadow, both clearly negative) | Asad | **High** | **yes** | **DONE** |
| R25 | `gate_block_below_r` -0.30 -> -0.05; the old limit permitted -0.29R | Asad | **High** | **yes** | **DONE** |
| R26 | `research-lab --all-strategies` to populate per-strategy research on demand | Asad | Medium | no | **DONE** |
| R27 | Add the 7 live strategies to `gate_strategies` once research exists | — | **High** | **yes** | **YOU** |
| R28 | Exclude switched-off rules from the session/hour tables | Asad | **High** | no | **DONE** |
| R29 | Investigate the 09:00 UTC cluster: average loss EXCEEDS the full stop | — | **High** | no | LATER |
| R30 | Measure exit geometry over recorded excursions, bounded by ordering | Asad | **High** | no | **DONE** |
| R31 | Run `exit_geometry.py` on the real 276-trade record | Asad | **High** | no | **YOU** |

Sorted by recommendation, then by value. Every **YES** is `Resets? = no`. That is not a coincidence — it
is the whole argument. With 23 trades on the record, anything that restarts the clock costs more than it
can return right now.

**#13 is the only NO.** Activity-based bars would touch every indicator, every strategy and the whole bar
pipeline, and retail FX supplies tick volume rather than true volume, so the statistical benefit is
weakened before it starts.

## Recommended sequence

**Batch 1 — the honesty batch.** #20, #21, #16, #19, #5, #8. None of it changes a trading decision; all of
it checks whether numbers the system already relies on are true. #20 first and urgently, because `cost_r`
is written as `None` on every signal, so the clock on collecting that data starts whenever it is fixed.

**Batch 2 — the ML batch.** #1, #2, #4, plus #7 and #6 for the research pipeline. Together these answer
whether AUC 0.517 is a real verdict on the model or the result of training it on non-independent samples.
Either answer is worth having: a working filter is a genuine edge improvement, and a confirmed dead one
stops the attention being spent on it.

**Batch 3 — only once the record reaches a few hundred trades.** #9 and #10 together, as one change.
Then #15 and #22, both of which depend on #16 having been running long enough to produce data.

## The change that is not in any book

**Turn off the three weakest CANDIDATE strategies.**

At ~11.5 trades/day shared across the live strategies, going from 8 to 5 roughly doubles each survivor's
trade rate, which halves the time to reach a statistically meaningful record. It also cuts the implicit
test count from 32 cells to 20, which lowers the chance of a false positive from 81% to about 64%.

It costs nothing, needs no code, and is available today. No book recommends it because no book knows the
record stands at 23 trades.

---

# Details

## 1. Sample uniqueness weighting — LdP ch.4

**Where:** `app/ml/filter.py`, `train_strategy()`

**Problem.** Every training row is weighted equally. But 13 symbols run concurrently with up to 10 open
positions, so trade outcomes overlap heavily in time — two trades opened an hour apart share most of the
price path that decides both. The rows are not independent observations.

López de Prado's analogy: blood samples where each tube has been contaminated with blood from the nine
patients before it, and you must still find what predicts cholesterol.

**Why it matters here.** The effective sample size is far below the row count. A boosted model trained on
non-unique rows fits in-bag data and generalises at chance. **The measured AUC of 0.517 is consistent with
this being the cause rather than the model being genuinely useless.**

**Change.**
1. For each label, count concurrency `c_t` = number of labels live at bar `t`.
2. Uniqueness `u_i` = mean of `1/c_t` over the label's life.
3. Pass `u_i` as LightGBM `sample_weight`.
4. Cap bootstrap draws at `mean(u)` so rows are not sampled more often than their independence allows.

**Test it answers:** if mean uniqueness is ~0.3, 500 rows are worth ~150 independent observations, and the
honest sample count is far smaller than it appears.

---

## 2. Purged + embargoed cross-validation — LdP ch.7

**Where:** `app/ml/filter.py:218` (`split = int(n * 0.7)`) and `app/research/lab/optuna_wf.py:61`

Both split chronologically with no gap at the boundary.

> *"Purge from the training set all observations whose labels overlapped in time with those labels included
> in the testing set... eliminate from the training set observations that immediately follow an observation
> in the testing set."*

**Problem.** A trade opened before the split and closed after it has its outcome partly determined by
test-period prices that influenced the tuning.

**Change.** Drop training rows whose label window overlaps the test window (purge), plus rows starting
within ~1% of the series length after the test boundary (embargo).

**Caveat.** Leakage *inflates* results, and the model currently scores 0.517. So this is a correctness fix,
not an explanation of the present number. Pair it with #1 — same non-IID problem from two sides.

---

## 3. Strategy-risk calculator — LdP ch.15

**Where:** new, alongside `tools/live_readiness.py`

For asymmetric payouts:

```
θ[p, n, π₋, π₊] = ((π₊ − π₋)·p + π₋) / ((π₊ − π₋)·√(p(1−p))) · √n
```

**Run on the 16–17 Oct record** (p = 0.783, wins +0.672R, losses −1.0R):

| Trades/year | Implied annual Sharpe |
|---|---|
| 250 | 7.07 |
| 1,000 | 14.15 |
| 3,000 | 24.50 |

Medallion runs about 2.5. **A Sharpe of 24 is a measurement error, not a result.** The formula converts
"23 trades is not enough" from an opinion into arithmetic.

Sensitivity at 3,000 trades/year:

| Precision | Sharpe |
|---|---|
| 0.78 | 24.0 |
| 0.70 | 12.2 |
| 0.65 | 6.0 |
| **0.598** | **0** |

**Break-even precision at the current payout ratio is 59.8%.**

**The planning consequence:** frequency enters as `√n`, precision does not. Doubling trade count multiplies
Sharpe by 1.41; precision slipping from 78% to 70% halves it. Trade quality dominates trade quantity
mathematically — chasing volume is the weaker lever.

**Change.** A tool that reads the live record and prints implied Sharpe, break-even precision, the
sensitivity table, and the precision required for a target Sharpe. Wire the break-even figure into
`live_readiness.py` as a check.

---

## 4. Triple-barrier labelling — LdP ch.3

**Where:** `app/ml/filter.py:215` — currently `y = (r > 0)`

**Problem.** The label is path-independent. A trade that reached +3R and closed at +0.01R is labelled
identically to one that went cleanly to target. The exit ladder produces a lot of barely-positive
finishes, so this matters more here than it would elsewhere.

**Change.** Label by which barrier was touched first — profit target, stop, or time limit. `mfe_r` and
`mae_r` are already persisted, so the path data exists.

---

## 5. Drawdown duration — Chan ch.2

**Where:** `app/research/metrics.py` has `max_drawdown` (depth) and no duration anywhere.

> *"If a strategy has deep (e.g., more than 10 percent) or lengthy (e.g., four or more months) drawdowns,
> it is unlikely that it will have a high Sharpe ratio."*

Depth is what the risk tiers watch. Duration is what makes people switch a system off. Add it to
`metrics.py` and to `live_readiness.py`.

---

## 6. Outlier / bad-tick filtering — Chan ch.7

**Where:** `app/research/lab/data.py` — no spike filtering anywhere in the codebase.

> *"A mean-reverting strategy will buy on a fictitious quote that is much lower than some moving average
> and sell on the next correct quote... and thus makes a profit."*

Bad prints inflate mean-reversion backtests specifically, which silently corrupts the research verdicts for
`mean_reversion_v1` and `vwap_reversion`.

---

## 7. Look-ahead detection by truncation — Chan ch.3

Run the backtest on all data, save positions to A. Truncate the last N days, run again, save to B. The
overlapping rows must be **identical**. Any difference is look-ahead leakage.

Mechanical and decisive. Three cross-checking engines do not catch this; only this test does.

---

## 8. Free-parameter audit — Chan ch.3

> *"I would not employ more than five parameters, including quantities such as entry and exit thresholds,
> holding period, or the lookback period."*

Plus roughly 252 data points per parameter. Measurable directly from `strategies.yaml`. If `micro_scalper`
carries twelve tuned parameters it is overfit by construction regardless of what the walk-forward says.

---

## 9. Half-life holding period — Chan ch.7  ⚠ resets the record

Model the series as `dz(t) = −θ(z(t) − μ)dt + dW`. Get θ by regressing `dz` on `z`. Half-life = `ln(2)/θ`.

**Why it is better than backtesting a holding period:** θ is estimated from every bar in the series, not
from the handful of bars where a trade triggered.

> *"The estimate for the half-life is much more robust than can be obtained directly from a trading model."*

The reversion strategies currently have no principled exit timing. This gives them one, per symbol.

**Also the "more trades" lever that works:** a reversion position going nowhere currently holds one of ten
slots until its stop or target. A half-life time stop recycles the slot on a measured schedule.

---

## 10. No price stops on mean-reversion strategies — Chan ch.6/7  ⚠ resets the record

> *"Stop loss can be suitable for momentum strategies but not reversal strategies."*

Run a momentum model again after a loss and it gives the opposite signal — the thesis broke. Run a
**reversal** model again after a loss and it gives the *same* signal, more strongly. The position improved.
A stop there exits at the worst available moment.

`mean_reversion_v1.py:58` sets `sl = min(low[i-2:i+1]) - sl_buffer_atr * atr`. `vwap_reversion` does the
same. Both inherit the global exit ladder.

**The hook already exists:** `exit_model` is per-strategy and `registry.py:26` already deep-merges
per-strategy overrides. Mostly configuration, not code.

**Adaptation Chan does not cover.** He writes about unlevered market-neutral equity pairs. This is a
leveraged account with a 5% daily loss limit and a 10-position ceiling, where a stopless position consumes
a slot indefinitely. **The right version here is a time stop, not no stop** — which is finding #9. These
two are one change.

---

## 11. Multi-strategy Kelly allocation — Chan ch.6  ⚠ resets the record

`app/edge/expectancy.py:24` implements the binary Kelly `p − (1−p)/b` per strategy. Chan's portfolio form
is `F* = C⁻¹M`, where C is the covariance matrix of strategy returns and M their means — it allocates
*across* strategies by how they co-move.

Current allocation is hand-set: 0.5 for six strategies, 1.0 for two. Those numbers are a judgement, not a
measurement.

Also answers whether the eight strategies genuinely diversify.

**Blocked:** needs real per-strategy trade history. At 23 trades this would fit noise. Keep behind the
200-trade gate.

**Worth absorbing regardless:** `g = r + S²/2`. Long-term growth is proportional to **Sharpe squared**.
R-expectancy ignores variance and trade frequency entirely.

---

## 12. Sequential bootstrap — LdP ch.4

Draw training samples with a probability that decreases as overlap with already-drawn samples increases.
Lower value than #1 on its own; natural follow-on once uniqueness is computed.

---

## 13. Activity-based bars — LdP ch.2  ⚠ resets the record

> *"Time bars oversample information during low-activity periods and undersample information during
> high-activity periods... time-sampled series often exhibit poor statistical properties, like serial
> correlation, heteroscedasticity, and non-normality of returns."*

Sampling by tick, volume or dollar value produces returns closer to IID Gaussian — which is what every
statistical method in this system assumes.

**Why this is not a quick win.** It touches every indicator, every strategy and the whole bar pipeline.
Retail FX gives tick volume, not true volume. If ever done, do it in the research lab first and leave live
trading on M5/M15/H1/H4.

---

## 14. Cointegration / pair trading — Chan ch.7  ⚠ resets the record

Test with augmented Dickey-Fuller; hedge ratio by regression. Chan notes CAD/AUD is naturally stationary,
and AUDUSD and USDCAD are both traded here.

**The standing argument against adding strategies applies.** 8 strategies × 4 sessions is already 32
implicit tests, where the chance of at least one false positive under pure noise is 81%. The difference
with this one is that it arrives with a statistical test attached rather than a hunch.

### Concrete recipe (Liu ch.9)

The Apress repository gives a runnable version, which removes the "how would we even build it" objection:

1. Candidate pairs: `itertools.combinations` over the symbol list.
2. Cointegration: `statsmodels.tsa.stattools.coint(a, b)` — Engle-Granger, p < 0.05.
3. Hedge ratio: OLS of one series on the other; the spread is the residual.
4. Normalise: rolling mean and standard deviation over a window → z-score.
5. Entry at `|z| ≥ 2`, exit at `|z| ≤ 1`.

Two corrections before any of this is used here. **Fit the hedge ratio on training data only** — the
repository's own example does not, and its results are therefore unachievable (see the note under #19).
And run the cointegration test per fold, not once: cointegration between two instruments is not a
permanent property and routinely breaks.

---

## 15. Limit orders instead of discarding spread-gated signals — Harris ch.18  ⚠ resets the record

**Where:** `app/broker/mt5_adapter.py:244` — every entry is `TRADE_ACTION_DEAL` with
`ORDER_TYPE_BUY`/`ORDER_TYPE_SELL`. The only pending orders in the system are the `BUY_STOP`/`SELL_STOP`
pair used by the news straddle (`mt5_adapter.py:342`), and stop orders take liquidity too.

**Every entry this system has ever made is a market order.** A limit order is never used anywhere.

> *"Traders demand liquidity when they submit market orders, and they supply liquidity when they submit
> limit orders... When the spread is wide, taking liquidity is expensive and offering liquidity is
> attractive. When the spread is narrow, market orders are attractive relative to limit orders."*

The spread gate currently treats a wide spread as a reason to **refuse**. Harris's framing is that a wide
spread is also a reason to **offer rather than take**. Since the spread gate is the most frequent refusal in
the system, signals currently discarded could instead rest as limit orders.

**This is the one idea in these books that plausibly increases trade count without lowering trade quality.**

### Important correction to Harris, for a CFD account

Harris is describing real exchanges with public order books, where a limit order genuinely supplies
liquidity and earns the spread. **That is not available here.** On an Exness CFD account the broker is
always the counterparty; there is no book to join, no rebate, and no spread to capture.

What a limit order actually buys on this account is narrower but still real: the entry price is *chosen*
rather than accepted at whatever the ask happens to be at the moment the signal fires. A `BUY_LIMIT`
fills when the ask reaches the limit price, so the saving is the difference between the current ask and the
chosen one — not the spread itself.

The cost is non-execution, which is Harris's **missed trade opportunity cost** (see #16).

**Note the overlap with existing behaviour.** The parked-signal mechanism already waits for a spread to
narrow and re-checks on later ticks. This finding extends that from "wait and then take" to "rest an order
and let the market come to us", which is a different risk profile, not a new concept.

### ⚠ The timing option — why this is NOT a free win for this system

The full edition (§14.4.2.2) carries a warning the draft omitted, and it lands directly on this
architecture:

> *"Limit order traders give a valuable timing option to market order traders when they do not
> continuously update their limit order prices... The timing option is most valuable when market order
> traders can respond to changing conditions faster than limit order traders can."*

> *"Order entry, order routing, and order handling delays also ensure that values may change before
> instructions to cancel limit orders become effective. These delays make the timing option more
> valuable. The equilibrium spread therefore also depends on the average time it takes limit order
> traders to successfully cancel their orders. If the order cancellation process is slow, equilibrium
> spreads will be wide."*

**This system visits each symbol roughly every 3.25 seconds** — 13 instruments sharing one 0.25 s loop.
A resting limit order that can only be repriced or cancelled on that cadence is handing a free 3.25-second
option to anyone faster, which on a CFD account is the broker and every other participant.

Harris's own framing is that the limit order trader is the one who gets adversely selected: *"When the
limit order traders trade, they wish they had been able to adjust their prices. When the limit order
traders do not trade, they wish that they had."*

**Consequence for this finding.** It is still worth doing, but not as "rest orders and collect better
fills". The viable version is narrow:

- Short-lived limit orders only, with an expiry measured in seconds, not minutes.
- Only on instruments where measured volatility is low relative to the spread, so the option being given
  away is cheap.
- Measured, not assumed: compare realised fill quality against what a market order would have paid at
  signal time (see #16), and abandon it if the comparison is negative.

The prerequisite is #16. Without implementation-shortfall measurement there is no way to tell whether
limit orders helped or quietly cost money.

---

## 16. Implementation shortfall — Harris ch.21

**Where:** slippage is currently measured at fill, in points, in `execution_events`.

Harris breaks transaction cost into three parts:

| Component | Measured here? |
|---|---|
| Explicit — commission, fees | yes, in the cost gate |
| Implicit — spread paid, price impact | partly: spread yes, decision-to-fill drift no |
| **Missed trade opportunity cost** | yes — the counterfactual ledger |

> *"When traders use a specified price benchmark, they estimate the per unit transaction cost as the
> difference between the trade price and the benchmark price."*

The right benchmark is the price **at the moment of the decision**, not the moment of the fill. The gap
between the two covers the round-robin wait (about 3.25 s per symbol), gate processing and order
transmission — and none of it is currently measured.

Signal timestamps and fill timestamps are both already stored, so this is an arithmetic change over data
that exists.

### The three benchmarks, and why the gap between two of them is the useful number

Full edition §21.3.1 names three measures, all computed as the signed difference between the fill price
and a quote midpoint. Only the choice of midpoint differs:

| Measure | Midpoint taken | What it tells you |
|---|---|---|
| **Effective spread** | at the moment of the trade | what crossing the spread cost |
| **Realized spread** | 5, 10, 15 or 60 min after the trade | what the liquidity supplier actually kept |
| **Implementation shortfall** | when the decision was made (Perold) | the full cost, including delay |

**The diagnostic worth having is `effective − realized`.** That difference is adverse selection — how far
the price kept moving against the fill after it happened. A system that is being systematically picked off
shows a realized spread much worse than its effective spread, and nothing in the current slippage
measurement would reveal it.

All three are computable from data already stored: `ticks` has the quote history, `signals` has decision
times, `execution_events` has fill times and prices.

Implementation shortfall is also the measure that prices the round-robin wait. If the decision-to-fill cost
turns out to be material, that is an argument for revisiting the 3.25 s cadence — and it is currently
invisible.

---

## 18. Predict spread from volatility rather than react to it — Harris ch.14

> *"The timing option will be quite valuable for volatile instruments because prices may change
> substantially before traders can reprice their orders. Equilibrium spreads therefore must be wider for
> volatile instruments than for relatively stable instruments."*

The adaptive limits currently derive the spread ceiling from recently *observed* spreads — it reacts after
the fact. Harris gives the causal direction: volatility drives spread, not the reverse.

ATR is already computed on every timeframe for every symbol. A spread ceiling conditioned on current
volatility would tighten *before* a volatile period widens the spread, instead of a cycle later.

Cheap, and it touches only the adaptive block, which is already built to tighten and never loosen.

---

## 17. Fundamental vs transitory volatility — Harris ch.20  ⚠ resets the record

> *"Fundamental volatility is due to unanticipated changes in instrument values while transitory
> volatility is due to trading activity by uninformed traders... Traders must distinguish between the two
> volatility types to accurately predict future volatility, the profitability of dealing strategies, and
> transaction costs."*

The regime gate classifies trending versus choppy. It does not distinguish *why* a market is moving.

**Why this matters specifically for the reversion strategies:** mean reversion works on transitory
volatility and is run over by fundamental volatility. The event gate is a crude proxy for this already — a
scheduled release implies fundamental — but an unscheduled fundamental move looks identical to a
transitory one to the current logic.

Testable from stored data: a move on high tick volume with no calendar event nearby is more likely
transitory; the same move with an event is fundamental. The VPIN series in `microstructure/flow.py`
is the natural input and is already computed.

---

## 19. Random-search baseline against the tuner — Liu ch.8

**Where:** `app/research/lab/optuna_wf.py:51` and `:108` — `optuna.samplers.TPESampler(seed=...)`, with no
control arm anywhere in the file.

The book's chapter 8 runs four Bayesian-optimisation acquisition functions (Expected Improvement, qEI,
Upper Confidence Bound, Knowledge Gradient) **and a random sampler**, then plots all five best-so-far
curves on the same axes (`update_random_observations`).

That baseline is the point. **If the tuner does not beat random draws from the same parameter space, the
tuning is fitting noise**, and every "optimised" parameter it produced is a coincidence dressed up as a
result. Right now there is no way to tell which case applies here.

**Change.** Add a `RandomSampler` arm to each walk-forward fold with the same trial budget, store both
scores in `research_lab_runs`, and report the gap. A tuner that cannot beat random on out-of-sample folds
should be switched off rather than trusted.

Cheap, pure research, and it tests the honesty of every parameter in the system.

---

## 20. Validate the cost model — the z-score test — Kissell ch.3

**Where:** `app/edge/cost.py` produces an estimate. Nothing anywhere checks whether that estimate is right.

The cost gate refuses trades whenever estimated cost exceeds 25% of the money at risk. That estimate has
never been compared against what the trades actually cost. **A gate that refuses trades on an unvalidated
number is making decisions on an assumption.**

Kissell gives the test:

```
Z = (Pre-Trade Cost Estimate − Actual Arrival Cost) / Pre-Trade Timing Risk
```

> *"If the pre-trade estimates are accurate, then the z-score statistic should be a random variable with
> mean zero and variance equal to one. That is, Z~(0,1)."*

So the diagnostic is sharp and falsifiable:

| Result | Meaning |
|---|---|
| mean ≈ 0, variance ≈ 1 | the cost model is honest |
| mean > 0 | it systematically **over**estimates cost — the gate is refusing trades it should take |
| mean < 0 | it systematically **under**estimates — trades are being taken that should not be |
| variance ≫ 1 | the risk estimate is too small; costs are far less predictable than assumed |

**The over-estimate case matters most here**, because the cost gate is one of the most frequent refusals.
A positive mean z-score would mean the system has been starving itself of trades on a number that is
wrong — and that is the one credible route to more trades that does not require a new edge.

Kissell's three conditions for the test to be valid: run it separately by order size, separately by
strategy, and base the estimate on what was actually traded rather than what was intended.

**Change.** Persist the pre-trade cost estimate on every signal, compute realised arrival cost on every
fill, and report the z-score distribution per symbol and per strategy.

### Correction (18 Oct 2026)

An earlier version of this entry claimed `cost_r` was written as `None` and discarded. **That was wrong.**

The `None` in `_insert_signal()` is an initial placeholder. `app/core/engine.py:1107`, immediately after
the cost gate runs, overwrites it:

```python
sig.edge_r, sig.cost_r = g.net_edge_r, cost.cost_r
self.ledger.update("signals", "id", sig.id, {"edge_r": ..., "cost_r": ...})
```

`Ledger.update()` exists in `app/storage/writer.py`. **The estimate is persisted correctly** for every
signal that reaches the cost gate. Signals refused by one of the eleven earlier gates have no `cost_r`,
which is right — no cost was computed for them, and they are not part of the population this test is
about.

So the historical data for the estimate side already exists. What is genuinely missing is the other half:

1. **The realised cost of each fill**, measured against the decision price, to compare the estimate with.
2. **Timing risk** — the denominator. Not computed anywhere.
3. **The audit itself.**

---

## 21. Normalise slippage by volatility, not points — Kissell ch.3

**Where:** `app/reports_session.py` reports `avg_slippage_points` pooled across all symbols.

> *"it is not possible to determine if missing VWAP by 3 bps in one stock is better performance than
> missing VWAP by 10 bps in another stock. If the first stock has very low volatility and the second stock
> has very high volatility, missing VWAP by 10 bps in the second name may in fact be better performance."*

**"Average slippage 4.2 pts" pools XAUUSDm with EURUSDm.** A point of gold and a point of EURUSD are not
comparable quantities, and averaging them produces a number that means nothing. This is a measurement bug
in the session review, not a theoretical nicety.

**Change.** Express slippage as a fraction of the instrument's own ATR, or in basis points of price. Both
make the figure comparable across the thirteen symbols and across days. Keep the raw points figure
per-symbol, where it is meaningful.

### The realisation that comes with it

Kissell decomposes execution cost into **market impact** and **price appreciation**. At 0.01–1.0 lots on a
retail CFD account, market impact is genuinely zero — nothing this system does moves a price.

Therefore **every point of slippage recorded here is price appreciation, not impact**: the market moving
during the round-robin wait, plus adverse selection. That reframes the measure entirely. It is not
measuring execution quality against liquidity; it is measuring latency and being picked off, which is the
same thing finding #16 sets out to quantify properly.

---

## 22. Cost forecast as a distribution, not a point — Kissell ch.3  ⚠ resets the record

> *"A cost measure is an 'ex-post' or 'after the fact' measure... It is always a single value. A cost
> forecast, on the other hand, occurs 'ex-ante'... It is an estimated value comprised of a distribution
> with an expected mean (cost) and standard deviation (timing risk)."*

The cost gate compares a single expected cost against 25% of risk. Kissell's framing is that a forecast is
a distribution, and the standard deviation — timing risk — comes from price volatility, liquidity risk and
parameter estimation error in the model itself.

A gate built on this would ask whether `cost + k × timing_risk` clears the threshold, not just `cost`. In
volatile conditions the same mean cost carries far more uncertainty, and the current gate cannot see that.

**Blocked on #20.** There is no point adding a risk term to a model whose central estimate has never been
validated. Do #20 first; if the z-score variance comes back far above 1, that is the evidence for this.

---

## Note: the repository's own example has look-ahead bias

Worth recording because it validates finding #7.

The pairs-trading backtest in chapter 9 fits the hedge ratio across the entire downloaded period:

```python
model = OLS(Y, X_with_constant).fit()      # fitted on ALL data
spread = Y - model.predict()               # then "backtested" on the same data
```

The hedge ratio that defines the spread is estimated using prices from the future of every trade the
backtest then takes. The reported performance cannot be achieved in practice.

This is a published teaching repository for a 2023 book, and the bug is in the headline strategy. It is
exactly what the truncation test in #7 exists to catch, and a reminder that a result being in print is no
evidence it is sound.

---

# Already in the system — do not re-raise

Checked and present. Future books should not surface these as gaps.

| Concept | Where |
|---|---|
| Deflated Sharpe ratio | `research/metrics.py:125` |
| Probabilistic Sharpe ratio | `research/metrics.py:108` |
| Meta-labelling | `ml/filter.py` |
| Monte Carlo on the trade distribution | `research/metrics.py:138` |
| VPIN and order-flow microstructure | `microstructure/flow.py` — research-only, correctly gated off decisions |
| Walk-forward with expanding window | `research/lab/optuna_wf.py` (no purging — see #2) |
| Kelly sizing, single-strategy form | `edge/expectancy.py:24` |
| Max drawdown depth | `research/metrics.py:21` |
| Counterfactual ledger on refused signals | `counterfactuals` table |
| Three independent backtest engines | fastsim, VectorBT, Backtrader |
| Welch t-test between groups | `research/metrics.py:180` |
| Missed-trade opportunity cost | `counterfactuals` table — this is Harris ch.21's third cost component |
| Spread measured from real ticks, not assumed | `collect-spreads`, `lab/data.py` |
| Slippage measured from own fills | `cost.slippage_sample_min = 20` |

---

# Rejected

| Idea | Source | Why not |
|---|---|---|
| Factor models / Fama-French | Chan ch.7 | Equities only; needs fundamental data vendors |
| January effect, year-on-year seasonality | Chan ch.7 | Author's own backtest returns −0.92; effect died |
| PCA factor trading | Chan ch.7 | Author's own example returns −1.81 |
| High-beta vs leverage | Chan ch.7 | Equity portfolios |
| Commodity futures seasonals | Chan ch.7 | Not traded here |
| Hierarchical risk parity | LdP ch.16 | 13 instruments, not a portfolio to optimise |
| Structural breaks (CUSUM/SADF) | LdP ch.17 | The regime gate already covers the need |
| Entropy features | LdP ch.18 | Marginal for this system |
| HPC, quantum computing | LdP ch.20–22 | Not a constraint here |
| QuantConnect LEAN integration | — | MT5/Exness is not a supported brokerage; would only be a fourth backtest engine |
| TradingView webhook automation | — | Free tier has no webhooks; paid starts ~$12.95/month |
| Dealer economics, specialists, block traders | Harris ch.13–17 | Describes exchange participants that do not exist on a CFD account |
| Order anticipators, front-runners, bluffers | Harris ch.11–12 | Requires a public order book to anticipate |
| Exchange structure, call vs continuous markets | Harris ch.23–27 | No choice of venue here |
| Insider trading, circuit breakers, regulation | Harris ch.28–29 | Not applicable to a retail CFD account |
| yfinance data pipeline, plotting, returns maths | Liu ch.1–7 | Introductory; this system's equivalents are more complete |
| SMA/EMA trend following, DJI cross-sectional momentum | Liu ch.5–6 | Simpler than the eight strategies already running |
| Backtest metrics (drawdown, annualised return, Sharpe) | Liu ch.7 | Already present, plus deflated and probabilistic Sharpe |
| Limit-order-book deep learning (FI-2010 dataset) | Liu ch.2 | Needs full order book depth; an MT5 CFD feed does not provide it |
| SVR / RandomForest / NN price-level prediction | Liu ch.10 | Predicting price level is weaker than meta-labelling, which is already built |
| BoTorch / Gaussian-process tuning | Liu ch.8 | Optuna TPE is already Bayesian optimisation; the *baseline* idea was worth taking (#19), the library was not |
| I-Star market impact model | Kissell ch.4–5 | Impact is a function of order size against average daily volume. At 0.01–1.0 lots it is zero — see the note under #21 |
| Trade schedule optimisation, Almgren-Chriss, efficient trading frontier | Kissell ch.5–6 | Slicing a large order over time. Orders here fill in one ticket |
| VWAP / TWAP / POV execution algorithms | Kissell ch.5 | Same — these work a large parent order; there is no parent order here |
| Portfolio optimisation with transaction costs | Kissell ch.9–11 | Portfolio construction across a stock universe, not applicable to 13 fixed instruments |
| Broker and algorithm performance comparison | Kissell ch.3 | One broker, no algo selection |
| Index-adjusted performance metric | Kissell ch.3 | Requires a benchmark index the instrument has a beta to |

---

# Books read

| Book | Date | Findings | Verdict |
|---|---|---|---|
| Ernest Chan, *Quantitative Trading* (2009) | 18 Oct 2026 | 6 new, 8 rejected | Dated and equity-centric; the exit-strategy and half-life material is the valuable part |
| Marcos López de Prado, *Advances in Financial Machine Learning* (2018) | 18 Oct 2026 | 6 new, 4 rejected | The source of several things already built here; the non-IID material is the valuable part |
| Larry Harris, *Trading and Exchanges* (2002) | 18 Oct 2026 | 4 new, 4 rejected | Read as a 113-page draft first, then the full edition. Most of it describes exchange structure a CFD account is not exposed to. The order-submission and transaction-cost chapters are the exceptions and justify the read on their own. **The full edition reversed a conclusion the draft supported** — see the timing option under #15 |

## A warning the three books agree on

Harris states his book's central lesson plainly in ch.14:

> *"You will learn why uninformed traders lose to well-informed traders whether they submit limit orders
> or market orders. Uninformed traders lose simply because they trade. If you are an uninformed trader
> and do not want to lose, you should minimize your trading."*

This is now the third independent argument in this file pointing the same way:

1. **Chan:** expectancy ≈ −(spread ÷ stop) with no edge — every trade starts underwater.
2. **López de Prado ch.15:** Sharpe scales with `√n` but is nonlinear in precision — quality dominates quantity.
3. **Harris ch.14:** without an informational edge, trading more only converts capital into transaction costs faster.

The stated goal behind reading these books was "more trades". Every one of them answers that trade
*count* is the wrong target, and that the only thing which makes more trades better is having an edge
first. Finding #15 is the single exception worth pursuing, because it converts signals that are already
being generated and then thrown away — it does not manufacture new ones.

| Peng Liu, *Quantitative Trading Strategies Using Python* (Apress, 2023) — code repository | 18 Oct 2026 | 1 new, 7 rejected | Introductory. Most of it is below what this system already does. The random-search baseline in ch.8 is the one real take, and its own ch.9 pairs backtest contains look-ahead bias |

| Robert Kissell, *The Science of Algorithmic Trading and Portfolio Management* (2013) | 18 Oct 2026 | 3 new, 6 rejected | Written for institutions executing large orders, most of which does not apply at retail size. But its transaction-cost-analysis framework exposed a real measurement bug (#21) and the fact that the cost model has never been validated (#20) |

## Books still to read

_Add them here._

| Book | Added | Status |
|---|---|---|
| | | |
