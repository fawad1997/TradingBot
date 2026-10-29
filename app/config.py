"""Validated, versioned configuration.

* config/config.json      -> AppConfig (runtime, risk, execution, research switches)
* config/strategies.json  -> strategy definitions + parameter registry
* .env                    -> secrets (MT5 login, DB URL, alert webhook, dashboard token)

Every change made at runtime goes through `ConfigManager.apply_change`, which
validates the WHOLE config again, writes a new version to disk and returns an
audit record (old value, new value, reason) for the config_versions table.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MAX_RISK_FRACTION_PER_TRADE = 0.015


class BrokerCfg(BaseModel):
    symbol_candidates: List[str] = ["XAUUSD", "XAUUSDm", "XAUUSDc"]
    magic_number: int = 20260101
    broker_utc_offset_hours: Optional[float] = None   # None = auto-detect from tick time
    connect_timeout_ms: int = 60000
    reconnect_max_attempts: int = 10
    # Symbols that trade through the weekend (e.g. Exness XAUUSD247m). For these the estimated Fri-21:00 ->
    # Sun-22:00 UTC weekend closure is NOT applied; every other symbol keeps the weekend-closed schedule.
    always_open_symbols: List[str] = []


class SessionWindow(BaseModel):
    name: str
    start_utc: float   # hour, e.g. 7.0
    end_utc: float     # hour, end-exclusive; may wrap past midnight


class SessionsCfg(BaseModel):
    windows: List[SessionWindow] = [
        SessionWindow(name="ROLLOVER", start_utc=21.0, end_utc=22.0),
        SessionWindow(name="ASIA", start_utc=22.0, end_utc=7.0),
        SessionWindow(name="LONDON", start_utc=7.0, end_utc=12.0),
        SessionWindow(name="LONDON_NY_OVERLAP", start_utc=12.0, end_utc=16.0),
        SessionWindow(name="NEW_YORK", start_utc=16.0, end_utc=21.0),
    ]
    weekend_open_minutes: int = 120   # first N minutes after the Sunday open


class HydrationCfg(BaseModel):
    # Recursive indicators (EMA, Wilder ATR/RSI/ADX) depend on their seed. Hydrating
    # `ema_warmup_multiple` x the longest lookback makes live values match the backtest's.
    ema_warmup_multiple: float = 5.0
    warmup_bars: int = 100
    safety_margin: float = 0.25
    timeframes: List[str] = ["H4", "H1", "M15", "M5"]
    min_bars: int = 300


class SpreadGateCfg(BaseModel):
    # per-symbol hard ceiling in points; symbols not listed use max_spread_points
    symbol_max_spread_points: Dict[str, float] = {}

    max_spread_points: float = 60.0         # absolute ceiling, in symbol points
    # Learn the ceiling from what this symbol's spread ACTUALLY is, instead of a hand-set number: once enough live
    # quotes have been observed the limit becomes (rolling median spread x dynamic_multiple), so a symbol that
    # normally trades at 640 points is not blocked by a 300-point guess, while a symbol that normally trades at 8
    # points is not allowed to trade at 90. The configured number stays as a backstop: the learned ceiling can never
    # exceed it by more than dynamic_max_expansion.
    dynamic_ceiling: bool = True
    dynamic_multiple: float = Field(default=1.5, ge=1.0, le=5.0)
    dynamic_max_expansion: float = Field(default=3.0, ge=1.0, le=10.0)
    dynamic_min_samples: int = Field(default=200, ge=20, le=100000)

    @field_validator("symbol_max_spread_points")
    @classmethod
    def positive_limits(cls, v):
        if any(x <= 0 for x in v.values()):
            raise ValueError("spread_gate.symbol_max_spread_points values must be positive")
        return v

    max_spread_percentile: float = 0.90     # vs rolling observed distribution for the session
    min_samples_for_percentile: int = 300
    wait_for_spread: bool = True            # WAIT inside signal TTL instead of rejecting


class ExecutionCfg(BaseModel):
    max_signal_age_sec: int = 120
    max_entry_drift_frac_of_stop: float = 0.25
    quick_profit_close_money: float = Field(default=0.0, ge=0.0)  # legacy fixed profit-lock activation
    profit_lock_floor_money: float = Field(default=0.05, ge=0.0)
    profit_lock_stop_buffer_points: float = Field(default=3.0, ge=0.0)
    protection_safety_buffer_points: float = Field(default=5.0, ge=0.0)
    sl_modification_cooldown_sec: float = Field(default=5.0, ge=5.0, le=10.0)
    # Smallest stop improvement worth a broker round trip, as a fraction of the trade's own initial risk.
    # Below this the move is skipped entirely. Without it a trail nudged by a fraction of a point fires an
    # SL_CHANGE on every tick, and since each attempt consumes sl_modification_cooldown_sec, trivial nudges
    # crowd out the meaningful move behind them (15 Oct: ~40 attempts in four minutes on one position, almost
    # all refused by our own cooldown). 0 restores the old behaviour of attempting on any improvement.
    min_sl_step_r: float = Field(default=0.02, ge=0.0, le=0.5)
    # How far the best or worst price must move before it is written to the database again, as a fraction
    # of the trade's own risk. These extremes used to be saved only on a bar close, so a trade that made its
    # final run and closed mid-bar recorded a peak up to fifteen minutes stale: on 16 Oct one GBPJPY trade
    # was recorded peaking at +0.30R while it was closing at its +1.51R target. Every MFE-based conclusion
    # drawn before this fix was computed on numbers that could only understate. 0 writes on every tick.
    peak_persist_step_r: float = Field(default=0.02, ge=0.0, le=1.0)
    # The protected floor is never allowed below this multiple of the trade's estimated round-trip exit
    # cost. Below 1.0 the floor can fire and still book a loss, which is what produced seven negative
    # SMALL_WIN_PROFIT_FLOOR exits on 14-15 Oct.
    profit_lock_cost_multiple: float = Field(default=1.5, ge=1.0, le=5.0)
    hard_profit_target_money: float = Field(default=1.0, ge=0.0)      # 0 = no fixed profit ceiling
    # >0: once profit passes ambition_activation_money the protected floor trails the peak, giving back at most this
    # fraction of it (0.3 -> floor = 70% of peak). 0 keeps the legacy fixed-dollar giveback.
    profit_giveback_fraction: float = Field(default=0.0, ge=0.0, le=0.9)
    small_win_activation_money: float = Field(default=0.20, ge=0.0)
    small_win_floor_money: float = Field(default=0.05, ge=0.0)
    ambition_activation_money: float = Field(default=0.90, ge=0.0)
    ambition_floor_money: float = Field(default=0.50, ge=0.0)
    # The same four levels as a fraction of the trade's initial risk (1R). The larger of the money value and the R
    # value is used. In money alone, $0.20 is 0.01R on a $20-risk trade: the floor then closes winners at about zero
    # while losers still run to -1R, so the average win can never pay for the average loss. 0 = money only.
    small_win_activation_r: float = Field(default=0.5, ge=0.0, le=10.0)
    small_win_floor_r: float = Field(default=0.1, ge=0.0, le=10.0)
    ambition_activation_r: float = Field(default=0.8, ge=0.0, le=10.0)
    ambition_floor_r: float = Field(default=0.4, ge=0.0, le=10.0)
    deviation_points: int = 30
    revalidated_retry_on_requote: bool = True
    allow_limit_orders: bool = False         # LIMIT routing reserved for a later research phase
    limit_order_ttl_sec: int = 600
    close_unprotected_position: bool = True  # if SL cannot be attached, close immediately
    # When the profit-lock stop cannot be moved (broker minimum-distance rules), close at market straight away.
    # False keeps the position on its existing broker stop and retries, so a winner is not cut short.
    close_when_profit_lock_unprotectable: bool = False
    position_sync_sec: float = 2.0
    reconcile_interval_sec: float = 30.0

    @model_validator(mode="after")
    def profit_floor_below_activation(self):
        if self.quick_profit_close_money > 0 and self.profit_lock_floor_money >= self.quick_profit_close_money:
            raise ValueError("profit_lock_floor_money must be less than quick_profit_close_money")
        if self.small_win_activation_money <= self.small_win_floor_money:
            raise ValueError("small_win_activation_money must be greater than small_win_floor_money")
        if self.ambition_activation_money <= self.small_win_activation_money:
            raise ValueError("ambition_activation_money must be greater than small_win_activation_money")
        if not self.small_win_floor_money < self.ambition_floor_money < self.ambition_activation_money:
            raise ValueError("ambition_floor_money must be above small_win_floor_money and below ambition_activation_money")
        r = (self.small_win_floor_r, self.small_win_activation_r, self.ambition_floor_r, self.ambition_activation_r)
        if all(x > 0 for x in r) and not (r[0] < r[1] and r[0] < r[2] < r[3] and r[1] < r[3]):
            raise ValueError("R levels must satisfy small_win_floor_r < small_win_activation_r < ambition_activation_r "
                             "and small_win_floor_r < ambition_floor_r < ambition_activation_r")
        if self.hard_profit_target_money > 0 and self.hard_profit_target_money <= self.ambition_activation_money:
            raise ValueError("hard_profit_target_money must exceed ambition_activation_money")
        return self


class CostCfg(BaseModel):
    commission_per_lot_round_trip: float = 0.0   # account currency, Standard = 0; Raw/Zero: set yours
    # Per-instrument commission (Raw Spread / Zero accounts charge differently per instrument). Keys may use any
    # account's naming ("XAUUSDm" also matches "XAUUSD"). Missing symbols use commission_per_lot_round_trip.
    symbol_commission_per_lot_round_trip: Dict[str, float] = {}
    default_slippage_points: float = 5.0
    slippage_sample_min: int = 20                 # observed fills needed before using empirical slippage
    max_cost_fraction_of_risk: float = 0.25       # costs may not eat more than 25% of 1R
    cost_buffer_r: float = 0.05                   # calibrated buffer; update from research
    # Spread / stop distance. A driftless random walk pays about -(spread / stop) R per trade, so a stop only a few
    # spreads wide is negative by construction, whatever the reward:risk. 0 disables the check.
    max_spread_fraction_of_stop: float = Field(default=0.10, ge=0.0, le=2.0)


class RiskCfg(BaseModel):
    max_risk_fraction_per_trade: float = Field(
        default=MAX_RISK_FRACTION_PER_TRADE, ge=0.0, le=MAX_RISK_FRACTION_PER_TRADE
    )                                               # hard ceiling per trade (not a target)
    research_risk_fraction: float = 0.01          # used only when no validated stats (DRY_RUN/DEMO)
    kelly_fraction: float = 0.25
    kelly_shrinkage_trades: int = 100
    max_total_open_risk_fraction: float = 0.10
    correlation_lookback_days: int = Field(default=30, ge=7, le=90)
    correlation_min_samples: int = Field(default=400, ge=100, le=2000)
    correlation_threshold: float = Field(default=0.70, gt=0.0, le=1.0)
    correlation_group_fraction: float = Field(default=0.50, gt=0.0, le=1.0)
    max_concurrent_positions: int = Field(default=10, ge=1)

    # ---- how several open positions are added up (app/risk/portfolio.py) --------------------------
    # "gross"       : plain arithmetic sum. Two uncorrelated 1% positions consume 2% of the budget, so
    #                 the second strategy to signal is refused on budget the first is not really using.
    # "diversified" : portfolio variance with direction and correlation. Identical to "gross" when the
    #                 book is perfectly correlated in one direction, and more permissive exactly to the
    #                 extent the positions are genuinely independent.
    # Switching this CHANGES LIVE TRADE BEHAVIOUR and therefore restarts the trade record.
    portfolio_risk_mode: str = Field(default="gross", pattern="^(gross|diversified)$")
    # Hard ceiling on the plain sum, enforced in diversified mode too. However well diversified the book
    # looks, total money at risk stays bounded - this is the backstop for the correlation model being
    # wrong, which is how portfolio risk models historically fail.
    max_gross_open_risk_fraction: float = Field(default=0.20, gt=0.0, le=1.0)
    # Correlation every pair is assumed to reach in a crisis. The book must still fit under it.
    stress_correlation: float = Field(default=0.80, ge=0.0, le=1.0)
    # Multiple of the normal budget allowed in that stressed view.
    stressed_risk_tolerance: float = Field(default=1.25, ge=1.0, le=3.0)
    # Lower clip on each pairwise term. 0.0 gives full credit for being UNCORRELATED and none for being
    # negatively correlated: low correlation is measurable, a hedge holding through the event you need
    # it for is a hope, and both legs of an opposed pair can be stopped out in one whipsaw.
    diversification_floor: float = Field(default=0.0, ge=-1.0, le=1.0)
    # What an unknown pair is assumed to be. 1.0 = worst case, so a pair with no correlation history
    # never earns a diversification benefit the data does not support.
    missing_correlation_value: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def gross_ceiling_above_budget(self):
        if self.max_gross_open_risk_fraction < self.max_total_open_risk_fraction:
            raise ValueError("risk.max_gross_open_risk_fraction must be at least "
                             "max_total_open_risk_fraction - a gross ceiling below the ordinary budget "
                             "would make the diversified mode stricter than the gross one")
        return self
    loss_reduction_after: int = Field(default=2, ge=1)
    loss_reduction_multiplier: float = Field(default=0.5, ge=0.0, le=1.0)
    loss_circuit_breaker_losses: int = Field(default=3, ge=1)
    loss_circuit_breaker_window_minutes: int = Field(default=180, ge=1)
    loss_circuit_breaker_cooldown_minutes: int = Field(default=60, ge=0)
    max_margin_usage_fraction: float = 0.5
    min_margin_level_after_pct: float = 300.0
    daily_loss_limit_fraction: float = 0.05
    dd_reduced: float = 0.10
    dd_restricted: float = 0.15
    dd_recovery_band: float = Field(default=0.01, ge=0.0, le=0.05)   # leave REDUCED/RESTRICTED only this far below the threshold
    dd_no_new_trades: float = 0.20
    dd_emergency: float = 0.30
    reduced_multiplier: float = 0.5
    restricted_multiplier: float = 0.25
    high_vol_multiplier: float = 0.5
    max_volume: float = 1.0                       # liquidity / capacity ceiling in lots


class EventsCfg(BaseModel):
    mode: Literal["observe", "block"] = "block"
    pre_minutes: int = 30
    event_minutes: int = 15
    post_minutes: int = 30
    impacts: List[str] = ["HIGH"]
    currencies: List[str] = ["USD"]
    stale_after_days: int = 10      # calendar older than this -> EVENT_STATE UNKNOWN


class CalendarSyncCfg(BaseModel):
    """Optional automatic refresh of the high-impact calendar (instead of hand-maintaining the CSV)."""
    enabled: bool = False
    url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    refresh_minutes: int = Field(default=60, ge=15, le=720)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)

    @field_validator("url")
    @classmethod
    def https_only(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("calendar_sync.url must be https")
        return v


class NewsStraddleCfg(BaseModel):
    """News straddle: BUY STOP + SELL STOP placed just before a high-impact event, OCO once one side fills.

    Distances are ATR multiples (M15 ATR(14)) so the same settings are meaningful on gold, FX and crypto.
    Risk is split between the two legs: with leg_risk_share=0.5 the worst case (both legs stopped out in a
    whipsaw) equals one normal trade's risk.
    """
    enabled: bool = False
    symbols: List[str] = ["XAUUSDm"]
    arm_minutes_before: float = Field(default=5.0, ge=1.0, le=30.0)       # place the pending orders this early
    expire_after_event_minutes: float = Field(default=5.0, ge=1.0, le=60.0)  # cancel unfilled legs after this
    entry_distance_atr: float = Field(default=0.5, gt=0.0, le=3.0)       # stop-order offset from bid/ask
    sl_atr: float = Field(default=1.0, gt=0.0, le=5.0)
    tp_atr: float = Field(default=2.0, gt=0.0, le=10.0)
    leg_risk_share: float = Field(default=0.5, gt=0.0, le=0.5)
    max_atr_quote_age_sec: float = Field(default=10.0, gt=0.0, le=60.0)  # refuse to arm on a stale quote
    # Which releases the straddle trades. Separate from events.currencies, which only decides when the other
    # strategies stand aside: blocking on a UK CPI release is cheap, straddling it is a new trade nobody tested.
    currencies: List[str] = ["USD"]
    # How many SYMBOLS may hold resting straddle legs at once. Straddle legs are NOT counted against
    # risk.max_concurrent_positions, because OCO guarantees at most one leg per symbol can ever become a
    # position - counting both made the first few symbols eat the whole allowance and silently blocked the
    # rest, whichever ones the round robin happened to reach first. Real money at risk stays bounded by the
    # portfolio reservation (risk.max_total_open_risk_fraction), which is the control that actually matters.
    # NOTE: a ceiling greater than or equal to the number of configured symbols constrains nothing. Setting it
    # to the symbol count (13 symbols, ceiling 13) documents an intent the code cannot enforce - every symbol
    # can arm. `ceiling_binds()` says whether it actually limits anything; startup logs a warning when it does
    # not, and tools/doc_facts.py reports it, so the misconfiguration cannot sit there looking like a control.
    max_armed_symbols: int = Field(default=13, ge=1, le=50)

    def ceiling_binds(self) -> bool:
        """True when max_armed_symbols can actually refuse an arming request."""
        if not self.symbols or "*" in self.symbols:
            return True          # wildcard: the symbol count is not known here, assume it may bind
        return self.max_armed_symbols < len(self.symbols)

    @model_validator(mode="after")
    def target_not_inside_stop(self):
        if self.tp_atr < self.sl_atr * 0.5:
            raise ValueError("news_straddle.tp_atr must be at least half of sl_atr")
        return self

    @model_validator(mode="after")
    def warn_non_binding_ceiling(self):
        if self.enabled and not self.ceiling_binds():
            log.warning(
                "news_straddle.max_armed_symbols=%d with %d configured symbols - this ceiling constrains "
                "nothing, every symbol can arm at once. Lower it below %d to make it bind, or accept that "
                "concurrency is bounded only by risk.max_total_open_risk_fraction.",
                self.max_armed_symbols, len(self.symbols), len(self.symbols))
        return self


class AdaptiveCfg(BaseModel):
    """Hourly per-symbol adaptation (see app/adaptive/optimizer.py). Hard bounds are enforced by the validators."""
    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=15, le=1440)
    lookback_bars: int = Field(default=200, ge=100, le=1000)          # M15 bars used for spread statistics
    atr_short_bars: int = Field(default=16, ge=4, le=48)              # 16 x M15 = 4 hours
    atr_long_bars: int = Field(default=96, ge=24, le=400)             # 96 x M15 = 24 hours
    scale_min: float = Field(default=0.75, ge=0.5, le=1.0)
    scale_max: float = Field(default=1.5, ge=1.0, le=2.0)
    max_step: float = Field(default=0.15, gt=0.0, le=0.5)             # max change of vol_scale per cycle
    spread_min_samples: int = Field(default=100, ge=50)
    spread_p90_mult: float = Field(default=1.5, ge=1.0, le=5.0)
    # A derived spread ceiling is only trustworthy while it is fresh. A refresh that cannot run (too little ATR
    # history, an exception) leaves the previous value in place, and the value is also reloaded across restarts,
    # so without an expiry a tight cap computed in one quiet hour could outlive the conditions that justified it
    # - including over a weekend. Past this age the cap is dropped and the configured ceiling applies again.
    max_spread_cap_age_minutes: int = Field(default=180, ge=30, le=1440)

    @model_validator(mode="after")
    def windows(self):
        if self.atr_long_bars <= self.atr_short_bars:
            raise ValueError("adaptive.atr_long_bars must exceed atr_short_bars")
        return self


class DisplayCfg(BaseModel):
    """Dashboard DISPLAY time zone only. Trading logic, sessions, the event calendar and the database stay in UTC."""
    timezone: str = "Asia/Karachi"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"unknown time zone '{v}' (use an IANA name such as Asia/Karachi, Asia/Dubai, UTC)")
        return v


class MicrostructureCfg(BaseModel):
    """Order-flow / microstructure analytics. RESEARCH-ONLY: displayed and stored with signals, never a trade gate."""
    enabled: bool = True
    bucket_ticks: int = Field(default=50, ge=10, le=1000)       # quote updates per VPIN bucket
    vpin_buckets: int = Field(default=20, ge=5, le=200)
    window_ticks: int = Field(default=200, ge=20, le=5000)
    burst_z: float = Field(default=2.5, ge=1.0, le=6.0)
    bin_points: float = Field(default=20.0, ge=1.0, le=1000.0)  # volume-profile price bin, in points
    depth_enabled: bool = True                                   # poll market depth if the broker offers it
    attach_to_signals: bool = True


class ScalingCfg(BaseModel):
    """Scale-in: add to a winning position on the same symbol. Adds are allowed ONLY when every open position on the
    symbol is the same direction, already protected at/beyond breakeven and at least `min_open_r` in profit, so the
    added risk is paid for by locked-in profit. Every portfolio / correlation / margin / daily-loss gate still applies."""
    enabled: bool = True
    max_positions_per_symbol: int = Field(default=3, ge=1, le=6)
    min_open_r: float = Field(default=0.5, ge=0.0, le=5.0)
    risk_multiplier: float = Field(default=0.5, gt=0.0, le=1.0)      # each add is sized at this share of normal risk


class StrategyGateCfg(BaseModel):
    """Research-gate thresholds for ONE strategy. Every field is optional; None means "use the global".

    Why this exists: the eight strategies are not comparable to each other. On the same history a
    breakout strategy produced 34 training trades while mean reversion produced 5 - so a single
    `min_trades` of 30 judges the first and leaves the second permanently unjudged, which looks like
    caution and is actually a blind spot. Likewise a -0.3R average is a disaster for a scalper taking
    many small trades and well inside normal variation for a strategy taking few large ones.

    Set these from each strategy's OWN record rather than by feel: `python tools/gate_thresholds.py`
    reads the live trades and the lab's per-strategy research and prints a recommended block below:
    """
    block_below_r: Optional[float] = Field(default=None, ge=-5.0, le=0.0)
    min_trades: Optional[int] = Field(default=None, ge=5, le=10000)
    max_age_hours: Optional[float] = Field(default=None, ge=6.0, le=2160.0)
    require_evidence: Optional[bool] = None
    unseen_alone: Optional[bool] = None
    note: str = ""              # why this strategy's bar differs, for whoever reads the config next


class ResearchLabCfg(BaseModel):
    """VectorBT + Backtrader research in a separate low-priority process started with the bot. Advisory only."""
    enabled: bool = True
    interval_minutes: int = Field(default=360, ge=30, le=1440)
    startup_delay_seconds: float = Field(default=120.0, ge=0.0, le=3600.0)
    symbols: List[str] = []                       # empty = every broker.symbol_candidates entry
    days_m1: int = Field(default=60, ge=3, le=120)        # needs MT5 "Max bars in chart" >= ~90,000 for 24/7 symbols
    # Longer histories, fetched straight from MT5 (in chunks when one request is refused). 90 days of M15 and 60
    # of M1 left a 2.5-4 week unseen window, too short to tell an edge from a lucky month. 0 = off (M5 falls back
    # to resampled M1; no H4 means no daily trend research).
    days_m15: int = Field(default=730, ge=10, le=1100)
    days_m5: int = Field(default=365, ge=0, le=730)
    days_h4: int = Field(default=3650, ge=0, le=7300)
    # The funnel and the threshold sweep replay every strategy bar by bar; they stay on this recent M15 window.
    recent_days_m15: int = Field(default=90, ge=10, le=365)
    # Statistics that count every attempt (app/research/lab/stats.py): a 95% range, the deflated Sharpe, the
    # probability of backtest overfitting and a random-direction benchmark on every cell, plus as-traded vs
    # bracket-only exits. Adds an `evidence_grade`; never changes a verdict, the gate or the selector.
    evidence_enabled: bool = True
    evidence_min_trades: int = Field(default=20, ge=5, le=1000)
    # Daily trend following (app/research/lab/trend.py) replayed over the H4 history, and the intraday momentum
    # test (last 30 minutes before the New York close) on these symbols. Research only.
    trend_research: bool = True
    intraday_momentum: bool = True
    intraday_momentum_symbols: List[str] = ["NVDAm", "TSLAm", "AAPLm", "AMZNm", "MSFTm"]
    # Real spreads: the spread of the first tick of every M1 bar over the most recent N days (0 = bar spreads only).
    tick_spread_days: int = Field(default=14, ge=0, le=60)
    # Trigger timeframes compared each cycle. M1 uses the VectorBT sweep (and drives the live gate); slower ones use
    # the same rules in the fast simulator. M1 confirms with M15, M5 with M15, M15 with H1.
    timeframes: List[Literal["M1", "M5", "M15"]] = ["M1", "M5", "M15"]
    # Candidate entry rules tested alongside the live strategy (app/research/lab/hypotheses.py). Research only:
    # a rule here never trades. Empty = skip them. Walk-forward is spent only on a rule whose grid test is positive.
    hypotheses: List[str] = ["breakout_retest", "momentum_trend", "session_breakout", "vwap_reversion",
                             "htf_continuation"]
    hypothesis_timeframes: List[Literal["M1", "M5", "M15"]] = ["M5", "M15"]
    # When the markets are shut (weekends, holidays) there is nothing competing for the machine, so the
    # lab spends that window on work too slow to run beside live trading: the cost-model audit, the
    # strategy-risk calculation, the parameter audit and a full ML refit. Research only - it records
    # verdicts and never promotes a strategy or changes a limit.
    idle_research_enabled: bool = True
    # Replay pullback_v1 / mean_reversion_v1 bar by bar and count where each condition stops them.
    strategy_funnel: bool = True
    # Sweep each gating threshold one at a time and report setups per week AND unseen R for every value.
    # Kept to the two M15 strategies by default: the candidate rules are slow to replay and are already swept
    # by the hypothesis grid and Optuna.
    threshold_sweep: bool = True
    threshold_sweep_strategies: List[str] = ["pullback_v1", "mean_reversion_v1"]
    optuna_enabled: bool = True
    optuna_trials: int = Field(default=40, ge=10, le=500)
    optuna_folds: int = Field(default=4, ge=2, le=10)
    sl_atr_grid: List[float] = [1.0, 1.5, 2.0, 3.0]
    rr_grid: List[float] = [1.0, 1.5, 2.0, 3.0]
    train_fraction: float = Field(default=0.7, ge=0.5, le=0.9)
    min_trades: int = Field(default=30, ge=10, le=1000)
    run_backtrader: bool = True
    # Live gate: block a strategy on a symbol where even the lab's BEST stop / reward:risk setting loses clearly.
    # Pooled over train + unseen trades; the unseen part must also be negative. See app/research/lab/gate.py.
    gate_live: bool = True
    # Which strategies the gate may BLOCK. Research now exists for all of them (research_all_strategies),
    # but blocking is opt-in per strategy: adding one here changes live trade behaviour and restarts the
    # trade record, so it is an operator decision made after reading what the research actually says.
    gate_strategies: List[str] = ["micro_scalper"]
    # Research (not blocking) for every configured strategy, at its LIVE parameters, via the generic
    # backtester. No parameter sweep: 8 strategies x 13 symbols of searching would manufacture false
    # findings. See app/research/lab/strategy_research.py.
    research_all_strategies: bool = True
    # Per-strategy thresholds. Any field left None falls back to the global value above, so an empty
    # override map reproduces today's behaviour exactly. Each strategy carries its own indicators and its
    # own R distribution, so each deserves its own bar rather than a shared one.
    gate_overrides: Dict[str, "StrategyGateCfg"] = {}
    # Per-strategy research replays the full M15/H1 (and H4) path, so it is far slower than the
    # micro_scalper fast path. Off by default during a normal cycle; the weekend idle window runs it.
    research_all_in_normal_cycle: bool = False
    # Nightly re-test (the Holly AI loop): once a day, in the first cycle after this UTC hour, run the per-strategy
    # research even on a weekday, so the selector and the gate judge each strategy on day-old evidence rather than
    # last weekend's. 21 = FX rollover, when spreads are wide and little is traded anyway.
    research_all_nightly: bool = True
    research_all_nightly_hour_utc: int = Field(default=21, ge=0, le=23)
    # GLOBAL DEFAULTS. These are the fallback for a strategy with no entry in gate_overrides; they are NOT
    # meant to judge every strategy identically. A scalper and a higher-timeframe continuation strategy
    # differ by an order of magnitude in both trade frequency and R dispersion, so one min_trades leaves
    # the slow strategies permanently unjudged while the fast one is judged, and one block_below_r is a
    # catastrophe for one strategy and ordinary noise for another. Set per-strategy values in
    # gate_overrides; tools/gate_thresholds.py recommends them from each strategy's own record.
    gate_block_below_r: float = Field(default=-0.3, ge=-5.0, le=0.0)
    gate_min_trades: int = Field(default=30, ge=10, le=10000)
    gate_max_age_hours: float = Field(default=48.0, ge=6.0, le=720.0)    # older results are ignored
    # Fail CLOSED instead of open. Off: a missing, stale, incomplete or thin research result permits the trade
    # (the gate only vetoes a proven absence of edge). On: all four of those block it instead. Off is right while
    # the lab is still filling up; on is required before real money - tools/live_readiness.py enforces that.
    gate_require_evidence: bool = False
    # Also block when the UNSEEN trades alone (at least gate_min_trades of them) are below gate_block_below_r, even if
    # the flattering in-sample number lifts the pooled average (XAUUSDm 14 Oct: train +0.28R, unseen -0.34R x 77).
    gate_unseen_alone: bool = True

    @field_validator("sl_atr_grid", "rr_grid")
    @classmethod
    def sane_grid(cls, v):
        if not v or len(v) > 8 or any(x <= 0 or x > 10 for x in v):
            raise ValueError("grids need 1-8 values between 0 and 10")
        return v


class ContextCfg(BaseModel):
    """Related markets attached to every signal as numbers for the learned trade filter (never a gate by itself).
    Each group lists candidate broker names; the first one this account has is used (any account suffix works)."""
    enabled: bool = True
    refresh_seconds: float = Field(default=60.0, ge=15.0, le=3600.0)
    synthetic_dollar_index: bool = True        # build one from EURUSD, USDJPY, GBPUSD, ... when no DXY symbol exists
    groups: Dict[str, List[str]] = {
        "usd_index": ["DXY", "USDX", "DX"],
        "us_stocks": ["US500", "SPX500", "USA500", "SP500"],
        "us_tech": ["USTEC", "NAS100", "US100"],
        "volatility": ["VIX", "VIXX", "VOLX"],
        "us_yields": ["US10Y", "UST10Y", "TNOTE", "US10YR"],
        "oil": ["USOIL", "WTI", "XTIUSD"],
    }


class MLFilterCfg(BaseModel):
    """Learned trade filter (LightGBM) trained by the research lab on the bot's own signals and their outcomes.
    mode: "off"; "shadow" = score every signal, never block; "auto" = block low scores ONLY while the latest model
    beat taking every trade on unseen data (see app/ml/filter.py); "enforce" = block even when not validated."""
    enabled: bool = True
    mode: Literal["off", "shadow", "auto", "enforce"] = "auto"
    strategies: List[str] = ["micro_scalper"]
    min_samples: int = Field(default=500, ge=100, le=1000000)
    lookback_days: int = Field(default=120, ge=7, le=730)
    min_uplift_r: float = Field(default=0.05, ge=0.0, le=2.0)
    min_accepted_test: int = Field(default=30, ge=10, le=10000)
    max_model_age_days: float = Field(default=7.0, ge=0.5, le=90.0)


class SelectorCfg(BaseModel):
    """Evidence-driven strategy selection (app/selection/selector.py): each strategy on each symbol earns a share
    of its normal risk from its own record after costs, and is benched (shadow only) where it loses.
    mode: "off"; "shadow" = compute, record and show, never change routing; "auto" = apply the multipliers.
    Multipliers only ever reduce a strategy's configured risk_multiplier."""
    mode: Literal["off", "shadow", "auto"] = "shadow"
    interval_minutes: int = Field(default=60, ge=10, le=1440)
    lookback_days: int = Field(default=120, ge=7, le=730)
    max_rows: int = Field(default=5000, ge=100, le=100000)
    prior_r: float = Field(default=-0.05, ge=-1.0, le=0.0)
    prior_trades: float = Field(default=20.0, ge=1.0, le=1000.0)
    z: float = Field(default=1.64, ge=0.5, le=4.0)
    default_sd_r: float = Field(default=1.0, gt=0.0, le=10.0)
    min_evidence: float = Field(default=30.0, ge=5.0, le=10000.0)
    min_bench_evidence: float = Field(default=10.0, ge=3.0, le=10000.0)
    min_live_trades_for_active: int = Field(default=20, ge=0, le=10000)
    unbench_margin_r: float = Field(default=0.05, ge=0.0, le=1.0)
    live_weight: float = Field(default=1.0, gt=0.0, le=1.0)
    counterfactual_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    lab_test_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    lab_train_weight: float = Field(default=0.25, ge=0.0, le=1.0)
    lab_max_age_hours: float = Field(default=72.0, ge=6.0, le=720.0)
    # Gates that reject a signal for reasons unrelated to the setup's quality, so the shadow outcome is a fair
    # sample of the strategy. COST/SPREAD/EDGE/ML_FILTER/EVENT/HEALTH rejections are excluded: those judge the
    # setup or its fill conditions, so their outcomes are a selected sample. SIZE matters on a small account,
    # where most signals are refused because the minimum lot is more risk than the budget allows.
    counterfactual_gates: List[str] = ["STRATEGY_STATUS", "REGIME", "RESEARCH", "SELECTOR", "SYMBOL_DISABLED",
                                       "PORTFOLIO", "SIZE", "MARGIN", "RISK", "KILL_SWITCH"]
    active_multiplier: float = Field(default=1.0, gt=0.0, le=1.0)
    probation_multiplier: float = Field(default=0.5, gt=0.0, le=1.0)
    unproven_multiplier: float = Field(default=0.25, gt=0.0, le=1.0)
    regime_veto: bool = True

    @model_validator(mode="after")
    def ordered_multipliers(self):
        if not self.unproven_multiplier <= self.probation_multiplier <= self.active_multiplier:
            raise ValueError("selector multipliers must satisfy unproven <= probation <= active")
        if self.min_bench_evidence > self.min_evidence:
            raise ValueError("selector.min_bench_evidence must not exceed min_evidence")
        return self


