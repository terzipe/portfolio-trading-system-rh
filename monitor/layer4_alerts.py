"""
Layer 4 — Alerts & Dashboard.
Compares today's snapshot to yesterday's to detect new strikes/expiries.
Flags target hits, stop hits, big IV moves.
Renders a terminal dashboard and fires iMessage alerts.
"""
import json
import datetime
import pathlib
from config import SNAPSHOTS_DIR, MAX_DAILY_LOSS_PCT, ACCOUNT_BUDGET
from alerts.imessage import send_imessage

IV_SPIKE_THRESHOLD = 0.10  # 10 percentage point IV move triggers alert
TARGET_PCT = 50.0           # profit target per position (%)
STOP_PCT = -30.0            # stop loss per position (%)


def _load_yesterday() -> list[dict] | None:
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    path = pathlib.Path(SNAPSHOTS_DIR) / f"{yesterday}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _detect_new_strikes(today_positions: list[dict], yesterday_positions: list[dict] | None) -> list[str]:
    if not yesterday_positions:
        return []
    alerts = []
    yday_keys = {(p["ticker"], p.get("expiry"), p.get("strike")) for p in yesterday_positions}
    for p in today_positions:
        key = (p["ticker"], p.get("expiry"), p.get("strike"))
        if key not in yday_keys and p["type"] == "option":
            alerts.append(f"NEW STRIKE: {p['ticker']} {p.get('expiry')} ${p.get('strike')}")
    return alerts


def _check_targets_stops(today_positions: list[dict]) -> list[str]:
    alerts = []
    for p in today_positions:
        pnl_pct = p.get("pnl_pct", 0)
        ticker = p["ticker"]
        if pnl_pct >= TARGET_PCT:
            alerts.append(f"TARGET HIT: {ticker} +{pnl_pct:.1f}% — consider taking profits")
        elif pnl_pct <= STOP_PCT:
            alerts.append(f"STOP HIT: {ticker} {pnl_pct:.1f}% — review position")
    return alerts


def _check_iv_spikes(today_positions: list[dict], yesterday_positions: list[dict] | None) -> list[str]:
    if not yesterday_positions:
        return []
    yday_iv = {p["ticker"]: p.get("iv", 0) for p in yesterday_positions if p["type"] == "option"}
    alerts = []
    for p in today_positions:
        if p["type"] != "option":
            continue
        prev_iv = yday_iv.get(p["ticker"], p.get("iv", 0))
        delta_iv = p.get("iv", 0) - prev_iv
        if abs(delta_iv) >= IV_SPIKE_THRESHOLD:
            direction = "spike" if delta_iv > 0 else "crush"
            alerts.append(f"IV {direction.upper()}: {p['ticker']} IV moved {delta_iv*100:+.1f}pp to {p.get('iv',0)*100:.1f}%")
    return alerts


def _check_portfolio_drawdown(analytics: dict) -> list[str]:
    pnl_pct = analytics.get("total_pnl_pct", 0)
    limit = -MAX_DAILY_LOSS_PCT * 100
    if pnl_pct <= limit:
        return [f"DRAWDOWN STOP: portfolio P&L {pnl_pct:.1f}% hits daily loss limit ({limit:.1f}%)"]
    return []


def render_dashboard(analytics: dict, market: dict, alerts: list[str]):
    macro = market["macro"]
    news = market["news"]

    print("\n" + "=" * 70)
    print(f"  PORTFOLIO DASHBOARD  —  {analytics['as_of']}")
    print("=" * 70)
    print(f"  NAV:  ${analytics['total_market_value']:>12,.2f}    P&L: ${analytics['total_pnl']:>+12,.2f}  ({analytics['total_pnl_pct']:+.1f}%)")
    print(f"  Theta bleed/day: ${analytics['daily_theta_bleed']:,.2f}")

    greeks = analytics["aggregate_greeks"]
    print(f"  Delta: {greeks['delta']:,.1f}  Gamma: {greeks['gamma']:.4f}  Vega: {greeks['vega']:.2f}")

    vix_detail = macro.get("signals", {}).get("vix_level", {}).get("detail", "")
    print(f"\n  MACRO  [{macro['regime'].upper()}]  score={macro['composite_macro_score']}  posture={macro.get('posture','')}  {vix_detail}")

    print("\n  SECTOR ALLOCATION")
    for sector, pct in analytics["sector_allocation_pct"].items():
        bar = "█" * int(pct / 2)
        print(f"    {sector:<25} {bar} {pct:.1f}%")

    print("\n  OPTIONS")
    for p in analytics["option_positions"]:
        print(f"    {p['ticker']:<6} {p['expiry']} ${p['strike']:>6}  DTE={p['dte']:>3}  IV={p['iv_pct']:>5.1f}%  "
              f"P&L={p['pnl_pct']:>+6.1f}%  θ=${p['theta_daily']:>+7.2f}/day")

    print("\n  SHARES")
    for p in analytics["share_positions"]:
        print(f"    {p['ticker']:<6} {p['quantity']} shares  spot=${p['spot']:.2f}  P&L={p['pnl_pct']:>+6.1f}%")

    print("\n  NEWS & SENTIMENT")
    for n in news:
        flag = {"consider_exit": " ⚠", "consider_add": " ✚", "monitor": " ~"}.get(n["action_flag"], "")
        print(f"    [{n['sentiment'].upper()[:4]}]{flag}  {n['ticker']}: {n['summary']}")

    ls_scores = market.get("ls_scores", {})
    if ls_scores:
        print("\n  LS EQUITY FUND FACTOR SCORES")
        for ticker, s in ls_scores.items():
            flag = s["long_short_flag"]
            sym = "▲" if flag == "LONG" else ("▼" if flag == "SHORT" else "·")
            print(f"    {ticker:<6} composite={s['composite']:.0f}  "
                  f"mom={s['momentum']:.0f}  qual={s['quality']:.0f}  {sym} {flag}")

    if alerts:
        print("\n  ACTIVE ALERTS")
        for a in alerts:
            print(f"    ! {a}")

    print("=" * 70 + "\n")


def run(valued_positions: list[dict], analytics: dict, market: dict):
    yesterday = _load_yesterday()

    alerts = (
        _detect_new_strikes(valued_positions, yesterday)
        + _check_targets_stops(valued_positions)
        + _check_iv_spikes(valued_positions, yesterday)
        + _check_portfolio_drawdown(analytics)
    )

    render_dashboard(analytics, market, alerts)

    if alerts:
        message = f"RH Tracker Alert {analytics['as_of']}:\n" + "\n".join(alerts)
        send_imessage(message)

    return alerts
