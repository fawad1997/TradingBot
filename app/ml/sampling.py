"""Making the learned filter's statistics honest.

Three corrections from Lopez de Prado, all addressing the same underlying problem: the rows the filter
trains on are not independent observations, and almost every statistical method assumes they are.

WHY THEY ARE NOT INDEPENDENT (ch.4)

    Thirteen instruments run concurrently with up to ten open positions. Two trades opened an hour apart
    share most of the price path that decides both outcomes. Lopez de Prado's analogy: blood samples
    where each tube has been contaminated with blood from the nine patients before it, and you must
    still work out what predicts cholesterol.

    The consequence is that the EFFECTIVE sample size is far below the row count. A boosted model
    trained this way fits its in-bag data and generalises at close to chance - which is what an AUC of
    0.517 looks like. Weighting each row by its uniqueness is the correction.

WHAT EACH PIECE DOES

    concurrency / average_uniqueness   ch.4  - how many other labels overlap each one, and therefore
                                              how much independent information it carries
    purge_and_embargo                  ch.7  - drop training rows whose outcome window overlaps the
                                              test window, plus those immediately after it
    triple_barrier_label               ch.3  - label by WHICH barrier was touched first rather than by
                                              the sign of the final result

WHY THE LABEL MATTERS HERE IN PARTICULAR

    The current label is `y = (r > 0)`, which is path-independent. The exit ladder raises a protected
    floor as a trade moves into profit, so it produces a lot of barely-positive finishes. A trade that
    reached +3R and closed at +0.01R currently gets the same label as one that went straight to target.
    Those are not the same event and the model should not be told they are.

Pure functions. No database, no model, no I/O.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# A label whose window cannot be determined is assumed to last this long, so that a missing close time
# produces a conservative (high-overlap, low-weight) estimate rather than a falsely unique one.
ASSUMED_SPAN_MINUTES = 60.0
MIN_WEIGHT = 1e-6


def _when(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------- label spans
def label_spans(rows: Sequence[dict]) -> List[Tuple[datetime, datetime]]:
    """The window of price action each row's outcome depends on: (start, end).

    Start is when the signal was made. End is when the trade closed, or the signal's resolution time
    for a counterfactual. Where the end is unknown or nonsensical (before the start), a fixed span is
    assumed rather than guessing it is instantaneous - treating an unknown span as zero-length would
    make the row look perfectly unique, which is the opposite of conservative.
    """
    out: List[Tuple[datetime, datetime]] = []
    for r in rows:
        t0 = _when(r.get("created_at")) or _when(r.get("opened_at"))
        if t0 is None:
            out.append((datetime.min.replace(tzinfo=timezone.utc),
                        datetime.min.replace(tzinfo=timezone.utc)))
            continue
        t1 = _when(r.get("closed_at")) or _when(r.get("resolved_at"))
        if t1 is None or t1 <= t0:
            t1 = t0 + timedelta(minutes=ASSUMED_SPAN_MINUTES)
        out.append((t0, t1))
    return out


def concurrency(spans: Sequence[Tuple[datetime, datetime]]) -> List[int]:
    """For each label, how many labels (including itself) its window overlaps.

    A sweep over interval endpoints rather than the O(n^2) pairwise comparison, so this stays usable
    as the record grows into the thousands.
    """
    n = len(spans)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: spans[i][0])
    counts = [0] * n
    for pos, i in enumerate(order):
        s_i, e_i = spans[i]
        c = 1                                   # itself
        # later-starting labels, until one starts after this one ends
        for j in order[pos + 1:]:
            if spans[j][0] > e_i:
                break
            c += 1
        # earlier-starting labels that are still open when this one starts
        for j in order[:pos]:
            if spans[j][1] >= s_i:
                c += 1
        counts[i] = c
    return counts


def average_uniqueness(rows: Sequence[dict]) -> List[float]:
    """Uniqueness of each row, in (0, 1]. 1.0 means it overlaps nothing else.

    Lopez de Prado defines it as the average of 1/concurrency over the label's life. The sweep above
    gives one concurrency figure per label, so this is 1/c - the same quantity at the resolution the
    data supports.
    """
    spans = label_spans(rows)
    return [1.0 / c if c > 0 else 1.0 for c in concurrency(spans)]


def sample_weights(rows: Sequence[dict], normalise: bool = True) -> List[float]:
    """Uniqueness as training weights, scaled to average 1.0 so the effective sample size is preserved.

    Without the rescale, weighting by raw uniqueness shrinks every weight below 1 and quietly changes
    the regularisation strength along with the weighting, which would confound the two effects.
    """
    u = average_uniqueness(rows)
    if not u:
        return []
    if not normalise:
        return [max(MIN_WEIGHT, x) for x in u]
    mean_u = sum(u) / len(u)
    if mean_u <= 0:
        return [1.0] * len(u)
    return [max(MIN_WEIGHT, x / mean_u) for x in u]


def effective_sample_size(rows: Sequence[dict]) -> float:
    """How many INDEPENDENT observations the rows are worth. The number that matters for sample-size rules."""
    u = average_uniqueness(rows)
    return float(sum(u))


# ------------------------------------------------------------------------------- purge and embargo
def purge_and_embargo(rows: Sequence[dict], split: int, embargo_fraction: float = 0.01) -> List[int]:
    """Indices of training rows that survive purging, given a chronological train/test split.

    Purge: drop any training row whose outcome window overlaps the test window at all. Its label was
    partly determined by bars the test set also uses.

    Embargo: additionally drop training rows starting just after the test window begins. Serially
    correlated features leak across the boundary even without an outcome overlap.

    Rows are assumed sorted by created_at, which load_rows guarantees.
    """
    n = len(rows)
    if split <= 0 or split >= n:
        return list(range(max(0, min(split, n))))
    spans = label_spans(rows)
    test_start = spans[split][0]
    test_end = max(e for _, e in spans[split:])
    embargo = timedelta(seconds=max(0.0, (test_end - test_start).total_seconds() * embargo_fraction))
    guard_start = test_start - timedelta(seconds=0)
    guard_end = test_end + embargo
    keep = []
    for i in range(split):
        s, e = spans[i]
        overlaps = not (e < guard_start or s > guard_end)
        if not overlaps:
            keep.append(i)
    return keep


# ------------------------------------------------------------------------------- triple barrier
def triple_barrier_label(outcome_r: Optional[float], mfe_r: Optional[float], mae_r: Optional[float],
                         profit_barrier_r: float = 1.0, stop_barrier_r: float = 1.0) -> Optional[int]:
    """1 if the profit barrier was reached first, 0 otherwise. None when it cannot be determined.

    The three barriers are the profit target, the stop, and the expiry of the holding period. The label
    is which one was touched first.

    mfe_r and mae_r give how far the trade travelled each way but not the ORDER in which it did so, so
    the ordering is inferred:

      - profit barrier reached, stop not        -> 1, unambiguous
      - stop reached, profit barrier not        -> 0, unambiguous
      - neither reached (expiry)                -> sign of the final result, which is what the vertical
                                                   barrier means
      - both reached                            -> the final result decides. A trade that touched both
                                                   and finished positive almost certainly hit profit
                                                   first, because hitting the stop first would have
                                                   closed it.

    That last case is why this is worth doing at all: under `y = (r > 0)` a trade that ran to +3R and
    was trailed back to +0.01R looks identical to a clean winner. Here it is still a 1, but the
    machinery now distinguishes WHY, and the barriers can be tuned to separate them.
    """
    r = _num(outcome_r)
    mfe, mae = _num(mfe_r), _num(mae_r)
    if r is None:
        return None
    if mfe is None or mae is None:
        return 1 if r > 0 else 0                       # no path data: fall back to the old label
    hit_profit = mfe >= profit_barrier_r
    hit_stop = abs(mae) >= stop_barrier_r
    if hit_profit and not hit_stop:
        return 1
    if hit_stop and not hit_profit:
        return 0
    return 1 if r > 0 else 0


def label_rows(rows: Sequence[dict], profit_barrier_r: float = 1.0,
               stop_barrier_r: float = 1.0) -> List[Optional[int]]:
    return [triple_barrier_label(r.get("outcome_r"), r.get("mfe_r"), r.get("mae_r"),
                                 profit_barrier_r, stop_barrier_r) for r in rows]


def label_report(rows: Sequence[dict], profit_barrier_r: float = 1.0,
                 stop_barrier_r: float = 1.0) -> Dict[str, Any]:
    """How the triple-barrier labels differ from the plain sign-of-result labels.

    If the two agree everywhere, the change bought nothing on this data and the report says so.
    """
    old = [1 if (_num(r.get("outcome_r")) or 0) > 0 else 0 for r in rows]
    new = label_rows(rows, profit_barrier_r, stop_barrier_r)
    paired = [(o, n) for o, n in zip(old, new) if n is not None]
    changed = sum(1 for o, n in paired if o != n)
    u = average_uniqueness(rows)
    return {
        "rows": len(rows),
        "labelled": len(paired),
        "labels_changed_vs_sign_rule": changed,
        "positive_rate_old": round(sum(o for o, _ in paired) / len(paired), 4) if paired else None,
        "positive_rate_new": round(sum(n for _, n in paired) / len(paired), 4) if paired else None,
        "mean_uniqueness": round(sum(u) / len(u), 4) if u else None,
        "effective_sample_size": round(sum(u), 1) if u else 0.0,
    }