class CounterfactualCfg(BaseModel):
    enabled: bool = True
    max_holding_bars: int = 96


class HealthCfg(BaseModel):
    stale_tick_sec: float = 30.0
    # always-open (24/7) symbols are thin on weekends: allow longer gaps between ticks before STALE_DATA
    weekend_stale_tick_sec: float = Field(default=300.0, ge=30.0, le=900.0)
    # How long a symbol may go without a quote before its MARKET is reported closed. Deliberately far more
    # generous than stale_tick_sec: that one flags a data problem on an open market, this one distinguishes
    # "open but thin" from "session has not started". A shut market stops quoting entirely.
    market_closed_quote_age_sec: float = Field(default=180.0, ge=60.0, le=3600.0)
    spread_shock_multiple: float = 3.0
    latency_warn_ms: float = 800.0
    max_tick_jump_atr: float = 3.0
    exec_failure_cluster: int = 3


class ResearchCfg(BaseModel):
    counterfactual_enabled: bool = True
    shadow_strategies_enabled: bool = True
    ai_enabled: bool = False     # AI layer is advisory and not part of v0.1
    ml_enabled: bool = False


class DashboardCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787

    @field_validator("host")
    @classmethod
    def local_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("dashboard must bind to localhost only")
        return v


class TradeAlertsCfg(BaseModel):
    """A message per trade OPEN and CLOSE, on top of the 'wake me up' system alerts.

    Off by default: a dozen messages a day is useful, but it is the operator's call. min_abs_r suppresses the
    smallest closes (set 0.2 to hear only about trades that moved at least a fifth of their risk).
    """
    enabled: bool = False
    on_open: bool = True
    on_close: bool = True
    min_abs_r: float = Field(default=0.0, ge=0.0, le=5.0)


