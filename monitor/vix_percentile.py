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
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from config import (
    VIX_PERCENTILE_STATE_FILE,
    VIX_PERCENTILE_RUNGS,
    VIX_PERCENTILE_LOOKBACK_YEARS,
    VIX_PERCENTILE_REFRESH_INTERVAL_DAYS,
    VIX_PERCENTILE_STALE_DAYS,
    VIX_LONGVOL_CHEAP_PERCENTILE,
)
from data.fred import fetch_vix_closes, FredError

# Every percentile anyone needs cached, so a single weekly refresh/state
# file serves both the SVIX ladder rungs and the LONG_VOL_TACTICAL cheap-
# vol gate (monitor/vix_longvol_gates.py) without a second FRED pull/cache.
_ALL_TRACKED_PERCENTILES = sorted(set(VIX_PERCENTILE_RUNGS) | {VIX_LONGVOL_CHEAP_PERCENTILE})


def _default_state() -> dict:
    return {"refreshed_at": None, "thresholds": {}, "lookback_years": None, "sample_size": None}


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


def refresh(force: bool = False) -> dict:
    """Refresh the cached threshold table from FRED if due (or forced).
    Safe to call every weekday — no-ops on days it isn't due. On fetch
    failure, keeps the existing cache untouched and returns it as-is."""
    state = _load_state()
    if not force and not needs_refresh(state):
        return state
    try:
        closes = fetch_vix_closes(VIX_PERCENTILE_LOOKBACK_YEARS)
    except FredError as exc:
        print(f"[vix_percentile] refresh failed, keeping cached thresholds: {exc}")
        return state
    thresholds = compute_thresholds(closes, percentiles=_ALL_TRACKED_PERCENTILES)
    state = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {str(p): v for p, v in thresholds.items()},
        "lookback_years": VIX_PERCENTILE_LOOKBACK_YEARS,
        "sample_size": len(closes),
    }
    _save_state(state)
    print(f"[vix_percentile] refreshed: {thresholds} (n={len(closes)})")
    return state


def get_thresholds() -> dict[float, float] | None:
    """Ascending percentile -> VIX-level thresholds from cache, filtered to
    just the SVIX ladder's rungs (config.VIX_PERCENTILE_RUNGS) — the cached
    state may hold additional percentiles for other consumers (see
    get_percentile_level()), but vix_ladder.py's rung-walk must only ever
    see its own rungs. Returns None if never successfully refreshed.
    Read-only — does not itself fetch."""
    state = _load_state()
    if not state["thresholds"]:
        return None
    wanted = set(VIX_PERCENTILE_RUNGS)
    return {
        float(p): v for p, v in sorted(state["thresholds"].items(), key=lambda kv: float(kv[0]))
        if float(p) in wanted
    }


def get_percentile_level(p: float) -> float | None:
    """VIX level at percentile `p` from the shared cache, or None if it
    hasn't been computed yet (e.g. `p` isn't in _ALL_TRACKED_PERCENTILES,
    or nothing has ever refreshed successfully). Used by consumers other
    than the SVIX ladder — e.g. monitor/vix_longvol_gates.py's cheap-vol
    gate — that need one specific percentile rather than the ladder's rung
    set."""
    state = _load_state()
    for key, value in state["thresholds"].items():
        if float(key) == float(p):
            return float(value)
    return None
