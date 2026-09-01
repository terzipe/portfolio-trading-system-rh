"""
VIX Trader BOT — posture + holdings -> action mapper (SRS v1.4 §7.5-§7.8,
Impl Plan §4). Pure decision logic; does not place orders (see
vix_executor.py) and does not call UW except for the fade-spike check,
which needs recent UVXY/VIX history.
"""
from __future__ import annotations

from dataclasses import dataclass

from data.unusual_whales import UWError
from monitor.vix_regime import LONG_VOL_TACTICAL, FADE_SPIKE_PUTS

SELL_SVIX_ALL = "SELL_SVIX_ALL"
BUY_SVIX_RUNG = "BUY_SVIX_RUNG"
SELL_SVIX_PARTIAL = "SELL_SVIX_PARTIAL"
BUY_UVXY_PUT = "BUY_UVXY_PUT"
BUY_VXX_PUT = "BUY_VXX_PUT"
BUY_VXX_CALL = "BUY_VXX_CALL"
BUY_UVXY_CALL = "BUY_UVXY_CALL"
ROLL_OPTION = "ROLL_OPTION"
CLOSE_OPTION = "CLOSE_OPTION"
HOLD = "HOLD"
NOOP = "NOOP"

_VOL_TICKERS = {"SVIX", "VXX", "UVXY"}


@dataclass
class Action:
    action: str
    ticker: str | None
    reason: str
    position: dict | None = None  # the held position this action targets, if any