class AlertsCfg(BaseModel):
    """Alert delivery. `channel` decides which transport carries the messages.

    Put the matching credential in .env - see app/notifications/channels.py for what each one needs.
    (Telegram was removed in the Quantara rename: it is blocked in Pakistan and was never usable here.)
    """
    enabled: bool = False
    channel: Literal["discord", "ntfy", "email", "webhook", "none"] = "discord"
    min_seconds_between_same_alert: int = 300
    daily_summary_hour_utc: int = 21
    trade_alerts: TradeAlertsCfg = TradeAlertsCfg()


class PaperCfg(BaseModel):
    starting_balance: float = 100.0
    slippage_points: float = 3.0


class AppConfig(BaseModel):
    environment: Literal["DRY_RUN", "SHADOW", "DEMO", "REAL"] = "DRY_RUN"
    broker: BrokerCfg = BrokerCfg()
    sessions: SessionsCfg = SessionsCfg()
    hydration: HydrationCfg = HydrationCfg()
    spread_gate: SpreadGateCfg = SpreadGateCfg()
    execution: ExecutionCfg = ExecutionCfg()
    cost: CostCfg = CostCfg()
    risk: RiskCfg = RiskCfg()
    events: EventsCfg = EventsCfg()
    calendar_sync: CalendarSyncCfg = CalendarSyncCfg()
    news_straddle: NewsStraddleCfg = NewsStraddleCfg()
    adaptive: AdaptiveCfg = AdaptiveCfg()
    display: DisplayCfg = DisplayCfg()
    microstructure: MicrostructureCfg = MicrostructureCfg()
    scaling: ScalingCfg = ScalingCfg()
    research_lab: ResearchLabCfg = ResearchLabCfg()
    context: ContextCfg = ContextCfg()
    ml_filter: MLFilterCfg = MLFilterCfg()
    selector: SelectorCfg = SelectorCfg()
    counterfactual: CounterfactualCfg = CounterfactualCfg()
    health: HealthCfg = HealthCfg()
    research: ResearchCfg = ResearchCfg()
    dashboard: DashboardCfg = DashboardCfg()
    alerts: AlertsCfg = AlertsCfg()
    paper: PaperCfg = PaperCfg()
    config_version: int = 1

    @model_validator(mode="after")
    def real_execution_disabled(self):
        if self.environment == "REAL":
            raise ValueError("REAL execution is disabled in this release; use DEMO or DRY_RUN")
        return self


