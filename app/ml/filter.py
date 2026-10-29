"""Learned trade filter (LightGBM "meta-labelling").

Statistics corrected Oct 2026 per Lopez de Prado: triple-barrier labels (ch.3), uniqueness sample
weights (ch.4) and purged/embargoed splitting (ch.7). See app/ml/sampling.py for why - in short, the
rows are not independent observations and treating them as such is consistent with the AUC of 0.517
this model had been scoring.

The strategy decides WHEN there is a setup. This model decides WHICH of those setups are worth taking, learned from
the bot's own history: every signal's recorded measurements (candle, volume, spread versus stop, order flow, related
markets, regime, session, time of day) and what happened afterwards - the real R of trades that were taken and the
"would have been" R of signals that were blocked (counterfactuals), so blocked signals teach it too.

Honesty rules:
* Time order only. The model is trained on the older 70 % and judged on the newest 30 %, which it never saw. The
  score threshold is chosen inside the training part, never on the test part.
* It only blocks trades (mode "auto") when, on that unseen 30 %, the trades it would have let through did better
  than taking everything - by at least `min_uplift_r` per trade, over at least `min_accepted_test` trades, and
  positive in absolute terms. Otherwise it only records its score next to each signal (shadow).
* Anything recorded AFTER the decision (research verdicts, its own score, and the retired vision grades still
  present on older rows) is never a
  feature, so it cannot learn from the future.

Training runs inside the research-lab process; the trading process only loads the saved model file.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.config import ROOT
from app.ml.sampling import label_report, label_rows, purge_and_embargo, sample_weights

log = logging.getLogger("ml.filter")

MODEL_DIR = ROOT / "research" / "results" / "ml"

# price levels and absolute distances differ per symbol and say nothing on their own; ratios do
EXCLUDE = {"c", "atr_val", "momentum", "stop_distance", "target_distance", "minimum_distance", "spread_price",
           "h4_channel_lower", "h4_channel_upper", "h4_channel_slope", "entry_candidate", "trigger_timeframe"}
EXCLUDE_PREFIX = ("vision_", "ml_", "research_lab", "selector")
NESTED = ("microstructure", "context")


# ------------------------------------------------------------------------------------------------ features
def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def _as_dict(f) -> dict:
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            return {}
    return f if isinstance(f, dict) else {}


def _when(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def flatten(features: Any, direction: Any = None, regime: Any = None, session: Any = None,
            created_at: Any = None, quality: Any = None) -> Dict[str, float]:
    """One signal -> {feature name: number}. Used identically for training and for live scoring."""
    f = _as_dict(features)
    out: Dict[str, float] = {}
    for k, v in f.items():
        if k in EXCLUDE or k.startswith(EXCLUDE_PREFIX):
            continue
        if k in NESTED and isinstance(v, dict):
            for k2, v2 in v.items():
                x = _num(v2)
                if x is not None:
                    out[f"{k}.{k2}"] = x
            continue
        if k == "trigger_pattern" and isinstance(v, str):
            out[f"pattern={v}"] = 1.0
            continue
        x = _num(v)
        if x is not None:
            out[k] = x
    d = str(direction or "").upper()
    if d in ("BUY", "SELL"):
        out["direction"] = 1.0 if d == "BUY" else -1.0
    if regime:
        out[f"regime={regime}"] = 1.0
    if session:
        out[f"session={session}"] = 1.0
    q = _num(quality)
    if q is not None:
        out["quality"] = q
    when = _when(created_at)
    if when is not None:
        hour = when.hour + when.minute / 60.0
        out["hour_sin"] = math.sin(2 * math.pi * hour / 24.0)
        out["hour_cos"] = math.cos(2 * math.pi * hour / 24.0)
        out["weekday"] = float(when.weekday())
    return out


# ------------------------------------------------------------------------------------------------ dataset
SQL = """
SELECT s.id, s.created_at, s.symbol, s.direction, s.regime, s.session, s.quality, s.features,
       t.r_multiple AS trade_r, cf.hypothetical_r AS cf_r,
       t.closed_at AS closed_at, t.mfe_r AS mfe_r, t.mae_r AS mae_r
