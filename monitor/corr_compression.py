"""VIX Trader BOT — realized-correlation COMPRESSION early-warning for the
SVIX manual campaign (monitor/svix_manual_campaign.py, consumed via
monitor/vix_leading_signals.py as a tier-1-level exit arm, OR'd with the
VVIX/VIX divergence tier 1).

Signal: the config.SVIX_CORR_WINDOW-day equal-weight average pairwise
realized correlation of the fixed large-cap basket config.SVIX_CORR_BASKET,
percentile-ranked against its own trailing config.SVIX_CORR_LOOKBACK_YEARS
of daily readings. "Compressed" when that percentile <=
config.SVIX_CORR_PERCENTILE (the 5th by default) — extreme short-vol
complacency / dispersion crowding, which historically leads a VIX spike by
~2–4 weeks (Stage-1 diagnostic 2026-09-08: at the 5th pct the 21d signal
led VIX +15%/20d and SVIX -14% max-drawdown/20d, and fired 10 days before
the Feb-2025 tariff selloff — a setup the VVIX/VIX compression tier missed).

Computed once per trading day and cached to config.SVIX_CORR_STATE_FILE.
The fast exit-poll loop calls is_compressed() every cycle (~15s–5min) but
only the first call of a new calendar day pays the yfinance batch fetch +
correlation math; the rest read the cache. Fail closed (compressed=False)
on any fetch/compute failure, EXCEPT that a transient failure falls back to
the last cache while it is <= config.SVIX_CORR_STALE_DAYS old (same policy
as monitor/vix_percentile.py — one bad yfinance call shouldn't silently
disarm the signal).

`dry_run=True` computes/returns the same result but never persists a fresh
compute — "dry run leaves no trace", matching vix_ladder / vix_leading_
signals / svix_manual_campaign.

This module re-implements the correlation math rather than sharing it with
backtest_svix_manual.py: the backtest needs the full historical series with
a rolling as-of percentile, this needs one current reading — same split of
responsibilities the rest of this codebase already uses (see the backtest's
module docstring).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import numpy as np

from config import (
    SVIX_CORR_BASKET,
    SVIX_CORR_WINDOW,
    SVIX_CORR_PERCENTILE,
    SVIX_CORR_LOOKBACK_YEARS,
    SVIX_CORR_STALE_DAYS,
    SVIX_CORR_STATE_FILE,
)

_TRADING_DAYS_PER_YEAR = 252


def _fetch_basket_closes():
    """Daily adjusted closes for SVIX_CORR_BASKET, oldest first, any
    all-NaN column dropped then any row with a gap dropped. None on any
    failure or too-thin history (fail closed)."""
    try:
        import yfinance as yf

        period = f"{max(1, round(SVIX_CORR_LOOKBACK_YEARS) + 1)}y"
        df = yf.download(
            SVIX_CORR_BASKET, period=period, interval="1d", progress=False, auto_adjust=True
        )["Close"]
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    df = df.dropna(axis=1, how="all").dropna(how="any")
    if df.shape[1] < 10 or len(df) < SVIX_CORR_WINDOW + 20:
        return None
    return df


def _avg_pairwise_corr(returns_window: np.ndarray) -> float:
    c = np.corrcoef(returns_window.T)
    n = c.shape[0]
    return float((np.nansum(c) - n) / (n * (n - 1)))


def compute() -> dict:
    """Recompute from scratch (yfinance fetch + correlation math). Returns
    {date, rc, percentile, compressed, computed_at}; compressed is False and
    rc/percentile are None on any failure. Never persists."""
    stamp = datetime.now(timezone.utc).isoformat()
    fail = {"date": date.today().isoformat(), "rc": None, "percentile": None,
            "compressed": False, "computed_at": stamp}

    closes = _fetch_basket_closes()
    if closes is None:
        return fail

    rets = np.log(closes).diff().dropna().values
    w = SVIX_CORR_WINDOW
    if len(rets) <= w + 5:
        return fail

    rc_series = np.array([_avg_pairwise_corr(rets[i - w:i]) for i in range(w, len(rets))])
    rc_now = rc_series[-1]
    if not np.isfinite(rc_now):
        return fail

    hist_days = int(SVIX_CORR_LOOKBACK_YEARS * _TRADING_DAYS_PER_YEAR)
    hist = rc_series[-hist_days:] if len(rc_series) > hist_days else rc_series
    pct = float((hist <= rc_now).mean() * 100.0)

    return {
        "date": date.today().isoformat(),
        "rc": float(round(rc_now, 4)),
        "percentile": round(pct, 1),
        "compressed": bool(pct <= SVIX_CORR_PERCENTILE),
        "computed_at": stamp,
    }


def _load_cache() -> dict | None:
    if not SVIX_CORR_STATE_FILE.exists():
        return None
    try:
        return json.loads(SVIX_CORR_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _cache_age_days(cached: dict) -> int:
    try:
        return (date.today() - date.fromisoformat(cached["date"])).days
    except (KeyError, ValueError, TypeError):
        return 10 ** 6


def get_status(dry_run: bool = False) -> dict:
    """Cached realized-corr compression status. Recomputes only when the
    cache is missing or its date != today. On a fresh-compute failure, keeps
    serving a cache that is still within SVIX_CORR_STALE_DAYS; older than
    that (or no cache) -> the failed (compressed=False) result."""
    cached = _load_cache()
    today = date.today().isoformat()
    if cached is not None and cached.get("date") == today:
        return cached

    fresh = compute()
    if fresh.get("rc") is not None:
        if not dry_run:
            try:
                SVIX_CORR_STATE_FILE.write_text(json.dumps(fresh, indent=2))
            except OSError:
                pass
        return fresh

    # fresh compute failed -- fall back to a recent cache rather than
    # flipping the signal off on a transient yfinance blip
    if cached is not None and _cache_age_days(cached) <= SVIX_CORR_STALE_DAYS:
        return cached
    return fresh


def is_compressed(dry_run: bool = False) -> bool:
    return bool(get_status(dry_run=dry_run).get("compressed"))