# ------------------------------------------------------------------ strategies
class ParamSpec(BaseModel):
    value: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    hypothesis: str = ""


class StrategyCfg(BaseModel):
    # Keys this model does not declare - "disabled_reason", the evidence written next to a switched-off rule - are
    # kept and written back. Ignored, as pydantic does by default, they vanished on the first dashboard
    # enable / disable / promote, which rewrites strategies.json from this model.
    model_config = ConfigDict(extra="allow")

    id: str
    class_name: str
    version: str
    enabled: bool = True
    status: Literal["RESEARCH_ONLY", "SHADOW", "DEMO", "MICRO_LIVE", "VALIDATED", "QUARANTINE", "DISABLED"]
    symbols: List[str] = ["*"]
    allowed_regimes: List[str] = []
    allowed_sessions: List[str] = []           # empty = all (measured, not assumed)
    # Share of the normal per-trade risk this strategy may use. Below 1.0 for a strategy that is being tried out,
    # so several strategies trading at once cannot each take a full-sized position.
    risk_multiplier: float = Field(default=1.0, ge=0.05, le=1.0)
    params: Dict[str, ParamSpec]
    exit_model: Dict[str, Any] = {}
    research_grid: Dict[str, List[Any]] = {}
    stats: Dict[str, Any] = {}                  # validated stats written by `promote`

    def param_values(self) -> Dict[str, Any]:
        return {k: v.value for k, v in self.params.items()}

    def check_params(self) -> List[str]:
        errors = []
        for name, p in self.params.items():
            if isinstance(p.value, (int, float)) and not isinstance(p.value, bool):
                if p.minimum is not None and p.value < p.minimum:
                    errors.append(f"{self.id}.{name}={p.value} < min {p.minimum}")
                if p.maximum is not None and p.value > p.maximum:
                    errors.append(f"{self.id}.{name}={p.value} > max {p.maximum}")
        return errors


