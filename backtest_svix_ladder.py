#!/usr/bin/env python3
"""
backtest_svix_ladder.py — historical backtest of the SVIX percentile ladder
(monitor/vix_ladder.py + monitor/vix_percentile.py) against real VIX (FRED
VIXCLS) and real SVIX (Yahoo Finance) daily closes.

Drives the ACTUAL production state machine day by day — vix_ladder.evaluate(),
record_rung_bought(), record_take_profit_step() — against an isolated tmp
state file, not a reimplementation of the strategy. The percentile threshold
table is recomputed weekly from a trailing-10y VIX window ending at each
backtest date only (no lookahead), matching loop_daily_vix.py's real weekly
refresh cadence. Rung/budget/pullback/take-profit constants are all read
from config.py, so this stays in sync with whatever's actually deployed.

Known simplifications (v1):
  - Daily-close triggering only. The real strategy fires "the instant a
    cycle observes" a level intraday; a close-only backtest understates how
    often a rung is actually touched (a day that spikes through and reverts
    intraday is invisible here) — understates activity, not fill quality,
    on days that DO close past a threshold.
  - Fills happen at that day's SVIX close, no slippage/spread modeled.
  - NAV is a fixed constant for the whole run (no compounding with realized
    P&L or the rest of a real portfolio) — isolates the ladder's own
    behavior from portfolio-level assumptions.
  - SVIX only trades from 2021-11-05 (Yahoo Finance data available from
    2022-03-30) — the backtest window can't include a 2008- or 2020-scale
    tail event. See the stress-test section for a synthetic overlay of that
    scenario applied to the backtest's actual peak exposure moment.

Usage:
  "Portfolio Trading System-RH/venv/bin/python" backtest_svix_ladder.py [--nav 250000]
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
from monitor import vix_ladder, vix_percentile  # noqa: E402

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch_vix_series(start: date, end: date) -> pd.Series:
    resp = requests.get(
        FRED_CSV_URL, params={"id": "VIXCLS", "cosd": start.isoformat(), "coed": end.isoformat()}, timeout=30,
    )
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), na_values=["."])
    date_col = "observation_date" if "observation_date" in df.columns else "DATE"
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.dropna(subset=["VIXCLS"])
    return df.set_index(date_col)["VIXCLS"].astype(float)


def fetch_svix_series() -> pd.Series:
    df = yf.download("SVIX", period="max", progress=False, auto_adjust=False)
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna()


def run_backtest(nav: float, lookback_years: float | None = None) -> dict:
    lookback_years = lookback_years if lookback_years is not None else config.VIX_PERCENTILE_LOOKBACK_YEARS
    svix = fetch_svix_series()
    lookback_buffer = timedelta(days=365 * lookback_years + 60)
    vix = fetch_vix_series(svix.index.min().date() - lookback_buffer, date.today())

    state_file = SYSTEM_DIR / "data" / "vix" / "_backtest_state.json"
    vix_ladder.VIX_SVIX_LADDER_STATE_FILE = state_file
    vix_ladder.reset_campaign()

    thresholds_cache = {"as_of": None, "table": None}

    def _thresholds_for(d: pd.Timestamp) -> dict[float, float]:
        if thresholds_cache["as_of"] is None or (d - thresholds_cache["as_of"]).days >= config.VIX_PERCENTILE_REFRESH_INTERVAL_DAYS:
            window_start = d - pd.Timedelta(days=365 * lookback_years)
            closes = vix[(vix.index > window_start) & (vix.index <= d)].tolist()
            thresholds_cache["table"] = vix_percentile.compute_thresholds(closes)
            thresholds_cache["as_of"] = d
        return thresholds_cache["table"]

    trade_log: list[dict] = []
    daily_marks: list[dict] = []

    for d in svix.index:
        if d in vix.index:
            vix_today = float(vix.loc[d])
        else:
            prior = vix[vix.index <= d]
            if prior.empty:
                continue
            vix_today = float(prior.iloc[-1])
        svix_today = float(svix.loc[d])

        table = _thresholds_for(d)
        vix_percentile.get_thresholds = lambda t=table: t
        vix_percentile.is_stale = lambda: False

        current_shares = vix_ladder.get_status()["current_shares"]
        positions = [{"ticker": "SVIX", "type": "share", "quantity": current_shares}] if current_shares > 0 else []

        actions = vix_ladder.evaluate(vix_today, nav, positions, svix_today, dry_run=False)
        for action in actions:
            if action.action == "BUY_SVIX_RUNG":
                target_dollars = action.position["target_dollars"]
                qty = int(target_dollars // svix_today)
                if qty <= 0:
                    continue
                vix_ladder.record_rung_bought(action.position["rung_percentile"], action.position["rung_vix_level"], qty, svix_today)
                trade_log.append({
                    "date": d.date().isoformat(), "action": "BUY", "percentile": action.position["rung_percentile"],
                    "vix": vix_today, "qty": qty, "price": svix_today, "dollars": qty * svix_today,
                })
            elif action.action == "SELL_SVIX_PARTIAL":
                qty = action.position["sell_quantity"]
                cost_before = vix_ladder._current_cost_basis(vix_ladder._load_state())
                vix_ladder.record_take_profit_step(action.position["step"], qty)
                cost_after = vix_ladder._current_cost_basis(vix_ladder._load_state())
                proceeds = qty * svix_today
                realized = proceeds - (cost_before - cost_after)
                trade_log.append({
                    "date": d.date().isoformat(), "action": "SELL", "step": action.position["step"],
                    "vix": vix_today, "qty": qty, "price": svix_today, "proceeds": proceeds, "realized_pnl": realized,
                })

        state = vix_ladder._load_state()
        shares_now = vix_ladder._current_shares(state)
        cost_basis_now = vix_ladder._current_cost_basis(state)
        avg_cost = vix_ladder._avg_cost_per_share(state)
        unrealized_pct = (svix_today - avg_cost) / avg_cost if avg_cost else None
        daily_marks.append({
            "date": d.date().isoformat(), "vix": vix_today, "svix": svix_today,
            "shares": shares_now, "cost_basis": cost_basis_now, "unrealized_pnl_pct": unrealized_pct,
        })

    vix_ladder.reset_campaign()
    return {"trade_log": trade_log, "daily_marks": daily_marks, "nav": nav, "svix": svix, "lookback_years": lookback_years}


def summarize(result: dict) -> None:
    trade_log, marks, nav = result["trade_log"], result["daily_marks"], result["nav"]
    buys = [t for t in trade_log if t["action"] == "BUY"]
    sells = [t for t in trade_log if t["action"] == "SELL"]
    total_realized = sum(t["realized_pnl"] for t in sells)
    total_deployed = sum(t["dollars"] for t in buys)

    peak_cost_basis = max((m["cost_basis"] for m in marks), default=0.0)
    peak_mark = max(marks, key=lambda m: m["cost_basis"], default=None)
    worst_unrealized = min((m["unrealized_pnl_pct"] for m in marks if m["unrealized_pnl_pct"] is not None), default=None)

    # Campaigns: contiguous runs of shares > 0
    campaigns = []
    open_campaign = None
    for m in marks:
        if m["shares"] > 0 and open_campaign is None:
            open_campaign = {"start": m["date"], "end": None}
        elif m["shares"] == 0 and open_campaign is not None:
            open_campaign["end"] = m["date"]
            campaigns.append(open_campaign)
            open_campaign = None
    still_open = open_campaign is not None

    # Per-campaign realized P&L, attributing each SELL to whichever
    # completed campaign's [start, end] date range it falls in -- gives a
    # "win rate" comparable in spirit to the LONG_VOL_TACTICAL gate
    # backtest's per-signal win rate, even though the ladder is a
    # continuous state machine, not discrete signals.
    for c in campaigns:
        c["realized_pnl"] = sum(
            t["realized_pnl"] for t in sells if c["start"] <= t["date"] <= c["end"]
        )
    campaign_win_rate = (
        sum(1 for c in campaigns if c["realized_pnl"] > 0) / len(campaigns) if campaigns else None
    )

    print("\n" + "=" * 70)
    print("  SVIX PERCENTILE LADDER — BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Window: {marks[0]['date']} -> {marks[-1]['date']} ({len(marks)} trading days)")
    print(f"  NAV (fixed): ${nav:,.2f} | budget cap: ${config.VIX_LADDER_BUDGET_PCT * nav:,.2f} "
          f"({config.VIX_LADDER_BUDGET_PCT:.0%} of NAV)")
    print(f"  Rungs: {config.VIX_PERCENTILE_RUNGS} | lookback: {result['lookback_years']}y | "
          f"refresh: every {config.VIX_PERCENTILE_REFRESH_INTERVAL_DAYS}d")
    print()
    print(f"  Campaigns completed (fully flattened): {len(campaigns)}")
    print(f"  Campaign still open at end of window:  {still_open}")
    print(f"  Campaign win rate (net-profitable completed campaigns): "
          f"{campaign_win_rate:.0%}" if campaign_win_rate is not None else "  Campaign win rate: n/a")
    print(f"  Total buys: {len(buys)} (${total_deployed:,.2f} deployed)  Total take-profit sells: {len(sells)}")
    print(f"  Total realized P&L: ${total_realized:,.2f}")
    print()
    print(f"  Peak cost-basis-at-risk: ${peak_cost_basis:,.2f} ({peak_cost_basis / nav:.1%} of NAV) "
          f"on {peak_mark['date'] if peak_mark else 'n/a'}")
    print(f"  Worst unrealized mark while a campaign was open: "
          f"{worst_unrealized:.1%}" if worst_unrealized is not None else "  Worst unrealized mark: n/a")
    print()
    for c in campaigns:
        print(f"    campaign {c['start']} -> {c['end']}")
    if still_open:
        print(f"    campaign {open_campaign['start'] if open_campaign else '?'} -> STILL OPEN at window end")

    # ── Stress test: synthetic 1-day -90% SVIX move at the actual peak
    # exposure moment (a Feb-2018-XIV-style acceleration event; the real
    # backtest window can't contain one since SVIX didn't exist in 2018).
    print("\n" + "-" * 70)
    print("  STRESS TEST: hypothetical -90% single-day SVIX move at peak exposure")
    print("-" * 70)
    if peak_mark:
        peak_market_value = peak_mark["shares"] * peak_mark["svix"]
        stress_loss = peak_market_value * 0.90
        print(f"  At peak exposure ({peak_mark['date']}): {peak_mark['shares']:.0f} shares "
              f"@ ${peak_mark['svix']:.2f} = ${peak_market_value:,.2f} market value "
              f"({peak_market_value / nav:.1%} of NAV)")
        print(f"  A -90% single-day move there -> -${stress_loss:,.2f} ({stress_loss / nav:.1%} of NAV) "
              f"in one session, with no P&L stop to intervene (by design).")

    pct_changes = result["svix"].pct_change()
    worst_real_daily_move = pct_changes.min()
    worst_real_move_date = pct_changes.idxmin()
    print(f"\n  For comparison, worst REAL single-day SVIX move in this backtest window: "
          f"{worst_real_daily_move:.1%} on {worst_real_move_date.date().isoformat()}")

    print("\n" + "-" * 70)
    print("  TRADE LOG")
    print("-" * 70)
    for t in trade_log:
        if t["action"] == "BUY":
            print(f"  {t['date']}  BUY   pct={t['percentile']:>5} vix={t['vix']:6.2f}  "
                  f"{t['qty']:>5.0f} sh @ ${t['price']:6.2f} = ${t['dollars']:>10,.2f}")
        else:
            print(f"  {t['date']}  SELL  step={t['step']}          "
                  f"{t['qty']:>5.0f} sh @ ${t['price']:6.2f} = ${t['proceeds']:>10,.2f}  "
                  f"realized=${t['realized_pnl']:>9,.2f}")

    return {
        "campaigns": campaigns, "still_open": still_open, "total_realized": total_realized,
        "total_deployed": total_deployed, "peak_cost_basis": peak_cost_basis, "peak_mark": peak_mark,
        "worst_unrealized": worst_unrealized, "worst_real_daily_move": worst_real_daily_move,
        "campaign_win_rate": campaign_win_rate,
    }


def _live_nav() -> float | None:
    """Real Alpaca paper account equity/NAV, if the session is reachable
    right now -- used to ground the structural stress test in the actual
    current account size rather than only illustrative round numbers. Was
    session.buying_power (Reg-T margin capacity, ~4x real equity) until
    2026-08-31 -- see loop_daily_vix.py's matching fix."""
    try:
        from monitor import vix_session
        session = vix_session.assess()
        return session.equity
    except Exception:  # noqa: BLE001
        return None