FROM signals s
LEFT JOIN trades t ON t.signal_id = s.id AND t.state IN ('CLOSED', 'BROKER_CLOSED')
LEFT JOIN counterfactuals cf ON cf.signal_id = s.id AND cf.status = 'RESOLVED'
WHERE s.strategy_id = %s AND s.created_at >= %s
ORDER BY s.created_at
"""


def load_rows(repo, strategy_id: str, days: int) -> List[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        rows = repo.query(SQL, [strategy_id, since])
    except NotImplementedError:                    # in-memory repository (tests): join in Python
        trades = {t.get("signal_id"): t for t in repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]})}
        cfs = {c.get("signal_id"): c for c in repo.select("counterfactuals", {"status": "RESOLVED"})}
        rows = []
        for s in repo.select("signals", {"strategy_id": strategy_id}):
            when = _when(s.get("created_at"))
            if when is not None and when < since:
                continue
            t = trades.get(s["id"]) or {}
            rows.append({**s, "trade_r": t.get("r_multiple"),
                         "cf_r": (cfs.get(s["id"]) or {}).get("hypothetical_r"),
                         "closed_at": t.get("closed_at"), "mfe_r": t.get("mfe_r"), "mae_r": t.get("mae_r")})
        rows.sort(key=lambda r: _when(r.get("created_at")) or since)
    out = []
    seen = set()
    for r in rows:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        outcome = r.get("trade_r") if r.get("trade_r") is not None else r.get("cf_r")
        o = _num(outcome)
        if o is None:
            continue
        r["outcome_r"] = max(-3.0, min(10.0, o))        # a gap through the stop should not dominate the fit
        out.append(r)
    return out


def matrix(rows: List[dict], names: Optional[List[str]] = None) -> Tuple[np.ndarray, List[str]]:
    flats = [flatten(r.get("features"), r.get("direction"), r.get("regime"), r.get("session"), r.get("created_at"),
                     r.get("quality")) for r in rows]
    if names is None:
        counts: Dict[str, int] = {}
        for f in flats:
            for k in f:
                counts[k] = counts.get(k, 0) + 1
        names = sorted(k for k, c in counts.items() if c >= max(5, int(0.02 * len(flats))))
    X = np.full((len(flats), len(names)), np.nan)
    idx = {k: i for i, k in enumerate(names)}
    for row, f in enumerate(flats):
        for k, v in f.items():
            i = idx.get(k)
            if i is not None:
                X[row, i] = v
    return X, names


# ------------------------------------------------------------------------------------------------ training
def _model(seed: int = 7):
    import lightgbm as lgb
    return lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=20,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                              random_state=seed, verbose=-1)


def _pick_threshold(p: np.ndarray, r: np.ndarray, min_keep: int) -> Optional[float]:
    """Score cut-off with the best average R among the kept trades (keeping at least min_keep)."""
    best, best_t = None, None
    for t in np.unique(np.round(np.quantile(p, np.linspace(0.0, 0.8, 33)), 4)):
        keep = p >= t
        if keep.sum() < min_keep:
            continue
        m = float(r[keep].mean())
        if best is None or m > best:
            best, best_t = m, float(t)
    return best_t


def _fit_with_threshold(X, y, r, min_keep, w=None) -> Tuple[Any, Optional[float]]:
    """Threshold chosen on the newest 20 % of the given data with a model fitted on the older 80 %;
    then the model is refitted on all of it.

    `w` carries per-row sample weights (uniqueness). Trades that overlap in time are not independent
    observations, and weighting by uniqueness stops the redundant ones dominating the fit.
    """
    cut = int(len(X) * 0.8)
    wa = None if w is None else np.asarray(w, dtype=float)
    inner = _model().fit(X[:cut], y[:cut], **({} if wa is None else {"sample_weight": wa[:cut]}))
    t = _pick_threshold(inner.predict_proba(X[cut:])[:, 1], r[cut:], min_keep)
    final = _model().fit(X, y, **({} if wa is None else {"sample_weight": wa}))
    return final, t


def train_strategy(rows: List[dict], cfg) -> Dict:
    n = len(rows)
    status: Dict[str, Any] = {"samples": n, "trained_at": datetime.now(timezone.utc).isoformat()}
    if n < cfg.min_samples:
        status.update(status="INSUFFICIENT_DATA", validated=False,
                      note=f"{n} recorded outcomes, {cfg.min_samples} needed")
        return status
    X, names = matrix(rows)
    r = np.array([row["outcome_r"] for row in rows], float)
    # Triple-barrier labels (Lopez de Prado ch.3): which barrier was touched first, not the sign of the
    # final result. Falls back to the sign rule for any row without path data.
    labels = label_rows(rows, getattr(cfg, "profit_barrier_r", 1.0), getattr(cfg, "stop_barrier_r", 1.0))
    y = np.array([lab if lab is not None else (1 if rr > 0 else 0) for lab, rr in zip(labels, r)], dtype=int)
    if y.min() == y.max():
        status.update(status="ONE_CLASS", validated=False, note="every outcome has the same sign")
        return status
    # Sample weights from label uniqueness (ch.4). Concurrent trades share price path, so they are not
    # independent draws; the effective sample size is what this reports, and it is usually much smaller
    # than the row count.
    weights = np.array(sample_weights(rows), dtype=float)
    status["sampling"] = label_report(rows, getattr(cfg, "profit_barrier_r", 1.0),
                                      getattr(cfg, "stop_barrier_r", 1.0))
    split = int(n * 0.7)
    # Purge training rows whose outcome window overlaps the test window, and embargo those straight
    # after it (ch.7). Without this a trade open across the boundary leaks test-period prices into
    # training.
    train_keep = purge_and_embargo(rows, split, getattr(cfg, "embargo_fraction", 0.01))
    status["sampling"]["train_rows_before_purge"] = split
    status["sampling"]["train_rows_after_purge"] = len(train_keep)
    if len(train_keep) < max(20, cfg.min_samples // 4):
        status.update(status="INSUFFICIENT_AFTER_PURGE", validated=False,
                      note=f"{len(train_keep)} training rows survive purging, too few to fit")
        return status
    train_idx = np.array(train_keep, dtype=int)
    min_keep = max(10, cfg.min_accepted_test // 3)
    model, t = _fit_with_threshold(X[train_idx], y[train_idx], r[train_idx], min_keep, weights[train_idx])
    if t is None:
        status.update(status="NO_THRESHOLD", validated=False)
        return status
    p_test = model.predict_proba(X[split:])[:, 1]
    keep = p_test >= t
    base = float(r[split:].mean())
    kept = float(r[split:][keep].mean()) if keep.any() else None
    try:
        from sklearn.metrics import roc_auc_score              # optional nicety
        auc = float(roc_auc_score(y[split:], p_test)) if 0 < y[split:].sum() < len(y[split:]) else None
    except Exception:
        auc = None
    test = {"trades": int(n - split), "baseline_r": round(base, 4), "accepted": int(keep.sum()),
            "accepted_r": None if kept is None else round(kept, 4),
            "uplift_r": None if kept is None else round(kept - base, 4), "auc": None if auc is None else round(auc, 3),
            "threshold": round(t, 4)}
    validated = (kept is not None and keep.sum() >= cfg.min_accepted_test and kept > 0
                 and kept - base >= cfg.min_uplift_r)
    status.update(test=test, validated=bool(validated),
                  status="VALIDATED" if validated else "NOT_VALIDATED")
    # The model that runs live is the one just scored on the unseen 30 %, with ITS threshold. It used to be refitted
    # on all rows with the threshold re-picked on the newest 20 % of them - inside that test window, against the
    # rule at the top of this module - so what went live was never what the test had judged (audit, 23 Oct 2026).
    status["threshold"] = round(t, 4)
    status["features"] = names
    try:
        imp = sorted(zip(names, model.booster_.feature_importance("gain")), key=lambda x: -x[1])[:12]
        status["top_features"] = [[k, round(float(v), 1)] for k, v in imp]
    except Exception:
        pass
    status["_model"] = model
    return status


def save(strategy_id: str, status: Dict, root: Path = MODEL_DIR) -> None:
    root.mkdir(parents=True, exist_ok=True)
    model = status.pop("_model", None)
    if model is not None:
        tmp = root / f"{strategy_id}.lgb.tmp"
        model.booster_.save_model(str(tmp))
        os.replace(tmp, root / f"{strategy_id}.lgb.txt")
    tmp = root / f"{strategy_id}.json.tmp"
    tmp.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, root / f"{strategy_id}.json")


def train_all(cfgm, repo, root: Path = MODEL_DIR) -> Dict:
    cfg = cfgm.app.ml_filter
    if not cfg.enabled:
        return {"status": "OFF"}
    if repo is None:
        return {"status": "NO_DATABASE"}
    try:
        import lightgbm  # noqa: F401
    except ImportError as exc:
        return {"status": "NOT_INSTALLED", "note": f"{exc}; pip install -r requirements.txt"}
    out = {}
    for sid in cfg.strategies:
        try:
            st = train_strategy(load_rows(repo, sid, cfg.lookback_days), cfg)
            save(sid, st, root)
            out[sid] = {k: v for k, v in st.items() if k not in ("features",)}
            log.info("learned filter %s: %s (%s samples)", sid, st.get("status"), st.get("samples"))
        except Exception as exc:
            log.exception("learned filter training failed for %s", sid)
            out[sid] = {"status": "ERROR", "note": str(exc)}
    return out


# ------------------------------------------------------------------------------------------------ live
class LiveFilter:
    """Loaded by every trading core; re-reads the files when the lab saves a new model."""

    def __init__(self, root: Path = MODEL_DIR, recheck_seconds: float = 300.0):
        self.root = root
        self.recheck_seconds = recheck_seconds
        self._models: Dict[str, Tuple[float, Any, dict]] = {}
        self._checked: Dict[str, float] = {}

    def _load(self, sid: str) -> Optional[Tuple[Any, dict]]:
        now = time.time()
        meta_path, model_path = self.root / f"{sid}.json", self.root / f"{sid}.lgb.txt"
        if now - self._checked.get(sid, 0.0) >= self.recheck_seconds:
            self._checked[sid] = now
            try:
                mtime = meta_path.stat().st_mtime
            except OSError:
                self._models.pop(sid, None)
                return None
            cached = self._models.get(sid)
            if cached is None or cached[0] != mtime:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    booster = None
                    if model_path.exists() and meta.get("features"):
                        import lightgbm as lgb
                        booster = lgb.Booster(model_file=str(model_path))
                    self._models[sid] = (mtime, booster, meta)
                except Exception as exc:
                    log.warning("could not load learned filter for %s: %s", sid, exc)
                    self._models.pop(sid, None)
        cached = self._models.get(sid)
        return None if cached is None else (cached[1], cached[2])

    def meta(self, sid: str) -> Optional[dict]:
        got = self._load(sid)
        return None if got is None else got[1]

    def score(self, sid: str, features, direction, regime, session, created_at, quality) -> Optional[Tuple[float, dict]]:
        got = self._load(sid)
        if got is None or got[0] is None:
            return None
        booster, meta = got
        names = meta["features"]
        flat = flatten(features, direction, regime, session, created_at, quality)
        x = np.array([[flat.get(k, np.nan) for k in names]], float)
        return float(booster.predict(x)[0]), meta