class StrategiesFile(BaseModel):
    strategies: List[StrategyCfg]


# ------------------------------------------------------------------ secrets
class Secrets(BaseModel):
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_terminal_path: Optional[str] = None
    database_url: Optional[str] = None
    # ntfy topic URL, Discord webhook URL, or any endpoint accepting a POST. SECRET: whoever holds it can read
    # your alerts, and for Discord can post into your channel.
    notify_url: Optional[str] = None
    # Optional per-category webhooks. Any left empty fall back to notify_url, so configuring none behaves
    # exactly as a single channel did. See app/notifications/routing.py.
    notify_url_health: Optional[str] = None
    notify_url_research: Optional[str] = None
    notify_url_trades: Optional[str] = None
    notify_url_risk: Optional[str] = None
    notify_url_review: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_to: Optional[str] = None
    dashboard_token: Optional[str] = None
    confirm_real: Optional[str] = None

    REAL_CONFIRMATION_PHRASE: str = "I_ACCEPT_REAL_MONEY_RISK"


def load_secrets(env_file: Optional[Path] = None) -> Secrets:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass

    def g(name: str) -> Optional[str]:
        v = os.environ.get(name)
        return v.strip() if v and v.strip() else None

    login = g("MT5_LOGIN")
    return Secrets(
        mt5_login=int(login) if login and login.isdigit() else None,
        mt5_password=g("MT5_PASSWORD"),
        mt5_server=g("MT5_SERVER"),
        mt5_terminal_path=g("MT5_TERMINAL_PATH"),
        database_url=g("DATABASE_URL"),
        notify_url=g("NOTIFY_URL"),
        notify_url_health=g("NOTIFY_URL_HEALTH"),
        notify_url_research=g("NOTIFY_URL_RESEARCH"),
        notify_url_trades=g("NOTIFY_URL_TRADES"),
        notify_url_risk=g("NOTIFY_URL_RISK"),
        notify_url_review=g("NOTIFY_URL_REVIEW"),
        smtp_host=g("SMTP_HOST"),
        smtp_port=int(g("SMTP_PORT") or 587) if (g("SMTP_PORT") or "587").isdigit() else 587,
        smtp_user=g("SMTP_USER"),
        smtp_password=g("SMTP_PASSWORD"),
        smtp_to=g("SMTP_TO"),
        dashboard_token=g("DASHBOARD_TOKEN"),
        confirm_real=g("QUANTARA_CONFIRM_REAL") or g("QUANTARA_CONFIRM_REAL"),
    )


