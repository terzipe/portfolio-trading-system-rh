"""
VIX Trader BOT — percentile-rung threshold table for the SVIX ladder (see
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md v2.0 §2-3). Converts a trailing
10-year distribution of VIX daily closes (data/fred.py) into a static
VIX-level threshold for each configured percentile rung
(config.VIX_PERCENTILE_RUNGS), cached independently of SVIX ladder campaign
state — this describes the historical distribution, not any one campaign,
and persists across arm/idle cycles.

Refresh is weekly, not daily, by design: at a 10-year window, one more day's
close moves a percentile boundary negligibly — the boundary only
meaningfully shifts when an old extreme (2008, 2020) ages out past the
10-year-ago cutoff, which is calendar-driven, not reactive to the current
week's vol. Weekly captures essentially the same information as daily at a
fifth of the external-call surface.

refresh() is called unconditionally (idempotent) from loop_daily_vix.py's
weekday-morning batch; needs_refresh() is what actually gates it to once a
week. A failed fetch never raises out of refresh() — it logs and returns the
untouched cached state, since a bad FRED response must not crash the daily
batch. Staleness is instead surfaced at read time via is_stale(), which
vix_ladder.py uses to decide whether the cache is too old to arm a NEW
campaign on (an already-open campaign keeps running on a stale table rather
than being force-flattened — see spec §2).

Also carries LONG_VOL_TACTICAL's Gate A threshold (monitor/vix_longvol_
gates.py), computed from the SAME weekly FRED fetch but against its OWN,
typically shorter, lookback window (config.VIX_LONGVOL_PERCENTILE_
LOOKBACK_YEARS) — decoupled from the ladder's VIX_PERCENTILE_LOOKBACK_YEARS
on purpose (backtesting 2026-08-26 found a 10y window structurally unable
to fire in a persistently mid-vol regime; a shorter window recalibrates
"cheap" against the current regime instead of a decade-old one). See
get_gate_a_threshold() vs. get_thresholds().
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from config import (
    VIX_PERCENTILE_STATE_FILE,
    VIX_PERCENTILE_RUNGS,
    VIX_PERCENTILE_LOOKBACK_YEARS,
    VIX_PERCENTILE_REFRESH_INTERVAL_DAYS,
    VIX_PERCENTILE_STALE_DAYS,
    VIX_LONGVOL_CHEAP_PERCENTILE,
    VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS,
)
from data.fred import fetch_dated_series, FredError, VIXCLS

# Every percentile the SVIX ladder needs cached, so a single weekly
# refresh/state file serves all of its rungs without a second FRED pull.
# Gate A (LONG_VOL_TACTICAL) is tracked separately below -- it uses its
# OWN, shorter lookback window (VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS),
# not VIX_PERCENTILE_LOOKBACK_YEARS, so it can't share this dict (see
# refresh()/get_gate_a_threshold()).
_ALL_TRACKED_PERCENTILES = sorted(set(VIX_PERCENTILE_RUNGS))
# One fetch covers both windows -- Gate A's is sliced from the same data.
_FETCH_LOOKBACK_YEARS = max(VIX_PERCENTILE_LOOKBACK_YEARS, VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS)


def _default_state() -> dict:
    return {
        "refreshed_at": None, "thresholds": {}, "lookback_years": None, "sample_size": None,
        "gate_a_threshold": None, "gate_a_percentile": None, "gate_a_lookback_years": None,
    }


def _load_state() -> dict:
    if not VIX_PERCENTILE_STATE_FILE.exists():
        return _default_state()
    try:
        state = json.loads(VIX_PERCENTILE_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_state()
    return {**_default_state(), **state}


def _save_state(state: dict) -> None:
    VIX_PERCENTILE_STATE_FILE.write_text(json.dumps(state, indent=2))


def _percentile_value(sorted_closes: list[float], p: float) -> float:
    """Linear-interpolation percentile (same convention as numpy's default
    `percentile` method) -- no numpy dependency needed for a one-shot calc
    over a few thousand closes."""
    n = len(sorted_closes)
    if n == 1:
        return sorted_closes[0]
    rank = (p / 100) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_closes[lo] + frac * (sorted_closes[hi] - sorted_closes[lo])


def compute_thresholds(closes: list[float], percentiles: list[float] | None = None) -> dict[float, float]:
    """Pure function, no I/O — percentile -> VIX-level threshold for each
    of `percentiles` (defaults to config.VIX_PERCENTILE_RUNGS, the SVIX
    ladder's rungs), given historical closes in any order. Independently
    unit-testable without hitting FRED."""
    if not closes:
        raise ValueError("compute_thresholds requires at least one close")
    ordered = sorted(closes)
    percentiles = percentiles if percentiles is not None else VIX_PERCENTILE_RUNGS
    return {p: round(_percentile_value(ordered, p), 2) for p in percentiles}


def _days_since(iso_ts: str | None) -> float | None:
    if iso_ts is None:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds() / 86400


def needs_refresh(state: dict | None = None) -> bool:
    state = state if state is not None else _load_state()
    age = _days_since(state["refreshed_at"])
    return age is None or age >= VIX_PERCENTILE_REFRESH_INTERVAL_DAYS


def is_stale(state: dict | None = None) -> bool:
    state = state if state is not None else _load_state()
    age = _days_since(state["refreshed_at"])
    return age is None or age >= VIX_PERCENTILE_STALE_DAYS


def staleness_status() -> dict:
    """Read-only summary for alerting/dashboard consumers (loop_daily_vix.py,
    regime_trader's dashboard) — never refreshes anything itself. is_stale
    here means "too old to arm a NEW campaign" (VIX_PERCENTILE_STALE_DAYS,
    default 14d / two missed weekly refreshes); an already-open campaign
    keeps running regardless (see vix_ladder.evaluate())."""
    state = _load_state()
    age = _days_since(state["refreshed_at"])
    return {
        "is_stale": is_stale(state),
        "refreshed_at": state["refreshed_at"],
        "age_days": round(age, 1) if age is not None else None,
        "stale_threshold_days": VIX_PERCENTILE_STALE_DAYS,
    }


def refresh(force: bool = False) -> dict:
    """Refresh the cached threshold table from FRED if due (or forced).
    Safe to call every weekday — no-ops on days it isn't due. On fetch
    failure, keeps the existing cache untouched and returns it as-is.

    One dated fetch (covering the longer of the ladder's and Gate A's
    lookback windows) serves both: the ladder's rungs use the full fetch,
    Gate A's threshold is computed from a shorter trailing sub-window
    sliced out of the same data — no second FRED call."""
    state = _load_state()
    if not force and not needs_refresh(state):
        return state
    try:
        dated = fetch_dated_series(VIXCLS, _FETCH_LOOKBACK_YEARS)
    except FredError as exc:
        print(f"[vix_percentile] refresh failed, keeping cached thresholds: {exc}")
        return state

    closes = [v for _, v in dated]
    thresholds = compute_thresholds(closes, percentiles=_ALL_TRACKED_PERCENTILES)

    gate_a_cutoff = date.today() - timedelta(days=365 * VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS)
    gate_a_closes = [v for d, v in dated if d >= gate_a_cutoff]
    gate_a_threshold = (
        compute_thresholds(gate_a_closes, percentiles=[VIX_LONGVOL_CHEAP_PERCENTILE])[VIX_LONGVOL_CHEAP_PERCENTILE]
        if gate_a_closes else None
    )

    state = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {str(p): v for p, v in thresholds.items()},
        "lookback_years": VIX_PERCENTILE_LOOKBACK_YEARS,
        "sample_size": len(closes),
        "gate_a_threshold": gate_a_threshold,
        "gate_a_percentile": VIX_LONGVOL_CHEAP_PERCENTILE,
        "gate_a_lookback_years": VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS,
    }
    _save_state(state)
    print(f"[vix_percentile] refreshed: {thresholds} (n={len(closes)}); "
          f"gate_a_threshold={gate_a_threshold} (pct={VIX_LONGVOL_CHEAP_PERCENTILE}, "
          f"lookback={VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS}y, n={len(gate_a_closes)})")
    return state


def get_thresholds() -> dict[float, float] | None:
    """Ascending percentile -> VIX-level thresholds from cache — the SVIX
    ladder's rungs (config.VIX_PERCENTILE_RUNGS), computed against
    VIX_PERCENTILE_LOOKBACK_YEARS. Returns None if never successfully
    refreshed. Read-only — does not itself fetch."""
    state = _load_state()
    if not state["thresholds"]:
        return None
    return {float(p): v for p, v in sorted(state["thresholds"].items(), key=lambda kv: float(kv[0]))}


def get_gate_a_threshold() -> float | None:
    """LONG_VOL_TACTICAL Gate A's cheap-vol threshold (VIX level at
    config.VIX_LONGVOL_CHEAP_PERCENTILE, computed against config.
    VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS — its own, typically shorter,
    lookback window, decoupled from the SVIX ladder's). Returns None if
    never successfully refreshed. Read-only — does not itself fetch."""
    state = _load_state()
    return state.get("gate_a_threshold")
