# What is in Quantara but not actually working for you

Written 16 Oct 2026, straight after the Quantara rename and the removal of the Gemini vision gate and Telegram.

This is the honest inventory you asked for: everything that ships but is switched off, records without acting,
or acts so rarely it may as well be off. Nothing here is a bug. Some of it is deliberately dormant and worth
keeping; some of it is costing you CPU and complexity for nothing. The last column says which.

---

## 1. Removed in this change

| Component | What it was | Why it is gone |
|---|---|---|
| `app/strategies/vision_gate.py` | Gemini graded a rendered chart image A–D; only A/A+ could trade | It was already doing nothing useful: `vision.strategies` was `[]`, so **no strategy ever waited for a grade**. It ran in shadow mode on 3 strategies, spending API quota and a background thread to write a letter onto signals nobody read. 288 lines out of `engine.py`. |
| `app/notifications/telegram.py` | Telegram transport | Blocked in Pakistan. Never delivered a single alert for you. |
| `google-genai` dependency | Gemini SDK | Nothing imports it any more. |
| 4 vision test files + the quota half of `test_spread_cost_and_quota.py` | ~140 tests of quota pacing, model fallback chains, shared budgets | All testing machinery that no longer exists. |

**What was deliberately kept:**

* `Gate.VISION` stays in the `Gate` enum, marked RETIRED. Your `rejections` table has historical rows carrying
  that value; deleting the member would break reading your own trade history.
* `signal_view()` still filters `vision_*` keys out of `features`. Old signals carry them, and they would
  otherwise show up as mystery columns in the losing-trades table.
* `ml/filter.py` still excludes the `vision_` prefix. Those are post-decision values — feeding them to the
  learned filter would be leakage.

---

## 2. Shipped OFF — doing nothing at all

| Component | Config | Verdict |
|---|---|---|
| ~~News straddle~~ | **now ON, all 13 symbols** | Moved out of this section on 16 Oct — see §2b below. |
| ~~Adaptive parameters~~ | **now ON** | Moved out of this section on 16 Oct — see §2b below. |
| **Adaptive strategy levels** | `h1_adx_relative: 0.0`, `rsi_reclaim_adaptive: false`, `rsi_extreme_sigma: 0.0` | Added and measured last week. Pullback went **−31.8R → −39.0R** on untouched data; adaptive RSI doubled both trades and losses. **Correctly off.** Measured, not guessed. |

### 2b. Switched ON on 16 Oct — watch these

**News straddle**, now armed on all 13 symbols. Before a high-impact USD release it places a buy stop above
and a sell stop below; whichever the news triggers becomes the trade and the other is cancelled.

Enabling it needed a code change, not just the switch. The arming check counted each resting leg against
`risk.max_concurrent_positions` (10), so two legs per symbol meant the first five symbols consumed the whole
allowance and symbols six to thirteen were refused with `MAX_CONCURRENT_POSITIONS` at every single release —
arbitrarily, by whichever order the round robin reached them in. Flipping the switch alone would have given
you five symbols while appearing to give you thirteen.

Straddle legs now have their own ceiling, `news_straddle.max_armed_symbols` (13), and are kept out of the
ordinary position count — OCO guarantees at most one leg per symbol survives, so two resting legs were never
two positions. `leg_risk_share` dropped from 0.5 to 0.25 so that 26 resting legs reserve 9.75% against the
10% portfolio cap. Real exposure when every symbol's straddle triggers is about 4.9%.

**What to watch:** this is the most spread-sensitive thing in the codebase, and spread is already where your
money goes. It stands down automatically if the gap blows out before the release, but the first few releases
are worth watching on the dashboard rather than trusting blind.

**Adaptive parameters**, now on. Each hour it re-measures how much each symbol moves and how wide its gap
usually runs, then adjusts the straddle distances and the spread ceiling.

**What to watch:** the adaptive spread cap can only *tighten* the learned ceiling, never loosen it. That is
deliberate and the one-way rule is pinned by a test — but it does mean **more signals will be blocked for
spread than before**. Three existing tests caught this interaction when the switch flipped, which is how it
was found. If trade count drops noticeably over the next few days, this is the first thing to look at:
`spread_limit_points()` reports its basis as "hourly adaptive cap" whenever it is the binding constraint,
and that shows on the Live page.

---

## 3. ON but record-only — costs CPU, changes no decision

| Component | What it actually does | Verdict |
|---|---|---|
| **Learned trade filter** (`ml_filter`) | AUC 0.60, mode `auto`, applies to `micro_scalper` only. In `auto` it enforces only when the model is both *validated* and under 7 days old — otherwise it scores and stores, blocking nothing. | **Keep.** It is cheap and it is the honest way to run a weak model. But do not count it as protection — right now it is almost certainly in shadow. |
| **Counterfactual ledger** | Follows every blocked/shadow signal to stop or target and records the hypothetical R. | **Keep.** This is one of the few things telling you whether your gates help or hurt. |
| **Microstructure** (`attach_to_signals: true`) | VPIN, tick bursts, depth imbalance stored on each signal. Gates nothing. | **Keep for now**, but it has produced no finding in months. If you want to cut CPU, this is the first thing to turn off. |
| **Context feed** | DXY / S&P / VIX / yields / oil, refreshed each minute. | **Keep.** Cheap, and stored on signals. |

---

## 4. ON but almost never fires

| Component | Why it rarely triggers |
|---|---|
| **Scaling** (`scaling.enabled: true`, max 3/symbol) | Requires an open position already ≥ 0.5R in profit **and** protected. Your median losing trade peaks at 0.04R. This fires approximately never. Harmless, but do not expect it to do anything. |
| **Calendar sync** | ON and working — pulls the ForexFactory weekly JSON. Worth keeping. |

---

## 5. The thing that matters most

**The research gate protects one strategy out of eight.**

```json
"research_lab": { "gate_strategies": ["micro_scalper"] }
```

`pullback_v1`, `mean_reversion_v1` and the five candidates trade with **no research gate at all**. The lab
measures them — `threshold_sweep_strategies` covers four of them — but nothing acts on the result.

And the gate that *does* cover `micro_scalper` has a suspected bug: `app/research/lab/gate.py` line 57 returns
before reading any numbers unless the verdict is exactly `NOT_ROBUST_ON_TEST`. Six other verdicts exist,
including `NO_QUALIFYING_PARAMETERS`, which means *"no parameter set reached the minimum trade count"* — i.e.
**"could not measure"** being treated as **"passed"**.

This is unresolved and it is the highest-value open question in the system. Run:

```
python tools\audit_research_gate.py --json > gate_audit.json
```

If most of the 13 symbols come back `VERDICT`, `micro_scalper` has been trading through a gate that never
checked it — which would account for a large share of its −$378.

---

## 6. What this does NOT fix

The rename is cosmetic. The removals cut a background thread, an API dependency and about 400 lines. Neither
touches the actual problem: **the entry rules have no measured edge.** Three separate improvement attempts
(breakeven trigger, profit-lock level, adaptive thresholds) were each overturned by measurement, and the
consistent finding was that there is no edge to protect.

Tidier code that loses money loses money just as fast.