def _migrate_app_config(data: dict) -> dict:
    """Carry an older config.json forward across the Quantara rename.

    Pydantic ignores keys it does not recognise, so a config still carrying the old "telegram" block would load
    without complaint and then deliver no alerts at all - the worst kind of failure, because everything looks
    healthy. The rename is applied here instead, loudly, and a config asking for the removed Telegram transport
    is rejected rather than silently downgraded to no alerts.
    """
    data = dict(data)
    if "telegram" in data:
        block = dict(data.pop("telegram") or {})
        if "alerts" not in data:
            if str(block.get("channel")) == "telegram":
                raise ConfigError(
                    'config.json asks for the "telegram" alert channel, which was removed (it is blocked in '
                    'Pakistan and never delivered). Rename the "telegram" block to "alerts" and set '
                    '"channel": "discord", then put the webhook URL in NOTIFY_URL in your .env.')
            data["alerts"] = block
            log.warning('config.json: the "telegram" block has been read as "alerts". Rename it in the file to '
                        'silence this warning - the name is all that changed.')
    if "vision" in data:
        data.pop("vision")
        log.warning('config.json: the "vision" block was ignored. The Gemini vision gate was removed; you can '
                    'delete that block and GEMINI_API_KEY from your .env.')
    return data


# ------------------------------------------------------------------ manager
# Paths the dashboard may change at runtime. Anything else requires editing the file + restart.
RUNTIME_EDITABLE = {
    "spread_gate.max_spread_points", "spread_gate.max_spread_percentile", "spread_gate.wait_for_spread",
    "execution.max_signal_age_sec", "execution.max_entry_drift_frac_of_stop", "execution.deviation_points",
    "risk.max_risk_fraction_per_trade", "risk.research_risk_fraction", "risk.kelly_fraction",
    "risk.max_total_open_risk_fraction", "risk.max_concurrent_positions", "risk.daily_loss_limit_fraction",
    "risk.max_margin_usage_fraction", "risk.high_vol_multiplier",
    "cost.commission_per_lot_round_trip", "cost.default_slippage_points", "cost.max_cost_fraction_of_risk",
    "cost.cost_buffer_r", "events.mode", "events.pre_minutes", "events.post_minutes",
    "counterfactual.enabled", "research.shadow_strategies_enabled",
}
# Hard safety ceilings the dashboard can never exceed.
SAFETY_CEILINGS = {
    "risk.max_risk_fraction_per_trade": MAX_RISK_FRACTION_PER_TRADE,
    "risk.research_risk_fraction": 0.02,
    "risk.max_total_open_risk_fraction": 0.10,
    "risk.kelly_fraction": 0.5,
    "risk.max_margin_usage_fraction": 0.8,
    "risk.daily_loss_limit_fraction": 0.15,
    "risk.max_concurrent_positions": 10,
}


