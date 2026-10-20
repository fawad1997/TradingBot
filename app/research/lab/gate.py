"""Turns a research-lab verdict into a live block (or not). Pure function, shared by the trading core and dashboard.

The lab replays micro_scalper's real entry triggers over the last weeks of M1 data, sweeps every stop / reward:risk
combination, picks the best one on the first 70 % and scores it on the last 30 %. If even that BEST combination
loses clearly - across the training window it was picked on AND on the unseen window - the triggers have no edge on
that symbol at any setting we would trade, so the strategy is blocked there.

Why pooled train+test: the training score is the most flattering number the lab can produce (it is the maximum over
the grid). A symbol whose best-case number is still strongly negative is a confident "no", and pooling lets a symbol
with few unseen trades (MSFTm: 47 train + 11 test) be judged on all of them instead of being waved through.

The block lifts by itself when a newer lab run no longer meets the rule, and is ignored when results are stale (the lab
stopped running), so a broken lab can never freeze trading on a symbol indefinitely.

FAIL-OPEN BY DEFAULT, AND WHY THAT IS A CHOICE
----------------------------------------------
This gate answers "has research proved there is no edge here?", not "has research proved there is an edge?".
Of its return paths only ONE blocks: a complete, fresh, sufficiently-large result whose pooled expectancy is below
the limit. Everything else - gate off, no result, stale result, incomplete numbers, too few backtest trades -
PERMITS the trade. Absence of evidence is not treated as evidence of absence.

That is defensible while the lab is still being populated (a half-built lab would otherwise silence the system),
but it is the wrong default for real money, where the safe answer to "I don't know" is "don't trade". Set
`research_lab.gate_require_evidence = true` to invert it: then MISSING / STALE / INCOMPLETE / THIN all block.
`tools/live_readiness.py` requires that flag before a real-money account is considered ready.

Every result carries an `evidence` field so the permit paths are visible rather than silent:

    OK          the lab ran, the numbers are fresh and complete - the verdict means something
    GATE_OFF    the operator turned the gate off; never blocks, whatever gate_require_evidence says
    MISSING     no research result for this symbol at all
    STALE       a result exists but is older than gate_max_age_hours
    INCOMPLETE  a result exists but train/test numbers are unusable
    THIN        fresh and complete, but fewer than gate_min_trades backtest trades behind it
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _age_hours(created_at, now: datetime) -> Optional[float]:
    if created_at is None:
        return None
    try:
        ts = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(str(created_at))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


#: evidence states that mean "the lab has not actually judged this symbol/strategy"
NO_EVIDENCE_STATES = ("MISSING", "STALE", "INCOMPLETE", "THIN")

#: Mirrors the defaults declared on ResearchLabCfg. Used only when a config object does not carry the
#: field at all, so that resolving a threshold can never raise - see the note in Thresholds.pick.
FALLBACKS = {"gate_block_below_r": -0.3, "gate_min_trades": 30, "gate_max_age_hours": 48.0,
             "gate_require_evidence": False, "gate_unseen_alone": True}


class Thresholds:
    """The bar this ONE strategy is held to.

    The eight strategies have different indicators, different trade frequencies and different R
    distributions, so holding them all to one `gate_min_trades` and one `gate_block_below_r` judges the
    busy ones and silently never judges the slow ones. Every value here falls back to the global setting,
    so an empty `gate_overrides` map behaves exactly as before."""

    __slots__ = ("strategy_id", "block_below_r", "min_trades", "max_age_hours", "require_evidence",
                 "unseen_alone", "source")

    def __init__(self, cfg, strategy_id: Optional[str] = None):
        over = None
        if strategy_id:
            over = (getattr(cfg, "gate_overrides", None) or {}).get(strategy_id)

        def pick(name: str, global_name: str):
            v = getattr(over, name, None) if over is not None else None
            if v is not None:
                return v
            # Tolerant on purpose. A config object missing a field must not raise here: an exception
            # inside the gate is caught upstream and turned into "not blocked", so a crash in the code
            # that decides whether to block is itself a silent fail-open. Fall back to the same values
            # the config declares as defaults.
            return getattr(cfg, global_name, FALLBACKS[global_name])

        self.strategy_id = strategy_id
        self.block_below_r = pick("block_below_r", "gate_block_below_r")
        self.min_trades = pick("min_trades", "gate_min_trades")
        self.max_age_hours = pick("max_age_hours", "gate_max_age_hours")
        self.require_evidence = bool(pick("require_evidence", "gate_require_evidence"))
        self.unseen_alone = bool(pick("unseen_alone", "gate_unseen_alone"))
        self.source = "per-strategy override" if over is not None else "global default"

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "block_below_r": self.block_below_r,
                "min_trades": self.min_trades, "max_age_hours": self.max_age_hours,
                "require_evidence": self.require_evidence, "unseen_alone": self.unseen_alone,
                "source": self.source}


def evaluate(entry: Optional[dict], created_at, cfg, now: Optional[datetime] = None,
             strategy_id: Optional[str] = None) -> dict:
    """{"blocked": bool, "evidence": str, "reason": str, ...numbers}. `cfg` is a ResearchLabCfg.

    `strategy_id` selects that strategy's own thresholds from `cfg.gate_overrides`; when it is omitted it
    is taken from the entry itself, and failing that the global defaults apply. Every returned result
    carries `thresholds`, so a block can always be traced to the bar that produced it."""
    now = now or datetime.now(timezone.utc)
    sid = strategy_id or (entry or {}).get("strategy_id")
    th = Thresholds(cfg, sid)
    out = {"blocked": False, "evidence": "OK", "reason": "", "pooled_r": None, "pooled_trades": None,
           "test_r": None, "age_hours": None, "thresholds": th.to_dict()}
    require = th.require_evidence

    def _no_evidence(state: str, detail: str) -> dict:
        """Permit or block depending on gate_require_evidence, but always say which it was."""
        out["evidence"] = state
        if require:
            out["blocked"] = True
            out["reason"] = f"RESEARCH_LAB_NO_EVIDENCE ({detail}; gate_require_evidence is on)"
        else:
            out["reason"] = f"{detail} - permitted, no research evidence either way"
        return out

    if not getattr(cfg, "gate_live", False):
        out["evidence"] = "GATE_OFF"
        out["reason"] = "live gate off"
        return out
    if not entry:
        return _no_evidence("MISSING", "no research result for this symbol")
    age = _age_hours(created_at, now)
    out["age_hours"] = None if age is None else round(age, 1)
    if age is None:
        return _no_evidence("STALE", "research result is stale - no usable timestamp")
    if age > th.max_age_hours:
        return _no_evidence("STALE", f"research result is stale - {age:.0f}h old, "
                                     f"limit {th.max_age_hours:.0f}h")
    verdict = entry.get("verdict")
    if not verdict:
        # A result with no verdict at all is not a judgement, whatever else the row contains. Distinguished
        # from a verdict that simply is not NOT_ROBUST_ON_TEST (e.g. ROBUST), which IS the lab having judged.
        return _no_evidence("INCOMPLETE", "research result carries no verdict")
    if verdict != "NOT_ROBUST_ON_TEST":
        out["reason"] = f"verdict {verdict}"
        return out
    train, test = entry.get("train") or {}, entry.get("test") or {}
    tn, tr = int(_num(train.get("n_trades")) or 0), _num(train.get("expectancy_r"))
    vn, vr = int(_num(test.get("n_trades")) or 0), _num(test.get("expectancy_r"))
    if tr is None or vr is None or tn + vn == 0:
        return _no_evidence("INCOMPLETE", "research result is missing train/test expectancy or trade counts")
    pooled = (tn * tr + vn * vr) / (tn + vn)
    out.update(pooled_r=round(pooled, 3), pooled_trades=tn + vn, test_r=vr)
    if tn + vn < th.min_trades:
        return _no_evidence("THIN", f"only {tn + vn} backtest trades (need {th.min_trades})")
    if vr >= 0:
        out["reason"] = "unseen data not negative"
        return out
    unseen_alone = (th.unseen_alone and vn >= th.min_trades and vr < th.block_below_r)
    if pooled >= th.block_below_r and not unseen_alone:
        out["reason"] = f"best setting {pooled:+.2f}R per trade, above the {th.block_below_r:+.2f}R limit"
        return out
    out["blocked"] = True
    if pooled < th.block_below_r:
        out["reason"] = (f"RESEARCH_LAB_NO_EDGE (best setting {pooled:+.2f}R per trade over {tn + vn} backtest "
                         f"trades, unseen {vr:+.2f}R; limit {th.block_below_r:+.2f}R)")
    else:
        out["reason"] = (f"RESEARCH_LAB_NO_EDGE (unseen {vr:+.2f}R per trade over {vn} trades; the in-sample "
                         f"{tr:+.2f}R did not hold; limit {th.block_below_r:+.2f}R)")
    return out