def stress_test_max_exposure(nav_levels: list[float] | None = None) -> None:
    """
    Structural stress test — NOT tied to backtest history (the actual
    historical window never loaded past 8% of NAV, so it can't speak to
    the strategy's true worst case). Computes the ladder's real maximum
    dollar exposure and applies a range of single-day shock magnitudes to
    it, at a few illustrative NAV levels.

    Key structural fact this surfaces: max exposure is
    min(VIX_LADDER_BUDGET_PCT * NAV, rungs * VIX_LADDER_RUNG_DOLLARS) --
    NOT always 15% of NAV. VIX_LADDER_RUNG_DOLLARS is a flat $5,000/rung,
    not a %-of-NAV, so for any account above the crossover NAV
    (rungs*$5,000 / 15%), the fixed rung total is what actually binds, and
    it SHRINKS as a fraction of NAV as the account grows. The live paper
    account (~$394k as of 2026-08-26) is well above that crossover.

    On the instrument itself: SVIX is a '40 Act ETF (holds VIX futures
    directly, daily-reset to a -1x index), not an ETN like XIV was. XIV's
    ~96% overnight loss in Feb 2018 came partly from its ETN structure's
    "Acceleration Event" clause, letting the issuer (Credit Suisse) force
    early redemption at a fraction of prior value -- a mechanism specific
    to ETNs (unsecured issuer debt), which SVIX's prospectus does not
    appear to carry (confirmed via SEC/Volatility Shares filings,
    2026-08-26). SVIX can't be "terminated" by an issuer the way XIV was.
    It remains fully exposed, as a -1x fund, to the same underlying
    short-vol-futures violence that caused XIV/SVXY's Feb 2018 collapse in
    the first place -- the structural analogy for "how bad can one day
    get" still holds even though the specific ETN-termination mechanism
    does not apply.
    """
    max_dollar_exposure = config.VIX_LADDER_RUNG_DOLLARS * len(config.VIX_PERCENTILE_RUNGS)
    crossover_nav = max_dollar_exposure / config.VIX_LADDER_BUDGET_PCT

    live_nav = _live_nav()
    if nav_levels is None:
        nav_levels = [100_000.0, crossover_nav, 250_000.0]
        if live_nav:
            nav_levels.append(live_nav)

    print("\n" + "=" * 78)
    print("  STRUCTURAL STRESS TEST — single-day short-vol shock at max exposure")
    print("=" * 78)
    print(f"  Max dollar exposure = min({config.VIX_LADDER_BUDGET_PCT:.0%} of NAV, "
          f"{len(config.VIX_PERCENTILE_RUNGS)} rungs x ${config.VIX_LADDER_RUNG_DOLLARS:,.0f}) "
          f"= min({config.VIX_LADDER_BUDGET_PCT:.0%} of NAV, ${max_dollar_exposure:,.0f})")
    print(f"  Crossover NAV (both caps equal): ${crossover_nav:,.0f} -- above this, the flat "
          f"${max_dollar_exposure:,.0f} rung total binds, NOT the 15%-of-NAV figure, since rung size "
          f"doesn't scale with account size.")

    shocks = [0.50, 0.70, 0.90, 0.96, 1.00]
    for nav in sorted(set(nav_levels)):
        budget_cap = config.VIX_LADDER_BUDGET_PCT * nav
        actual_exposure = min(budget_cap, max_dollar_exposure)
        binding = "5-rung total (fixed $)" if actual_exposure < budget_cap else "15%-of-NAV budget"
        tag = " <- current live paper account" if live_nav and abs(nav - live_nav) < 1 else ""
        print(f"\n  NAV=${nav:,.0f}{tag}  |  binding constraint: {binding}  |  "
              f"max exposure=${actual_exposure:,.0f} ({actual_exposure / nav:.1%} of NAV)")
        for shock in shocks:
            loss = actual_exposure * shock
            label = "XIV's actual Feb-2018 loss" if shock == 0.96 else ("total loss" if shock == 1.00 else "")
            print(f"    -{shock:.0%} single-day move {f'({label})' if label else '':<28} -> "
                  f"-${loss:,.2f}  ({loss / nav:.1%} of NAV)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the SVIX percentile ladder")
    parser.add_argument("--nav", type=float, default=250_000.0, help="Fixed NAV for budget-cap sizing (default: 250000)")
    args = parser.parse_args()

    result = run_backtest(args.nav)
    stats = summarize(result)
    stress_test_max_exposure()
