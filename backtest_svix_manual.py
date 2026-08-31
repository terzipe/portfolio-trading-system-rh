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

Usage:
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_manual.py [--nav 250000]
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_manual.py --sweep
"""
from __future__ import annotations

import argparse
import sys
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
                        tier3_confirm_days: int = 1) -> pd.DataFrame:
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
                  tier3_confirm_days: int | None = None, tier1_stop_pct: float | None = None) -> dict:
    stop_pct = stop_pct if stop_pct is not None else config.SVIX_MANUAL_STOP_PCT
    tier1_stop_pct = tier1_stop_pct if tier1_stop_pct is not None else config.SVIX_MANUAL_TIER1_STOP_PCT
    tier3_confirm_days = tier3_confirm_days if tier3_confirm_days is not None else config.VIX_LEADING_TIER3_CONFIRM_DAYS
    svix = fetch_yf_series("SVIX")
    if signal_df is None:
        signal_df = build_signal_frame(
            sessions=config.VIX_LEADING_DIVERGENCE_SESSIONS, divergence_min_pp=config.VIX_LEADING_DIVERGENCE_MIN_PP,
            compression_window=config.VIX_LEADING_COMPRESSION_WINDOW, compression_pct=config.VIX_LEADING_COMPRESSION_PERCENTILE,
            compression_history_years=config.VIX_LEADING_COMPRESSION_LOOKBACK_YEARS,
            term_sessions=config.VIX_LEADING_TERM_STRUCTURE_SESSIONS, term_min_pct=config.VIX_LEADING_TERM_STRUCTURE_MIN_PCT,
            skew_sessions=config.VIX_LEADING_SKEW_SESSIONS, skew_min_pct=config.VIX_LEADING_SKEW_MIN_PCT,
            tier3_confirm_days=tier3_confirm_days,
        )
    signal_on_svix = signal_df.reindex(svix.index, method="ffill")

    state_file = SYSTEM_DIR / "data" / "vix" / "_backtest_svix_manual_state.json"
    smc.SVIX_MANUAL_STATE_FILE = state_file
    smc._save_state(smc._default_state())

    trade_log: list[dict] = []
    daily_marks: list[dict] = []
    shadow_lots: list[dict] = []  # buy-and-hold baseline: same buys, never sold on signal

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
        if shares > 0:
            if exit_level >= 3:
                need_flatten = True
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

        if need_flatten and shares > 0:
            cost_before = smc._current_cost_basis(state)
            smc._consume_lots(shares)
            cost_after = smc._current_cost_basis(smc._load_state())
            proceeds = shares * svix_today
            realized = proceeds - (cost_before - cost_after)
            trade_log.append({"date": d.date().isoformat(), "action": "SELL", "exit_level": exit_level, "vix": row["vix"], "qty": shares, "price": svix_today, "proceeds": proceeds, "realized_pnl": realized})

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the SVIX manual buy-below-$20 campaign")
    parser.add_argument("--nav", type=float, default=250_000.0, help="Fixed NAV for budget-cap sizing (default: 250000)")
    parser.add_argument("--sweep", action="store_true", help="Sweep exit-tier thresholds instead of a single backtest")
    parser.add_argument(
        "--tier3-confirm-days", type=int, default=None, dest="tier3_confirm_days",
        help="Require tier 3 (term structure) confirmed on this many CONSECUTIVE sessions before flattening "
             "(default: config.VIX_LEADING_TIER3_CONFIRM_DAYS, currently 2 -- the live-wired value as of "
             "2026-08-29; pass 1 to reproduce the original un-gated behavior for comparison).",
    )
    args = parser.parse_args()

    if args.sweep:
        run_sweep(args.nav, tier3_confirm_days=args.tier3_confirm_days)
    else:
        result = run_backtest(args.nav, tier3_confirm_days=args.tier3_confirm_days)
        summarize(result)
