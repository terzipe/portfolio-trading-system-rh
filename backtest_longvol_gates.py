#!/usr/bin/env python3
"""
backtest_longvol_gates.py — historical signal-quality backtest of the
LONG_VOL_TACTICAL data-driven gates (monitor/vix_longvol_gates.py) against
real VIX/VIX3M (FRED) and VXX (Yahoo Finance) daily closes, plus a
systematic parameter sweep across Gate A percentile / Gate C momentum
threshold / shared lookback / min-gates-to-confirm.

Does NOT simulate options P&L (needs paid historical options chains, out of
scope). Instead measures signal quality the cheap way: each time the score
newly confirms (edge-triggered — a fresh signal, not re-counted every day it
stays confirmed, mirroring how vix_signals.decide_actions() only enters once
per open position), record VXX's own forward return at a few horizons — a
direct proxy for whether a 21-45 DTE VXX call bought on that signal would
likely have paid off.

Gate B and Gate C's "N sessions ago" comparison both use VXX's OWN trading
calendar (not FRED's, which can have a slightly different holiday calendar)
so both lookbacks are counted consistently — a correctness fix over the
first draft of this script, which used FRED's calendar for Gate B and VXX's
for Gate C.

Gate A (cheap-vol percentile) is evaluated with a weekly-refreshed,
no-lookahead trailing-10y threshold, same discipline as
backtest_svix_ladder.py.

Usage:
  "Portfolio Trading System-RH/venv/bin/python" backtest_longvol_gates.py           # single run at config defaults
  "Portfolio Trading System-RH/venv/bin/python" backtest_longvol_gates.py --sweep   # parameter grid sweep
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

SYSTEM_DIR = Path(__file__).parent
sys.path.insert(0, str(SYSTEM_DIR))

import config  # noqa: E402
from monitor import vix_percentile  # noqa: E402

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch_fred_series(series_id: str, start: date, end: date) -> pd.Series:
    resp = requests.get(FRED_CSV_URL, params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()}, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), na_values=["."])
    date_col = "observation_date" if "observation_date" in df.columns else "DATE"
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.dropna(subset=[series_id])
    return df.set_index(date_col)[series_id].astype(float)


def fetch_ticker_series(ticker: str) -> pd.Series:
    df = yf.download(ticker, period="max", progress=False, auto_adjust=False)
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna()


def fetch_data() -> pd.DataFrame:
    """One-time fetch + alignment onto VXX's own trading calendar (the
    tradable universe -- both tickers share the same underlying market
    days) — VIX/VIX3M/UVXY are forward-filled onto it. Reused across every
    sweep combination so the sweep doesn't re-hit the network 100+ times.
    UVXY is fetched alongside VXX so momentum_gate_ticker can be tested
    against either without a second network round-trip (2026-08-27,
    evaluating a UVXY-specific long-vol alert)."""
    vxx = fetch_ticker_series("VXX")
    uvxy = fetch_ticker_series("UVXY")
    lookback_buffer = timedelta(days=365 * config.VIX_PERCENTILE_LOOKBACK_YEARS + 60)
    start = vxx.index.min().date() - lookback_buffer
    vix_full = fetch_fred_series("VIXCLS", start, date.today())
    vix3m_full = fetch_fred_series("VXVCLS", start, date.today())

    df = pd.DataFrame({"vxx": vxx})
    df["uvxy"] = uvxy.reindex(df.index, method="ffill")
    df["vix"] = vix_full.reindex(df.index, method="ffill")
    df["vix3m"] = vix3m_full.reindex(df.index, method="ffill")
    df.attrs["vix_full"] = vix_full  # kept for Gate A's trailing-10y lookback, predates VXX's own start
    return df


def _gate_a_threshold_series(df: pd.DataFrame, cheap_percentile: float, lookback_years: float | None = None) -> pd.Series:
    """Weekly-refreshed, no-lookahead trailing-N-year percentile threshold,
    aligned to df.index (N = lookback_years, default config.
    VIX_PERCENTILE_LOOKBACK_YEARS = 10). Recomputes only on the configured
    refresh interval (not every day) — matches production cadence and
    keeps the sweep fast. A shorter lookback recalibrates "cheap" against
    the CURRENT regime instead of a decade that may include stretches no
    longer representative (2026-08-26: VIX hasn't touched the 10y-15th-pct
    threshold at all in the last 2 years)."""
    lookback_years = lookback_years if lookback_years is not None else config.VIX_PERCENTILE_LOOKBACK_YEARS
    vix_full = df.attrs["vix_full"]
    levels = []
    cache_as_of, cache_level = None, None
    for d in df.index:
        if cache_as_of is None or (d - cache_as_of).days >= config.VIX_PERCENTILE_REFRESH_INTERVAL_DAYS:
            window_start = d - pd.Timedelta(days=365 * lookback_years)
            closes = vix_full[(vix_full.index > window_start) & (vix_full.index <= d)].tolist()
            cache_level = vix_percentile.compute_thresholds(closes, percentiles=[cheap_percentile])[cheap_percentile]
            cache_as_of = d
        levels.append(cache_level)
    return pd.Series(levels, index=df.index)


def run_backtest(
    df: pd.DataFrame, lookback_sessions: int, min_pct: float, cheap_percentile: float, min_gates: int,
    gate_b_min_pct: float = 0.0, require_gate_a: bool = False, gate_a_lookback_years: float | None = None,
    ticker: str = "vxx",
) -> dict:
    """require_gate_a=True switches scoring from equal-weighted "score >=
    min_gates" to "Gate A AND score >= min_gates" -- i.e. Gate A (cheap
    vol) becomes a hard prerequisite rather than one of three equally-
    weighted votes, motivated by the 2026-08-25 gate-pair breakdown
    (almost all the adopted config's edge came from signals where A had
    confirmed; when A was off, results were negative). min_gates still
    counts all three gates toward the total in this mode (e.g. min_gates=2
    with require_gate_a=True means "A confirmed, plus at least one of
    B/C").

    `ticker` ("vxx" or "uvxy") controls BOTH Gate C's momentum source and
    which instrument's own forward returns measure signal quality -- for a
    UVXY-specific alert, momentum and P&L exposure should both be UVXY's,
    not VXX's (2026-08-27). Gate A/B are index-level (VIX/VIX3M), unaffected
    by ticker choice."""
    threshold = _gate_a_threshold_series(df, cheap_percentile, lookback_years=gate_a_lookback_years)
    gate_a = df["vix"] < threshold

    ratio = df["vix"] / df["vix3m"]
    ratio_then = ratio.shift(lookback_sessions)
    gate_b = ratio <= ratio_then * (1 - gate_b_min_pct)

    px = df[ticker]
    px_then = px.shift(lookback_sessions)
    gate_c = (px / px_then - 1) >= min_pct

    score = gate_a.astype(int) + gate_b.astype(int) + gate_c.astype(int)
    confirmed = (score >= min_gates) & gate_a if require_gate_a else (score >= min_gates)
    new_signal = confirmed & ~confirmed.shift(1, fill_value=False)

    fwd_returns = {h: (px.shift(-h) / px - 1) for h in (10, 21, 45)}

    signals = []
    for d in df.index[new_signal]:
        signals.append({
            "date": d.date().isoformat(), "score": int(score.loc[d]), "vix": float(df["vix"].loc[d]),
            "entry_price": float(px.loc[d]),
            "gates": (bool(gate_a.loc[d]), bool(gate_b.loc[d]), bool(gate_c.loc[d])),  # (A, B, C)
            "fwd_return": {h: (float(fwd_returns[h].loc[d]) if pd.notna(fwd_returns[h].loc[d]) else None) for h in (10, 21, 45)},
        })
    return {"signals": signals}


def run_rotation_backtest(
    df: pd.DataFrame, lookback_sessions: int, min_pct: float, cheap_percentile: float, min_gates: int,
    gate_b_min_pct: float = 0.0, require_gate_a: bool = True, gate_a_lookback_years: float | None = None,
    tie_pct: float | None = None,
) -> dict:
    """Simulates the ACTUAL VXX/UVXY rotation rule (vix_regime.
    _pick_longvol_ticker(), confirmed 2026-09-01, refined 2026-09-02): trade
    whichever ticker's OWN gates confirm. When both do, momentum magnitude
    is only compared when BOTH tickers' Gate C individually/independently
    confirmed (a genuine momentum reading on both sides) -- Gate A/B are
    index-level and identical for both tickers, so confirmation via A+B
    alone (Gate C not itself confirming for either) is the common case and
    does NOT trigger a momentum comparison; it defaults straight to VXX.
    Ties within `tie_pct` (default config.VIX_LONGVOL_MOMENTUM_TIE_PCT) also
    default to VXX. Distinct from run_backtest() above, which measures each
    ticker's signal quality in ISOLATION -- this measures what the COMBINED
    rotation strategy would actually have traded and its forward returns,
    i.e. the thing that's actually live now. Same "does not simulate
    options P&L" caveat as run_backtest()."""
    tie_pct = tie_pct if tie_pct is not None else config.VIX_LONGVOL_MOMENTUM_TIE_PCT
    threshold = _gate_a_threshold_series(df, cheap_percentile, lookback_years=gate_a_lookback_years)
    gate_a = df["vix"] < threshold
    ratio = df["vix"] / df["vix3m"]
    ratio_then = ratio.shift(lookback_sessions)
    gate_b = ratio <= ratio_then * (1 - gate_b_min_pct)

    vxx_px, uvxy_px = df["vxx"], df["uvxy"]
    vxx_mom = vxx_px / vxx_px.shift(lookback_sessions) - 1
    uvxy_mom = uvxy_px / uvxy_px.shift(lookback_sessions) - 1
    gate_c_vxx = vxx_mom >= min_pct
    gate_c_uvxy = uvxy_mom >= min_pct

    score_vxx = gate_a.astype(int) + gate_b.astype(int) + gate_c_vxx.astype(int)
    score_uvxy = gate_a.astype(int) + gate_b.astype(int) + gate_c_uvxy.astype(int)
    confirmed_vxx = (score_vxx >= min_gates) & gate_a if require_gate_a else (score_vxx >= min_gates)
    confirmed_uvxy = (score_uvxy >= min_gates) & gate_a if require_gate_a else (score_uvxy >= min_gates)

    # Edge-triggered on the ROTATION as a whole (either ticker confirming) --
    # matches vix_signals.decide_actions()'s "only enter once per open
    # position" semantics: once holding, later gate flips (e.g. VXX
    # confirms, then UVXY also confirms with stronger momentum) don't
    # re-decide the ticker, same as live.
    rotation_confirmed = confirmed_vxx | confirmed_uvxy
    new_signal = rotation_confirmed & ~rotation_confirmed.shift(1, fill_value=False)

    picked_via = {
        "vxx_only": 0, "uvxy_only": 0, "both_vxx_won": 0, "both_uvxy_won": 0,
        "both_tied_vxx": 0, "both_ab_only_vxx_default": 0,
    }
    signals = []
    for d in df.index[new_signal]:
        vxx_c, uvxy_c = bool(confirmed_vxx.loc[d]), bool(confirmed_uvxy.loc[d])
        vxx_gc, uvxy_gc = bool(gate_c_vxx.loc[d]), bool(gate_c_uvxy.loc[d])
        vxx_m = float(vxx_mom.loc[d]) if pd.notna(vxx_mom.loc[d]) else 0.0
        uvxy_m = float(uvxy_mom.loc[d]) if pd.notna(uvxy_mom.loc[d]) else 0.0
        if vxx_c and not uvxy_c:
            ticker, path = "vxx", "vxx_only"
        elif uvxy_c and not vxx_c:
            ticker, path = "uvxy", "uvxy_only"
        elif not (vxx_gc and uvxy_gc):
            # Both confirmed, but not via a genuine Gate C reading on both
            # sides (e.g. A+B alone) -- momentum magnitude isn't a real
            # signal here, so skip the comparison and default to VXX.
            ticker, path = "vxx", "both_ab_only_vxx_default"
        elif abs(vxx_m - uvxy_m) < tie_pct:
            ticker, path = "vxx", "both_tied_vxx"
        elif vxx_m > uvxy_m:
            ticker, path = "vxx", "both_vxx_won"
        else:
            ticker, path = "uvxy", "both_uvxy_won"
        picked_via[path] += 1

        px = df[ticker]
        i = df.index.get_loc(d)
        entry_price = float(px.iloc[i])
        fwd = {}
        for h in (10, 21, 45):
            j = i + h
            fwd[h] = (float(px.iloc[j]) / entry_price - 1) if j < len(df) else None
        signals.append({
            "date": d.date().isoformat(), "ticker": ticker, "path": path,
            "vxx_confirmed": vxx_c, "uvxy_confirmed": uvxy_c,
            "vxx_gate_c": vxx_gc, "uvxy_gate_c": uvxy_gc,
            "vxx_momentum": vxx_m, "uvxy_momentum": uvxy_m,
            "entry_price": entry_price, "fwd_return": fwd,
        })
    return {"signals": signals, "picked_via": picked_via}


def summarize_rotation(result: dict) -> None:
    signals, picked_via = result["signals"], result["picked_via"]
    print(f"\n  ROTATION strategy: {len(signals)} signal(s)")
    for h in (10, 21, 45):
        s = _stats(signals, h)
        if s is None:
            continue
        print(f"    +{h}d: n={s['n']}  avg fwd return={s['avg']:+.1%}  win rate={s['win_rate']:.0%}  "
              f"best={s['best']:+.1%}  worst={s['worst']:+.1%}")
    n_vxx = sum(1 for s in signals if s["ticker"] == "vxx")
    n_uvxy = sum(1 for s in signals if s["ticker"] == "uvxy")
    print(f"\n  Ticker picked: VXX {n_vxx} ({n_vxx / len(signals):.0%})  UVXY {n_uvxy} ({n_uvxy / len(signals):.0%})" if signals else "  No signals.")
    print("  Path breakdown:")
    for path, n in picked_via.items():
        print(f"    {path}: {n}")


def breakdown_by_gate_pair(signals: list[dict], horizon: int = 21) -> None:
    """Splits signals by WHICH pair of gates confirmed (only meaningful at
    min_gates=2, where exactly one gate is 'off' per signal) -- shows
    whether one gate is carrying the signal and the others are noise."""
    groups: dict[str, list[dict]] = {}
    for s in signals:
        a, b, c = s["gates"]
        label = f"A={'Y' if a else 'n'} B={'Y' if b else 'n'} C={'Y' if c else 'n'}"
        groups.setdefault(label, []).append(s)
    print(f"\n  Breakdown by which gates confirmed (+{horizon}d forward return):")
    for label, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        stat = _stats(group, horizon)
        if stat is None:
            print(f"    {label}: n={len(group)} (no forward-return data)")
            continue
        print(f"    {label}: n={stat['n']:>3}  avg={stat['avg']:+.1%}  win_rate={stat['win_rate']:.0%}")


def run_calendar_baseline(df: pd.DataFrame) -> list[dict]:
    """One entry per year on (or just after) Aug 1, forward returns at the
    same horizons -- the OLD pure-calendar trigger being replaced."""
    signals = []
    for yr in sorted(set(df.index.year)):
        window = df[(df.index >= pd.Timestamp(yr, 8, 1)) & (df.index <= pd.Timestamp(yr, 8, 10))]
        if window.empty:
            continue
        d = window.index[0]
        i = df.index.get_loc(d)
        entry_price = float(df["vxx"].iloc[i])
        fwd = {}
        for h in (10, 21, 45):
            j = i + h
            fwd[h] = (float(df["vxx"].iloc[j]) / entry_price - 1) if j < len(df) else None
        signals.append({"date": d.date().isoformat(), "entry_price": entry_price, "fwd_return": fwd})
    return signals


def _stats(signals: list[dict], horizon: int) -> dict | None:
    vals = [s["fwd_return"][horizon] for s in signals if s["fwd_return"].get(horizon) is not None]
    if not vals:
        return None
    return {
        "n": len(vals), "avg": sum(vals) / len(vals),
        "win_rate": sum(1 for v in vals if v > 0) / len(vals),
        "best": max(vals), "worst": min(vals),
    }


def _summarize(signals: list[dict], label: str) -> None:
    print(f"\n  {label}: {len(signals)} signal(s)")
    for h in (10, 21, 45):
        s = _stats(signals, h)
        if s is None:
            continue
        print(f"    +{h}d: n={s['n']}  avg fwd return={s['avg']:+.1%}  win rate={s['win_rate']:.0%}  "
              f"best={s['best']:+.1%}  worst={s['worst']:+.1%}")


def run_sweep(df: pd.DataFrame, require_gate_a: bool = False) -> None:
    # min_gates=3 confirmed to produce ZERO signals across the entire
    # 8.5y window under every combination in the first sweep (2026-08-25)
    # -- Gate A (cheap) and Gate C (rising momentum) are structurally in
    # tension, so all three essentially never align. Fixed at 2 here to
    # spend the compute budget on the new gate_b_min_pct dimension instead.
    # With require_gate_a=True, min_gates=2 means "A confirmed AND at
    # least one of B/C" -- the architecture requested 2026-08-25.
    cheap_percentiles = [15, 20, 25, 30, 35]
    momentum_min_pcts = [0.03, 0.05, 0.08, 0.10]
    lookback_options = [10, 15, 20]
    gate_b_min_pcts = [0.0, 0.05, 0.10, 0.15, 0.20]
    min_gates = 2

    rows = []
    done = 0
    for cheap_pct in cheap_percentiles:
        for min_pct in momentum_min_pcts:
            for lookback in lookback_options:
                for gate_b_pct in gate_b_min_pcts:
                    result = run_backtest(
                        df, lookback, min_pct, cheap_pct, min_gates,
                        gate_b_min_pct=gate_b_pct, require_gate_a=require_gate_a,
                    )
                    s21 = _stats(result["signals"], 21)
                    done += 1
                    if s21 is None or s21["n"] < 5:
                        continue  # too few signals to mean anything
                    rows.append({
                        "cheap_pct": cheap_pct, "min_pct": min_pct, "lookback": lookback, "gate_b_pct": gate_b_pct,
                        "n_signals": s21["n"], "avg_21d": s21["avg"], "win_rate_21d": s21["win_rate"],
                        "signals": result["signals"],
                    })
    mode = "A required + (B or C)" if require_gate_a else "equal-weighted 2-of-3"
    print(f"\nSwept {done} combinations, scoring={mode} ({len(rows)} had >=5 signals).")

    header = f"  {'cheap_pct':>9} {'min_pct':>8} {'lookback':>8} {'gate_b_pct':>10} {'n_sig':>6} {'avg_21d':>9} {'win_rate':>9}"

    rows.sort(key=lambda r: (r["win_rate_21d"], r["avg_21d"]), reverse=True)
    print("\n" + "=" * 100)
    print("  TOP 15 BY WIN RATE (21d forward return), min 5 signals")
    print("=" * 100)
    print(header)
    for r in rows[:15]:
        print(f"  {r['cheap_pct']:>9} {r['min_pct']:>8.0%} {r['lookback']:>8} {r['gate_b_pct']:>10.0%} "
              f"{r['n_signals']:>6} {r['avg_21d']:>+9.1%} {r['win_rate_21d']:>9.0%}")

    rows.sort(key=lambda r: r["avg_21d"], reverse=True)
    print("\n" + "=" * 100)
    print("  TOP 15 BY AVERAGE 21d FORWARD RETURN, min 5 signals")
    print("=" * 100)
    print(header)
    for r in rows[:15]:
        print(f"  {r['cheap_pct']:>9} {r['min_pct']:>8.0%} {r['lookback']:>8} {r['gate_b_pct']:>10.0%} "
              f"{r['n_signals']:>6} {r['avg_21d']:>+9.1%} {r['win_rate_21d']:>9.0%}")

    best_win_rate = max(rows, key=lambda r: (r["win_rate_21d"], r["avg_21d"]))
    print("\n  Gate breakdown for the best-win-rate combo:")
    breakdown_by_gate_pair(best_win_rate["signals"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true", help="Run the parameter grid sweep instead of a single backtest")
    parser.add_argument("--require-gate-a", action="store_true",
                         help="Score as 'A required AND (B or C)' instead of equal-weighted 2-of-3")
    parser.add_argument("--rotation", action="store_true",
                         help="Backtest the actual live VXX/UVXY rotation strategy instead of each ticker's signal in isolation")
    args = parser.parse_args()

    print("Fetching data (VXX, VIX, VIX3M)...")
    df = fetch_data()
    print(f"  {len(df)} trading days, {df.index.min().date()} -> {df.index.max().date()}")

    if args.sweep:
        run_sweep(df, require_gate_a=args.require_gate_a)
    elif args.rotation:
        rotation = run_rotation_backtest(
            df, config.VIX_LONGVOL_LOOKBACK_SESSIONS, config.VIX_LONGVOL_MOMENTUM_MIN_PCT,
            config.VIX_LONGVOL_CHEAP_PERCENTILE, config.VIX_LONGVOL_MIN_GATES,
            gate_b_min_pct=config.VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT, require_gate_a=True,
        )
        vxx_only = run_backtest(
            df, config.VIX_LONGVOL_LOOKBACK_SESSIONS, config.VIX_LONGVOL_MOMENTUM_MIN_PCT,
            config.VIX_LONGVOL_CHEAP_PERCENTILE, config.VIX_LONGVOL_MIN_GATES,
            gate_b_min_pct=config.VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT, require_gate_a=True, ticker="vxx",
        )
        uvxy_only = run_backtest(
            df, config.VIX_LONGVOL_LOOKBACK_SESSIONS, config.VIX_LONGVOL_MOMENTUM_MIN_PCT,
            config.VIX_LONGVOL_CHEAP_PERCENTILE, config.VIX_LONGVOL_MIN_GATES,
            gate_b_min_pct=config.VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT, require_gate_a=True, ticker="uvxy",
        )
        print("\n" + "=" * 70)
        print("  VXX/UVXY ROTATION — BACKTEST vs. EACH TICKER TRADED ALONE")
        print("=" * 70)
        print(f"  Tie band: {config.VIX_LONGVOL_MOMENTUM_TIE_PCT:.0%} (within this, defaults to VXX)")
        summarize_rotation(rotation)
        _summarize(vxx_only["signals"], "VXX ALONE (never rotates to UVXY)")
        _summarize(uvxy_only["signals"], "UVXY ALONE (never rotates to VXX)")
    else:
        result = run_backtest(
            df, config.VIX_LONGVOL_LOOKBACK_SESSIONS, config.VIX_LONGVOL_MOMENTUM_MIN_PCT,
            config.VIX_LONGVOL_CHEAP_PERCENTILE, config.VIX_LONGVOL_MIN_GATES,
            gate_b_min_pct=config.VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT, require_gate_a=args.require_gate_a,
        )
        baseline = run_calendar_baseline(df)

        print("\n" + "=" * 70)
        print("  LONG_VOL_TACTICAL GATES — SIGNAL-QUALITY BACKTEST")
        print("=" * 70)
        print(f"  Gate A cheap-vol percentile: {config.VIX_LONGVOL_CHEAP_PERCENTILE}")
        print(f"  Shared lookback: {config.VIX_LONGVOL_LOOKBACK_SESSIONS} sessions | "
              f"Gate C min momentum: {config.VIX_LONGVOL_MOMENTUM_MIN_PCT:.0%} | "
              f"Gate B min ratio decrease: {config.VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT:.0%} | "
              f"min gates to confirm: {config.VIX_LONGVOL_MIN_GATES}/3")
        _summarize(result["signals"], "NEW data-driven gates")
        _summarize(baseline, "OLD calendar-only baseline (Aug 1 each year)")
        breakdown_by_gate_pair(result["signals"])