def _get_path(d: dict, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        cur = cur[part]
    return cur


def _set_path(d: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = value


class ConfigError(Exception):
    pass


class ConfigManager:
    def __init__(self, config_path: Optional[Path] = None, strategies_path: Optional[Path] = None):
        self.config_path = Path(config_path or CONFIG_DIR / "config.json")
        self.strategies_path = Path(strategies_path or CONFIG_DIR / "strategies.json")
        self._lock = threading.RLock()
        self.app = self._load_app()
        self.strategies = self._load_strategies()

    # -------------------------------------------------------------- load
    def _load_app(self) -> AppConfig:
        if not self.config_path.exists():
            return AppConfig()
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.pop("_comment", None)
        data = _migrate_app_config(data)
        try:
            return AppConfig.model_validate(data)
        except ValidationError as e:
            raise ConfigError(f"Invalid {self.config_path.name}: {e}") from e

    def _load_strategies(self) -> StrategiesFile:
        with open(self.strategies_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._strategies_comment = data.pop("_comment", None)       # written back by _write_strategies
        try:
            sf = StrategiesFile.model_validate(data)
        except ValidationError as e:
            raise ConfigError(f"Invalid {self.strategies_path.name}: {e}") from e
        errors = [err for s in sf.strategies for err in s.check_params()]
        if errors:
            raise ConfigError("Parameter registry violations: " + "; ".join(errors))
        ids = [s.id for s in sf.strategies]
        if len(ids) != len(set(ids)):
            raise ConfigError("Duplicate strategy ids in strategies.json")
        return sf

    def strategy(self, sid: str) -> StrategyCfg:
        for s in self.strategies.strategies:
            if s.id == sid:
                return s
        raise ConfigError(f"Unknown strategy {sid}")

    # -------------------------------------------------------------- change
    def apply_change(self, path: str, value: Any, actor: str, reason: str) -> dict:
        """Validate and persist a single runtime change. Returns an audit record."""
        with self._lock:
            if path not in RUNTIME_EDITABLE:
                raise ConfigError(f"'{path}' cannot be changed at runtime (edit config.json and restart)")
            ceiling = SAFETY_CEILINGS.get(path)
            if ceiling is not None and float(value) > ceiling:
                raise ConfigError(f"{path}={value} exceeds safety ceiling {ceiling}")
            if not reason or len(reason.strip()) < 3:
                raise ConfigError("A reason is required for every configuration change")
            data = self.app.model_dump()
            old = _get_path(data, path)
            if isinstance(old, bool):
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int) and not isinstance(old, bool):
                value = int(value)
            elif isinstance(old, float):
                value = float(value)
            _set_path(data, path, value)
            data["config_version"] = self.app.config_version + 1
            try:
                new_cfg = AppConfig.model_validate(data)
            except ValidationError as e:
                raise ConfigError(f"Validation failed: {e}") from e
            self._write_json(self.config_path, new_cfg.model_dump())
            self.app = new_cfg
            return {"config_version": new_cfg.config_version, "actor": actor, "action": "SET",
                    "path": path, "old_value": old, "new_value": value, "reason": reason,
                    "full_config": new_cfg.model_dump()}

    def set_strategy_enabled(self, sid: str, enabled: bool, actor: str, reason: str) -> dict:
        with self._lock:
            if not reason or len(reason.strip()) < 3:
                raise ConfigError("A reason is required")
            data = self.strategies.model_dump()
            found = None
            for s in data["strategies"]:
                if s["id"] == sid:
                    found = s
            if found is None:
                raise ConfigError(f"Unknown strategy {sid}")
            old = found["enabled"]
            found["enabled"] = bool(enabled)
            self.strategies = StrategiesFile.model_validate(data)
            self._write_strategies(data)
            self.app = self.app.model_copy(update={"config_version": self.app.config_version + 1})
            self._write_json(self.config_path, self.app.model_dump())
            return {"config_version": self.app.config_version, "actor": actor,
                    "action": "ENABLE_STRATEGY" if enabled else "DISABLE_STRATEGY",
                    "path": f"strategies.{sid}.enabled", "old_value": old, "new_value": bool(enabled),
                    "reason": reason, "full_config": None}

    def set_strategy_status(self, sid: str, status: str, stats: Optional[dict], actor: str, reason: str) -> dict:
        with self._lock:
            data = self.strategies.model_dump()
            found = None
            for s in data["strategies"]:
                if s["id"] == sid:
                    found = s
            if found is None:
                raise ConfigError(f"Unknown strategy {sid}")
            old = {"status": found["status"], "stats": found.get("stats", {})}
            found["status"] = status
            if stats is not None:
                found["stats"] = stats
            self.strategies = StrategiesFile.model_validate(data)
            self._write_strategies(data)
            return {"config_version": self.app.config_version, "actor": actor, "action": "PROMOTE",
                    "path": f"strategies.{sid}.status", "old_value": old,
                    "new_value": {"status": status, "stats": stats}, "reason": reason, "full_config": None}

    def _write_strategies(self, data: dict) -> None:
        """strategies.json from the model, with the file's own `_comment` kept at the top."""
        comment = getattr(self, "_strategies_comment", None)
        self._write_json(self.strategies_path, {"_comment": comment, **data} if comment else data)

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)   # atomic on Windows and Linux

    def snapshot(self) -> dict:
        with self._lock:
            return {"app": copy.deepcopy(self.app.model_dump()),
                    "strategies": copy.deepcopy(self.strategies.model_dump()),
                    "runtime_editable": sorted(RUNTIME_EDITABLE),
                    "safety_ceilings": SAFETY_CEILINGS}
