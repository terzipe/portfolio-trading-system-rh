#!/usr/bin/env python3
"""
backtest_svix_manual.py — historical backtest of the SVIX manual buy-below-
$20 campaign (monitor/svix_manual_campaign.py) + its leading-indicator exit
stack (monitor/vix_leading_signals.py), against real VIX/VIX3M (FRED),
VVIX/SKEW (yfinance), and real SVIX (yfinance) daily closes.

Reuses the REAL state-mutation primitives from svix_manual_campaign.py
(next_entry_rung(), _record_lot(), _consume_lots()) against an isolated tmp
state file — same pattern as backtest_svix_ladder.py — rather than
reimplementing the campaign's bookkeeping. The four leading-indicator tiers
are NOT computed by calling monitor.vix_leading_signals.evaluate() in the
per-day loop: that function's tier-3 (term_structure_gate) fetches FRED
data ending at "today" with no as_of parameter (correct for a live cycle,
wrong — and a live network call per simulated day — for a backtest). This
follows the exact precedent backtest_longvol_gates.py already established
for its own Gate B: tier 1/2/3/4 are precomputed here as vectorized pandas
Series over the full historical DataFrame using the SAME formulas as
vix_leading_signals.py's divergence_gate()/compression_gate()/
term_structure_gate(), then reindexed onto SVIX's own trading-day index.

Data window: VIX/VIX3M/VVIX/SKEW are fetched over 7 years for the leading-
indicator tiers to calibrate against (their own trailing-SD-percentile/
divergence math needs deep history to be meaningful). SVIX's own price
series is only real back to 2022-03-30 (confirmed live, not 7 years) — the
actual campaign P&L simulation is bounded to SVIX's real trading window;
the 7y signal history simply gives every one of those SVIX days a properly
calibrated tier reading rather than being truncated to match.

Known simplifications (v1), same spirit as backtest_svix_ladder.py:
  - Daily-close triggering only, fills at that day's close, no slippage.
  - NAV is a fixed constant for the whole run.
  - The exit tiers are recomputed here as pure pandas math, not by invoking
    monitor.vix_leading_signals.evaluate() itself — see module docstring.
    A --dry-run of loop_svix_exit_monitor.py against live data is still the
    way to sanity-check the LIVE evaluate() path this backtest doesn't
    exercise.

RIDE MODE (prototype, 2026-09-07 — NOT wired into live code yet):
  An optional second exit regime for the case where SVIX is trending up
  hard and the tight tier-1/tier-2 stop keeps chopping the position out
  before the post-spike recovery leg is captured. When --ride is passed:
    - Engage "ride mode" when, all on pure SVIX price action: N-day
      momentum >= RIDE_MOMENTUM_PCT, price within RIDE_NEAR_HIGH_BUF of the
      N-day high, and the position is green by >= RIDE_MIN_PNL_PCT.
    - While riding: tier-1 AND tier-2 stops are fully suspended. Protection
      is a wide trailing exit off the ride-peak (style tested: fixed_pct /
      atr / swing_low) plus tier 3.
    - Tier-3 sustained term-structure flatten stays an ABSOLUTE override.
    - If momentum fizzles below RIDE_EXIT_MOMENTUM_PCT without hitting an
      exit, snap back to the normal tight-stop regime (re-arms next cycle).
  Design answers locked 2026-09-07 (see project_svix_manual_strategy memory).

Usage:
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_manual.py [--nav 250000]
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_manual.py --sweep
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_manual.py --ride
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_manual.py --ride-sweep
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

SYSTEM_DIR = Path(__file__).parent
sys.path.insert(0, str(SYSTEM_DIR))

import config  # noqa: E402
from monitor import svix_manual_campaign as smc  # noqa: E402

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SIGNAL_LOOKBACK_YEARS = 7


def fetch_fred_series(series_id: str, start: date, end: date) -> pd.Series:
    resp = requests.get(FRED_CSV_URL, params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()}, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), na_values=["."])
    date_col = "observation_date" if "observation_date" in df.columns else "DATE"
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.dropna(subset=[series_id])
    return df.set_index(date_col)[series_id].astype(float)


def fetch_yf_series(ticker: str) -> pd.Series:
    df = yf.download(ticker, period="max", progress=False, auto_adjust=False)
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna()


def fetch_yf_ohlc(ticker: str) -> pd.DataFrame:
    """High/Low/Close DataFrame for a yfinance ticker, tz-naive index,
    NaN-dropped — used for SVIX ride-mode ATR and swing-low trailing
    stops (close-only fetch_yf_series() isn't enough for those)."""
    df = yf.download(ticker, period="max", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[["High", "Low", "Close"]].dropna()


def _wilder_atr(ohlc: pd.DataFrame, window: int) -> pd.Series:
    """Wilder's ATR (EWM with alpha=1/window) over a High/Low/Close frame."""
    h, l, c = ohlc["High"], ohlc["Low"], ohlc["Close"]
    prev_c = c.shift(1)
    true_range = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return true_range.ewm(alpha=1.0 / window, adjust=False).mean()


def _val(series: pd.Series, key) -> float | None:
    """series.get(key), returning None for missing / NaN (fail closed)."""
    try:
        v = series.get(key)
    except Exception:
        return None
    if v is None:
        return None
    v = float(v)
    return None if np.isnan(v) else v


def _default_ride_params() -> dict:
    """Ride-mode prototype defaults (2026-09-07). Every value is
    --ride-sweep-able; these are starting points, not conclusions."""
    return {
        "sessions": 10,              # momentum / near-high lookback (trading days)
        "momentum_pct": 0.08,        # ENTER ride: SVIX up >= this over `sessions`
        "near_high_buf": 0.015,      # ENTER ride: price >= `sessions`-day high * (1 - this)
        "min_pnl_pct": 0.05,         # ENTER ride: unrealized >= this vs blended cost
        "exit_momentum_pct": 0.02,   # SNAP BACK to tight stop: momentum falls below this
        "trail_style": "fixed_pct",  # fixed_pct | atr | swing_low
        "trail_pct": 0.07,           # fixed_pct: exit if price <= ride_peak * (1 - this)
        "atr_mult": 2.5,             # atr: exit if price <= ride_peak - this * ATR
        "atr_window": 14,
        "swing_window": 10,          # swing_low: exit if price <= min(low, `swing_window`d) * (1 - swing_buf)
        "swing_buf": 0.0,
        "rollover_mode": "trail_only",  # trail_only | ma_cross | momentum_flip (compare responsiveness)
        "ma_window": 10,
    }


def _ride_trail_level(ride: dict, ride_peak: float, d, atr_series: pd.Series | None,
                      swing_low_series: pd.Series | None) -> float | None:
    style = ride["trail_style"]
    if style == "atr":
        atr = _val(atr_series, d) if atr_series is not None else None
        if atr is not None:
            return ride_peak - ride["atr_mult"] * atr
        return ride_peak * (1 - ride["trail_pct"])  # fallback until ATR has history
    if style == "swing_low":
        low = _val(swing_low_series, d) if swing_low_series is not None else None
        return low * (1 - ride["swing_buf"]) if low is not None else None
    return ride_peak * (1 - ride["trail_pct"])  # fixed_pct (default)


def _rolling_sd_pct_rank(s: pd.Series, window: int, history_days: int) -> pd.Series:
    """For each day, the percentile rank of that day's trailing `window`-day
    SD against the trailing `history_days` of rolling `window`-day SDs —
    same math as vix_leading_signals._rolling_sd_percentile(), vectorized.
    """
    rolling_sd = s.rolling(window).std(ddof=0)

    def _rank(arr: np.ndarray) -> float:
        today = arr[-1]
        if np.isnan(today):
            return np.nan
        return (arr <= today).sum() / len(arr) * 100

    return rolling_sd.rolling(history_days, min_periods=window + 1).apply(_rank, raw=True)


def build_signal_frame(sessions: int, divergence_min_pp: float, compression_window: int,
                        compression_pct: float, compression_history_years: float,
                        term_sessions: int, term_min_pct: float,
                        skew_sessions: int, skew_min_pct: float,
                        tier3_confirm_days: int = 1, tier3_mode: str = "fall") -> pd.DataFrame:
    """tier3_mode selects what "term structure = exit" means:
      "fall"          — as-built: VIX/VIX3M ratio fell >= term_min_pct over
                        term_sessions (reuses vix_longvol_gates.term_
                        structure_gate()'s exact logic — but see the
                        2026-09-07 diagnostic: this fires on DEEPENING
                        contango / VIX collapsing, the phase the campaign
                        exists to harvest, and is premature ~60% of the time).
      "rise"          — sign-flipped: ratio ROSE >= term_min_pct (what
                        vix_leading_signals.py's docstring actually
                        describes — "ratio rising off a deep contango
                        reading").
      "backwardation" — only when the curve has actually inverted:
                        ratio >= 1.0. A last-resort catastrophe check, not
                        the primary exit.
      "off"           — tier 3 disabled entirely (tier 1/2 stops carry the
                        exit; use with --ride)."""
    """One combined, day-indexed DataFrame of tier1-4 booleans + exit_level
    over the full 7y VIX/VIX3M/VVIX/SKEW window — computed once per set of
    thresholds; cheap to re-derive for a sweep since the underlying rolling
    SD/pct-change series (the expensive part) are cached by the caller
    across sweep combinations that share window/lookback (see run_sweep())."""
    end = date.today()
    start = end - timedelta(days=365 * SIGNAL_LOOKBACK_YEARS + 60)
    vix = fetch_fred_series("VIXCLS", start, end)
    vix3m = fetch_fred_series("VXVCLS", start, end)
    vvix = fetch_yf_series("^VVIX")
    skew = fetch_yf_series("^SKEW")

    df = pd.DataFrame(index=vix.index)
    df["vix"] = vix
    df["vix3m"] = vix3m.reindex(df.index, method="ffill")
    df["vvix"] = vvix.reindex(df.index, method="ffill")
    df["skew"] = skew.reindex(df.index, method="ffill")
    df = df.dropna()

    vix_chg = df["vix"].pct_change(sessions)
    vvix_chg = df["vvix"].pct_change(sessions)
    df["tier1"] = (vvix_chg - vix_chg >= divergence_min_pp) & (vix_chg <= 0)

    history_days = int(compression_history_years * 252)
    vix_sd_pct = _rolling_sd_pct_rank(df["vix"], compression_window, history_days)
    vvix_sd_pct = _rolling_sd_pct_rank(df["vvix"], compression_window, history_days)
    df["tier2"] = (vix_sd_pct <= compression_pct) & (vvix_sd_pct <= compression_pct)

    ratio = df["vix"] / df["vix3m"]
    ratio_then = ratio.shift(term_sessions)
    if tier3_mode == "rise":
        tier3_raw = ratio >= ratio_then * (1 + term_min_pct)
    elif tier3_mode == "backwardation":
        tier3_raw = ratio >= 1.0
    elif tier3_mode == "off":
        tier3_raw = pd.Series(False, index=df.index)
    else:  # "fall" — as-built
        tier3_raw = ratio <= ratio_then * (1 - term_min_pct)
    if tier3_confirm_days <= 1:
        df["tier3"] = tier3_raw
    else:
        # Require tier3_raw True on EVERY one of the trailing
        # tier3_confirm_days sessions (not just today) before treating tier
        # 3 as confirmed -- kills same-day buy-then-flatten round-trips and
        # single-day whipsaw exits that a one-day-blip trigger produces,
        # at the cost of reacting one-plus day(s) slower to a genuine
        # regime shift. Investigated 2026-08-29 after the un-gated (1-day)
        # version was found to flatten 133/134 total sells and round-trip
        # 37% of all entries same-day -- see project memory.
        df["tier3"] = tier3_raw.astype(int).rolling(tier3_confirm_days, min_periods=tier3_confirm_days).sum() >= tier3_confirm_days

    skew_chg = df["skew"].pct_change(skew_sessions)
    df["tier4"] = skew_chg >= skew_min_pct

    df[["tier1", "tier2", "tier3", "tier4"]] = df[["tier1", "tier2", "tier3", "tier4"]].fillna(False)
    df["exit_level"] = np.where(df["tier3"], 3, np.where(df["tier2"], 2, np.where(df["tier1"], 1, 0)))
    return df


def run_backtest(nav: float, stop_pct: float | None = None, signal_df: pd.DataFrame | None = None,
                  tier3_confirm_days: int | None = None, tier1_stop_pct: float | None = None,
                  ride: dict | None = None, tier3_mode: str = "fall") -> dict:
    stop_pct = stop_pct if stop_pct is not None else config.SVIX_MANUAL_STOP_PCT
    tier1_stop_pct = tier1_stop_pct if tier1_stop_pct is not None else config.SVIX_MANUAL_TIER1_STOP_PCT
    tier3_confirm_days = tier3_confirm_days if tier3_confirm_days is not None else config.VIX_LEADING_TIER3_CONFIRM_DAYS
    svix = fetch_yf_series("SVIX")

    # Ride-mode helper series, all indexed on SVIX's own trading days.
    # Only built when ride mode is active so a plain run stays untouched.
    svix_mom = svix_roll_max = svix_sma = atr_series = swing_low_series = None
    if ride is not None:
        svix_mom = svix.pct_change(ride["sessions"])
        svix_roll_max = svix.rolling(ride["sessions"]).max()
        svix_sma = svix.rolling(ride["ma_window"]).mean()
        try:
            ohlc = fetch_yf_ohlc("SVIX").reindex(svix.index).ffill()
            atr_series = _wilder_atr(ohlc, ride["atr_window"])
            swing_low_series = ohlc["Low"].rolling(ride["swing_window"]).min()
        except Exception as exc:  # noqa: BLE001
            print(f"  [ride] SVIX OHLC fetch failed ({exc}); atr/swing_low styles will fall back to fixed_pct")

    if signal_df is None:
        signal_df = build_signal_frame(
            sessions=config.VIX_LEADING_DIVERGENCE_SESSIONS, divergence_min_pp=config.VIX_LEADING_DIVERGENCE_MIN_PP,
            compression_window=config.VIX_LEADING_COMPRESSION_WINDOW, compression_pct=config.VIX_LEADING_COMPRESSION_PERCENTILE,
            compression_history_years=config.VIX_LEADING_COMPRESSION_LOOKBACK_YEARS,
            term_sessions=config.VIX_LEADING_TERM_STRUCTURE_SESSIONS, term_min_pct=config.VIX_LEADING_TERM_STRUCTURE_MIN_PCT,
            skew_sessions=config.VIX_LEADING_SKEW_SESSIONS, skew_min_pct=config.VIX_LEADING_SKEW_MIN_PCT,
            tier3_confirm_days=tier3_confirm_days, tier3_mode=tier3_mode,
        )
    signal_on_svix = signal_df.reindex(svix.index, method="ffill")

    state_file = SYSTEM_DIR / "data" / "vix" / "_backtest_svix_manual_state.json"
    smc.SVIX_MANUAL_STATE_FILE = state_file
    smc._save_state(smc._default_state())

    trade_log: list[dict] = []
    daily_marks: list[dict] = []
    shadow_lots: list[dict] = []  # buy-and-hold baseline: same buys, never sold on signal
    ride_mode = False
    ride_peak = 0.0

    for d in svix.index:
        svix_today = float(svix.loc[d])
        if d not in signal_on_svix.index or pd.isna(signal_on_svix.loc[d, "vix"]):
            continue
        row = signal_on_svix.loc[d]
        exit_level = int(row["exit_level"])

        state = smc._load_state()
        rung = smc.next_entry_rung(state, svix_today)
        if rung is not None:
            budget = smc.remaining_budget(state)
            dollars = min(config.SVIX_MANUAL_RUNG_DOLLARS, budget)
            qty = int(dollars // svix_today)
            if qty > 0:
                state["rungs_fired"].append(rung)
                smc._save_state(state)
                smc._record_lot(rung, qty, svix_today)
                shadow_lots.append({"qty_remaining": qty, "price": svix_today})
                trade_log.append({"date": d.date().isoformat(), "action": "BUY", "rung": rung, "vix": row["vix"], "qty": qty, "price": svix_today, "dollars": qty * svix_today})

        state = smc._load_state()
        shares = smc._current_shares(state)
        need_flatten = False
        exit_reason = None
        if shares > 0:
            avg_cost_now = smc._avg_cost_per_share(state)
            unreal_pct = (svix_today - avg_cost_now) / avg_cost_now if avg_cost_now else 0.0

            if exit_level >= 3:
                need_flatten = True                       # tier 3 = absolute override, both regimes
                exit_reason = "tier3"
            elif ride is not None and ride_mode:
                # RIDE MODE: tier-1 AND tier-2 stops fully suspended. Wide
                # trailing exit off the ride-peak + cost-basis disaster
                # floor + (tier 3 handled above).
                ride_peak = max(ride_peak, svix_today)
                mom = _val(svix_mom, d)
                if ride["rollover_mode"] == "ma_cross":
                    sma = _val(svix_sma, d)
                    rolled = sma is not None and svix_today < sma
                elif ride["rollover_mode"] == "momentum_flip":
                    rolled = mom is not None and mom < 0
                else:  # trail_only — "X% off ride-peak" (primary per design answer #5)
                    trail = _ride_trail_level(ride, ride_peak, d, atr_series, swing_low_series)
                    rolled = trail is not None and svix_today <= trail
                if rolled:
                    need_flatten = True
                    exit_reason = "ride_rollover"
                elif mom is not None and mom < ride["exit_momentum_pct"]:
                    ride_mode = False                     # snap back to tight-stop regime (re-arms next cycle)
                    ride_peak = 0.0
                    state["armed_stop"] = None
                    smc._save_state(state)
            else:
                # Mirrors svix_manual_campaign.run_exit_cycle()'s ratchet:
                # level 1 arms a wide stop, level 2 tightens it, and the
                # stop only ever moves closer to price, never loosens.
                level_pct = {1: tier1_stop_pct, 2: stop_pct}.get(exit_level)
                if level_pct is not None:
                    candidate = round(svix_today * (1 - level_pct), 4)
                    if state["armed_stop"] is None or candidate > state["armed_stop"]:
                        state["armed_stop"] = candidate
                        smc._save_state(state)
                state = smc._load_state()
                if state["armed_stop"] is not None and svix_today <= state["armed_stop"]:
                    need_flatten = True
                    exit_reason = "tight_stop"

                # RIDE-MODE ENTRY — pure SVIX price action (design answer #2):
                # momentum + near-highs + green position. Can engage even
                # while tier-2 compression is confirmed.
                if ride is not None and not need_flatten:
                    mom = _val(svix_mom, d)
                    roll_max = _val(svix_roll_max, d)
                    if (mom is not None and mom >= ride["momentum_pct"]
                            and roll_max is not None and svix_today >= roll_max * (1 - ride["near_high_buf"])
                            and unreal_pct >= ride["min_pnl_pct"]):
                        ride_mode = True
                        ride_peak = svix_today
                        state["armed_stop"] = None
                        smc._save_state(state)

        if need_flatten and shares > 0:
            cost_before = smc._current_cost_basis(state)
            smc._consume_lots(shares)
            cost_after = smc._current_cost_basis(smc._load_state())
            proceeds = shares * svix_today
            realized = proceeds - (cost_before - cost_after)
            trade_log.append({"date": d.date().isoformat(), "action": "SELL", "exit_level": exit_level,
                              "reason": exit_reason, "vix": row["vix"], "qty": shares, "price": svix_today,
                              "proceeds": proceeds, "realized_pnl": realized})
            ride_mode = False
            ride_peak = 0.0

        state = smc._load_state()
        shares_now = smc._current_shares(state)
        cost_basis_now = smc._current_cost_basis(state)
        avg_cost = smc._avg_cost_per_share(state)
        unrealized_pct = (svix_today - avg_cost) / avg_cost if avg_cost else None

        shadow_mv = sum(l["qty_remaining"] * svix_today for l in shadow_lots)
        shadow_cost = sum(l["qty_remaining"] * l["price"] for l in shadow_lots)
        shadow_unrealized_pct = (shadow_mv - shadow_cost) / shadow_cost if shadow_cost else None

        daily_marks.append({
            "date": d.date().isoformat(), "vix": float(row["vix"]), "svix": svix_today,
            "shares": shares_now, "cost_basis": cost_basis_now, "unrealized_pnl_pct": unrealized_pct,
            "shadow_mv": shadow_mv, "shadow_cost": shadow_cost, "shadow_unrealized_pct": shadow_unrealized_pct,
            "ride_mode": ride_mode and shares_now > 0,
        })

    smc._save_state(smc._default_state())
    return {"trade_log": trade_log, "daily_marks": daily_marks, "nav": nav, "svix": svix, "stop_pct": stop_pct}


def summarize(result: dict) -> dict:
    trade_log, marks, nav = result["trade_log"], result["daily_marks"], result["nav"]
    buys = [t for t in trade_log if t["action"] == "BUY"]
    sells = [t for t in trade_log if t["action"] == "SELL"]
    total_realized = sum(t["realized_pnl"] for t in sells)
    total_deployed = sum(t["dollars"] for t in buys)

    days_in_market = sum(1 for m in marks if m["shares"] > 0)
    time_in_market_pct = days_in_market / len(marks) if marks else 0.0

    campaigns = []
    open_campaign = None
    for m in marks:
        if m["shares"] > 0 and open_campaign is None:
            open_campaign = {"start": m["date"], "end": None, "days": 0}
        if m["shares"] > 0:
            open_campaign["days"] += 1
        elif m["shares"] == 0 and open_campaign is not None:
            open_campaign["end"] = m["date"]
            campaigns.append(open_campaign)
            open_campaign = None
    still_open = open_campaign is not None
    for c in campaigns:
        c["realized_pnl"] = sum(t["realized_pnl"] for t in sells if c["start"] <= t["date"] <= c["end"])
    win_rate = sum(1 for c in campaigns if c["realized_pnl"] > 0) / len(campaigns) if campaigns else None
    avg_duration = sum(c["days"] for c in campaigns) / len(campaigns) if campaigns else None

    # Same-day round-trips: a rung bought and immediately flattened the same
    # session (entry fired while an exit condition was already confirmed) --
    # these never show up in `campaigns` above (shares never persist to a
    # daily mark) but still cost a real order pair. Investigated 2026-08-29
    # as the dominant explanation for lower-than-expected time-in-market.
    buy_dates = {t["date"] for t in buys}
    sell_dates = {t["date"] for t in sells}
    same_day_roundtrips = len(buy_dates & sell_dates)

    # Drawdown-avoidance: the actual signal-exited run's worst unrealized
    # mark vs. the shadow (never-exits-on-signal) buy-and-hold baseline's
    # worst unrealized mark, on the SAME buy events.
    worst_signal = min((m["unrealized_pnl_pct"] for m in marks if m["unrealized_pnl_pct"] is not None), default=None)
    worst_shadow = min((m["shadow_unrealized_pct"] for m in marks if m["shadow_unrealized_pct"] is not None), default=None)

    print("\n" + "=" * 70)
    print("  SVIX MANUAL CAMPAIGN — BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Window: {marks[0]['date']} -> {marks[-1]['date']} ({len(marks)} trading days)")
    print(f"  NAV (fixed): ${nav:,.2f} | budget cap: ${config.SVIX_MANUAL_BUDGET_DOLLARS:,.2f} | stop_pct: {result['stop_pct']:.1%}")
    print(f"  Rungs: {config.SVIX_MANUAL_RUNGS}")
    print(f"  Time in market: {time_in_market_pct:.0%} ({days_in_market}/{len(marks)} days)")
    print()
    print(f"  Campaigns completed: {len(campaigns)}  |  still open at window end: {still_open}")
    print(f"  Campaign win rate: {win_rate:.0%}" if win_rate is not None else "  Campaign win rate: n/a")
    print(f"  Avg campaign duration: {avg_duration:.1f} days" if avg_duration is not None else "  Avg campaign duration: n/a")
    print(f"  Total buys: {len(buys)} (${total_deployed:,.2f} deployed)  |  Total sells: {len(sells)}")
    print(f"  Same-day round-trips (bought and flattened same session): {same_day_roundtrips} "
          f"({same_day_roundtrips / len(buys):.0%} of all buys)" if buys else "  Same-day round-trips: n/a")
    print(f"  Total realized P&L: ${total_realized:,.2f}")
    print()
    print(f"  Worst unrealized mark, SIGNAL-EXITED run: {worst_signal:.1%}" if worst_signal is not None else "  Worst unrealized mark: n/a")
    print(f"  Worst unrealized mark, SHADOW buy-and-hold (same buys, never exits): {worst_shadow:.1%}" if worst_shadow is not None else "  Worst shadow mark: n/a")
    if worst_signal is not None and worst_shadow is not None:
        # Both are signed (more negative = worse). signal is better when its
        # mark is LESS negative than shadow's, i.e. worst_signal - worst_shadow > 0.
        print(f"  Drawdown avoided by the exit signal: {worst_signal - worst_shadow:+.1%} (positive = signal helped)")

    if any(t.get("reason") for t in sells):
        by_reason = Counter(t.get("reason") or "?" for t in sells)
        pnl_by_reason: dict[str, float] = {}
        for t in sells:
            k = t.get("reason") or "?"
            pnl_by_reason[k] = pnl_by_reason.get(k, 0.0) + t["realized_pnl"]
        ride_days = sum(1 for m in marks if m.get("ride_mode"))
        print()
        print(f"  Ride-mode days: {ride_days} ({ride_days / len(marks):.0%} of window, "
              f"{ride_days / days_in_market:.0%} of time-in-market)" if days_in_market else "  Ride-mode days: 0")
        print("  Exits by reason:")
        for r, n in by_reason.most_common():
            print(f"    {r:16s} {n:3d}   realized ${pnl_by_reason[r]:,.0f}")

    return {"campaigns": campaigns, "win_rate": win_rate, "avg_duration": avg_duration,
            "total_realized": total_realized, "total_deployed": total_deployed,
            "time_in_market_pct": time_in_market_pct, "same_day_roundtrips": same_day_roundtrips,
            "worst_signal": worst_signal, "worst_shadow": worst_shadow}


def run_sweep(nav: float, tier3_confirm_days: int | None = None) -> None:
    """Nested-grid sweep, same shape as backtest_longvol_gates.py's
    run_sweep() — one signal_frame build per (compression_pct,
    divergence_min_pp, term_min_pct) combo (the rolling-SD/pct-change math
    itself doesn't change across a stop_pct sweep, so stop_pct is the inner
    loop, reusing the same signal_df). `tier3_confirm_days` defaults to
    config.VIX_LEADING_TIER3_CONFIRM_DAYS (2, live-wired 2026-08-29) so a
    fresh sweep recalibrates comp/div/term/stop under the SAME tier-3
    confirmation regime that's actually live, not the stale 1-day
    assumption the original 2026-08-29 sweep ran under."""
    tier3_confirm_days = tier3_confirm_days if tier3_confirm_days is not None else config.VIX_LEADING_TIER3_CONFIRM_DAYS
    compression_pcts = [5, 10, 15, 20]
    divergence_min_pps = [0.05, 0.10, 0.15]
    term_min_pcts = [0.02, 0.03, 0.05]
    stop_pcts = [0.03, 0.05, 0.07, 0.10]

    rows = []
    done = 0
    total = len(compression_pcts) * len(divergence_min_pps) * len(term_min_pcts) * len(stop_pcts)
    for comp_pct in compression_pcts:
        for div_pp in divergence_min_pps:
            for term_pct in term_min_pcts:
                signal_df = build_signal_frame(
                    sessions=config.VIX_LEADING_DIVERGENCE_SESSIONS, divergence_min_pp=div_pp,
                    compression_window=config.VIX_LEADING_COMPRESSION_WINDOW, compression_pct=comp_pct,
                    compression_history_years=config.VIX_LEADING_COMPRESSION_LOOKBACK_YEARS,
                    term_sessions=config.VIX_LEADING_TERM_STRUCTURE_SESSIONS, term_min_pct=term_pct,
                    skew_sessions=config.VIX_LEADING_SKEW_SESSIONS, skew_min_pct=config.VIX_LEADING_SKEW_MIN_PCT,
                    tier3_confirm_days=tier3_confirm_days,
                )
                for stop_pct in stop_pcts:
                    result = run_backtest(nav, stop_pct=stop_pct, signal_df=signal_df)
                    marks = result["daily_marks"]
                    if len(marks) < 30:
                        done += 1
                        continue
                    days_in_market = sum(1 for m in marks if m["shares"] > 0)
                    total_realized = sum(t["realized_pnl"] for t in result["trade_log"] if t["action"] == "SELL")
                    worst_signal = min((m["unrealized_pnl_pct"] for m in marks if m["unrealized_pnl_pct"] is not None), default=None)
                    worst_shadow = min((m["shadow_unrealized_pct"] for m in marks if m["shadow_unrealized_pct"] is not None), default=None)
                    avoided = (worst_signal - worst_shadow) if (worst_signal is not None and worst_shadow is not None) else None
                    rows.append({
                        "comp_pct": comp_pct, "div_pp": div_pp, "term_pct": term_pct, "stop_pct": stop_pct,
                        "time_in_market": days_in_market / len(marks), "total_realized": total_realized,
                        "drawdown_avoided": avoided,
                    })
                    done += 1
                    print(f"  [{done}/{total}] comp={comp_pct} div={div_pp:.0%} term={term_pct:.0%} stop={stop_pct:.0%} "
                          f"-> realized=${total_realized:,.0f} avoided={avoided:+.1%}" if avoided is not None else
                          f"  [{done}/{total}] comp={comp_pct} div={div_pp:.0%} term={term_pct:.0%} stop={stop_pct:.0%} -> realized=${total_realized:,.0f}")

    valid = [r for r in rows if r["drawdown_avoided"] is not None]
    print("\n" + "=" * 78)
    print("  TOP 10 BY DRAWDOWN AVOIDED")
    print("=" * 78)
    for r in sorted(valid, key=lambda r: r["drawdown_avoided"], reverse=True)[:10]:
        print(f"  comp={r['comp_pct']} div={r['div_pp']:.0%} term={r['term_pct']:.0%} stop={r['stop_pct']:.0%}  "
              f"avoided={r['drawdown_avoided']:+.1%}  realized=${r['total_realized']:,.0f}  time_in_mkt={r['time_in_market']:.0%}")

    print("\n" + "=" * 78)
    print("  TOP 10 BY TOTAL REALIZED P&L")
    print("=" * 78)
    for r in sorted(rows, key=lambda r: r["total_realized"], reverse=True)[:10]:
        avoided_str = f"{r['drawdown_avoided']:+.1%}" if r["drawdown_avoided"] is not None else "n/a"
        print(f"  comp={r['comp_pct']} div={r['div_pp']:.0%} term={r['term_pct']:.0%} stop={r['stop_pct']:.0%}  "
              f"realized=${r['total_realized']:,.0f}  avoided={avoided_str}  time_in_mkt={r['time_in_market']:.0%}")


def run_ride_sweep(nav: float) -> None:
    """Grid over the ride-mode knobs, signal_df held at the live config
    (ride mode changes only the EXIT path once a position is open, not the
    tier thresholds). Baseline (ride off) is printed first for comparison.
    Runtime ~ a few minutes — one run_backtest per combo, signal_df reused."""
    signal_df = build_signal_frame(
        sessions=config.VIX_LEADING_DIVERGENCE_SESSIONS, divergence_min_pp=config.VIX_LEADING_DIVERGENCE_MIN_PP,
        compression_window=config.VIX_LEADING_COMPRESSION_WINDOW, compression_pct=config.VIX_LEADING_COMPRESSION_PERCENTILE,
        compression_history_years=config.VIX_LEADING_COMPRESSION_LOOKBACK_YEARS,
        term_sessions=config.VIX_LEADING_TERM_STRUCTURE_SESSIONS, term_min_pct=config.VIX_LEADING_TERM_STRUCTURE_MIN_PCT,
        skew_sessions=config.VIX_LEADING_SKEW_SESSIONS, skew_min_pct=config.VIX_LEADING_SKEW_MIN_PCT,
        tier3_confirm_days=config.VIX_LEADING_TIER3_CONFIRM_DAYS,
    )

    def _metrics(result: dict) -> dict:
        marks = result["daily_marks"]
        sells = [t for t in result["trade_log"] if t["action"] == "SELL"]
        dim = sum(1 for m in marks if m["shares"] > 0)
        ws = min((m["unrealized_pnl_pct"] for m in marks if m["unrealized_pnl_pct"] is not None), default=None)
        wsh = min((m["shadow_unrealized_pct"] for m in marks if m["shadow_unrealized_pct"] is not None), default=None)
        return {
            "realized": sum(t["realized_pnl"] for t in sells),
            "time_in_market": dim / len(marks) if marks else 0.0,
            "ride_days": sum(1 for m in marks if m.get("ride_mode")),
            "avoided": (ws - wsh) if (ws is not None and wsh is not None) else None,
            "worst": ws,
        }

    base = _metrics(run_backtest(nav, signal_df=signal_df))
    print("\n" + "=" * 90)
    print(f"  BASELINE (ride OFF): realized=${base['realized']:,.0f}  time_in_mkt={base['time_in_market']:.0%}  "
          f"worst_mark={base['worst']:.1%}  drawdown_avoided={base['avoided']:+.1%}")
    print("=" * 90)

    styles = ["fixed_pct", "atr", "swing_low"]
    rollover_modes = ["trail_only", "ma_cross", "momentum_flip"]
    momentum_pcts = [0.06, 0.08, 0.10]
    trail_pcts = [0.06, 0.07, 0.08]
    atr_mults = [2.0, 2.5, 3.0]
    min_pnl_pcts = [0.03, 0.05, 0.08]

    rows: list[dict] = []
    combos: list[dict] = []
    for style in styles:
        for roll in rollover_modes:
            for mom in momentum_pcts:
                for mpnl in min_pnl_pcts:
                    if style == "atr":
                        widths = [("atr_mult", a) for a in atr_mults]
                    elif style == "fixed_pct":
                        widths = [("trail_pct", t) for t in trail_pcts]
                    else:  # swing_low has no width knob here
                        widths = [("swing_window", 10)]
                    for wkey, wval in widths:
                        p = _default_ride_params()
                        p["trail_style"] = style
                        p["rollover_mode"] = roll
                        p["momentum_pct"] = mom
                        p["min_pnl_pct"] = mpnl
                        p[wkey] = wval
                        combos.append(p)

    for i, p in enumerate(combos, 1):
        m = _metrics(run_backtest(nav, signal_df=signal_df, ride=p))
        width = p["atr_mult"] if p["trail_style"] == "atr" else (p["trail_pct"] if p["trail_style"] == "fixed_pct" else p["swing_window"])
        rows.append({**m, "style": p["trail_style"], "roll": p["rollover_mode"],
                     "mom": p["momentum_pct"], "mpnl": p["min_pnl_pct"], "width": width})
        print(f"  [{i}/{len(combos)}] style={p['trail_style']:9s} roll={p['rollover_mode']:13s} "
              f"mom={p['momentum_pct']:.0%} minpnl={p['min_pnl_pct']:.0%} w={width} -> "
              f"realized=${m['realized']:,.0f} tim={m['time_in_market']:.0%} ride_d={m['ride_days']} "
              f"avoided={m['avoided']:+.1%}" if m["avoided"] is not None else "")

    def _dump(title: str, key, reverse: bool):
        print("\n" + "=" * 90)
        print(f"  {title}")
        print("=" * 90)
        for r in sorted([x for x in rows if x[key] is not None], key=lambda x: x[key], reverse=reverse)[:12]:
            print(f"  style={r['style']:9s} roll={r['roll']:13s} mom={r['mom']:.0%} minpnl={r['mpnl']:.0%} w={r['width']}  "
                  f"realized=${r['realized']:,.0f}  avoided={r['avoided']:+.1%}  tim={r['time_in_market']:.0%}  "
                  f"worst={r['worst']:.1%}  ride_days={r['ride_days']}")

    _dump("TOP 12 BY REALIZED P&L", "realized", True)
    _dump("TOP 12 BY DRAWDOWN AVOIDED", "avoided", True)
    _dump("WORST 12 BY DRAWDOWN AVOIDED (ride mode HURT protection here)", "avoided", False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the SVIX manual buy-below-$20 campaign")
    parser.add_argument("--nav", type=float, default=250_000.0, help="Fixed NAV for budget-cap sizing (default: 250000)")
    parser.add_argument("--sweep", action="store_true", help="Sweep exit-tier thresholds instead of a single backtest")
    parser.add_argument("--ride", action="store_true", help="Single run with ride mode ON at _default_ride_params()")
    parser.add_argument("--ride-sweep", action="store_true", dest="ride_sweep",
                        help="Grid over the ride-mode knobs (trail style, rollover mode, momentum, min-P&L, width)")
    parser.add_argument("--tier3-mode", default="fall", dest="tier3_mode",
                        choices=["fall", "rise", "backwardation", "off"],
                        help="What 'term structure = exit' means (see build_signal_frame). Default 'fall' = as-built.")
    parser.add_argument("--tier3-compare", action="store_true", dest="tier3_compare",
                        help="Run all four tier3 modes (x ride off/on) side by side and stop.")
    parser.add_argument(
        "--tier3-confirm-days", type=int, default=None, dest="tier3_confirm_days",
        help="Require tier 3 (term structure) confirmed on this many CONSECUTIVE sessions before flattening "
             "(default: config.VIX_LEADING_TIER3_CONFIRM_DAYS, currently 2 -- the live-wired value as of "
             "2026-08-29; pass 1 to reproduce the original un-gated behavior for comparison).",
    )
    args = parser.parse_args()

    if args.tier3_compare:
        print(f"\n{'tier3_mode':14s} {'ride':5s} {'realized':>11s} {'time_in_mkt':>12s} {'worst_mark':>11s} {'avoided':>9s}")
        for mode in ["fall", "rise", "backwardation", "off"]:
            for ride in (None, _default_ride_params()):
                r = run_backtest(args.nav, tier3_mode=mode, ride=ride)
                marks = r["daily_marks"]
                sells = [t for t in r["trade_log"] if t["action"] == "SELL"]
                dim = sum(1 for m in marks if m["shares"] > 0) / len(marks)
                ws = min((m["unrealized_pnl_pct"] for m in marks if m["unrealized_pnl_pct"] is not None), default=0)
                wsh = min((m["shadow_unrealized_pct"] for m in marks if m["shadow_unrealized_pct"] is not None), default=0)
                print(f"{mode:14s} {'on' if ride else 'off':5s} "
                      f"${sum(t['realized_pnl'] for t in sells):>10,.0f} {dim:>11.0%} {ws:>10.1%} {ws - wsh:>+8.1%}")
    elif args.ride_sweep:
        run_ride_sweep(args.nav)
    elif args.sweep:
        run_sweep(args.nav, tier3_confirm_days=args.tier3_confirm_days)
    else:
        ride = _default_ride_params() if args.ride else None
        if ride is not None:
            print("\n--- BASELINE (ride OFF) ---")
            summarize(run_backtest(args.nav, tier3_confirm_days=args.tier3_confirm_days, tier3_mode=args.tier3_mode))
            print("\n--- RIDE MODE ON (_default_ride_params) ---")
        result = run_backtest(args.nav, tier3_confirm_days=args.tier3_confirm_days, ride=ride, tier3_mode=args.tier3_mode)
        summarize(result)