def fetch_ticker_history(uw_client, ticker: str, sessions: int = 10) -> list[float] | None:
    """
    Last `sessions` regular-session daily closes (oldest first) for any
    ticker. Confirmed live 2026-08-19 (on UVXY): GET /api/stock/{ticker}/
    ohlc/1d returns one row per session *segment* per day (market_time:
    "pr" pre-market / "r" regular / "po" post-market), not one row per
    day — a naive "last N rows" would mix pre/post segments and undercount
    actual trading days, so this filters to market_time=="r" before taking
    the tail. Returns None on any fetch/parse failure or too few closes —
    fail closed, so callers never make a momentum decision on partial data.
    """
    try:
        payload = uw_client.ohlc(ticker, candle_size="1d")
    except UWError:
        return None
    rows = payload.get("data", [])
    regular = [r for r in rows if r.get("market_time") == "r"]
    closes: list[float] = []
    for row in regular[-sessions:]:
        try:
            closes.append(float(row["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    return closes if len(closes) >= 2 else None


def fetch_uvxy_history(uw_client, sessions: int = 10) -> list[float] | None:
    """UVXY-specific alias of fetch_ticker_history(), for
    evaluate_fade_spike()'s window (SRS §7.5: "UVXY +30% in <=10
    sessions")."""
    return fetch_ticker_history(uw_client, "UVXY", sessions)


def evaluate_fade_spike(uw_client, vix: float | None, uvxy_history: list[float] | None = None) -> bool:
    """
    SRS §7.5 — all three must be true:
      1. Spike already happened: UVXY +30% in <=10 sessions, or VIX rose to
         >=25 from a sub-20 base.
      2. Curve flattening or re-steepening (not still deepening backwardation)
         — approximated here by the caller re-checking vix_term() on the
         next cycle and comparing; this function only evaluates #1 and #3.
      3. VIX has printed a lower high or one session close back toward
         contango.

    `uvxy_history` is a list of recent UVXY closes, oldest first, covering
    at most the last ~10 sessions. Callers without OHLC wiring yet can pass
    None, which makes this conservatively return False (no fade-spike
    entry without confirming data — fail closed, consistent with SRS §6.4).
    """
    if not uvxy_history or len(uvxy_history) < 2:
        return False

    window = uvxy_history[-10:]
    spiked = (max(window) / window[0] - 1) >= 0.30 if window[0] else False
    vix_spiked = vix is not None and vix >= 25

    if not (spiked or vix_spiked):
        return False

    # Lower high or one close back toward contango: the most recent close
    # is below the window's max.
    lower_high = window[-1] < max(window)
    return lower_high


def decide_actions(posture: str, positions: list[dict], longvol_ticker: str | None = None) -> list[Action]:
    """
    Map posture + current UVXY/VXX option holdings to actions. SVIX is no
    longer posture-driven — monitor/vix_ladder.py owns all SVIX entries and
    exits independently, on its own VIX-level campaign, regardless of what
    posture says (see VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md). This
    function is options-only now.

    `longvol_ticker` (added 2026-09-01, VXX/UVXY rotation): which ticker
    vix_regime.compute_posture() actually confirmed for THIS cycle -- see
    its _pick_longvol_ticker() for the rotation/tie-break rule. Still
    mutually exclusive with holding the other (his call, 2026-09-01):
    holding EITHER VXX or UVXY calls blocks a new buy of either, same as
    before this rotation existed -- only which ticker gets bought when
    entering fresh is new. Defaults to None for callers that haven't been
    updated yet, in which case LONG_VOL_TACTICAL can't fire a fresh entry
    (matches vix_regime.compute_posture()'s own "no gates passed -> never
    triggers" fallback convention) but an already-held position still HOLDs
    normally.
    """
    actions: list[Action] = []
    uvxy_options = [p for p in positions if p.get("ticker") == "UVXY" and p.get("type") == "option"]
    vxx_options = [p for p in positions if p.get("ticker") == "VXX" and p.get("type") == "option"]

    if posture == FADE_SPIKE_PUTS:
        if uvxy_options:
            actions.append(Action(HOLD, "UVXY", "posture=FADE_SPIKE_PUTS, put already held", uvxy_options[0]))
        elif vxx_options:
            actions.append(Action(HOLD, "VXX", "posture=FADE_SPIKE_PUTS, fallback put already held", vxx_options[0]))
        else:
            actions.append(Action(BUY_UVXY_PUT, "UVXY", "posture=FADE_SPIKE_PUTS, no put held (UVXY primary, VXX fallback in vix_options)"))
        return actions

    if posture == LONG_VOL_TACTICAL:
        if uvxy_options or vxx_options:
            actions.append(Action(HOLD, "VXX/UVXY", "posture=LONG_VOL_TACTICAL, calls already held"))
        elif longvol_ticker == "UVXY":
            actions.append(Action(BUY_UVXY_CALL, "UVXY", "posture=LONG_VOL_TACTICAL, UVXY's own gates confirmed, no calls held"))
        elif longvol_ticker == "VXX":
            actions.append(Action(BUY_VXX_CALL, "VXX", "posture=LONG_VOL_TACTICAL, VXX's own gates confirmed, no calls held"))
        else:
            # Posture fired but no ticker was resolved (stale caller not
            # passing longvol_ticker yet) -- fail closed, no blind default.
            actions.append(Action(NOOP, None, "posture=LONG_VOL_TACTICAL but no longvol_ticker resolved -- not buying blind"))
        return actions

    # CASH
    actions.append(Action(NOOP, None, "posture=CASH, nothing to do"))
    return actions


def decide_option_management(positions: list[dict], roll_pct: float, tp_pct: float, sl_pct: float) -> list[Action]:
    """Per-option roll/take-profit/stop check, independent of posture —
    an open UVXY/VXX option is managed on its own P&L regardless of what
    the current posture says about new entries (SRS §9)."""
    actions: list[Action] = []
    for pos in positions:
        if pos.get("type") != "option" or pos.get("ticker") not in _VOL_TICKERS:
            continue
        pnl_pct = pos.get("pnl_pct")
        if pnl_pct is None:
            continue
        if pnl_pct <= sl_pct:
            actions.append(Action(CLOSE_OPTION, pos["ticker"], f"stop: {pnl_pct:.0%} <= {sl_pct:.0%}", pos))
        elif pnl_pct >= tp_pct:
            actions.append(Action(CLOSE_OPTION, pos["ticker"], f"take-profit: {pnl_pct:.0%} >= {tp_pct:.0%}", pos))
        elif pnl_pct >= roll_pct:
            actions.append(Action(ROLL_OPTION, pos["ticker"], f"roll candidate: {pnl_pct:.0%} >= {roll_pct:.0%}", pos))
    return actions
