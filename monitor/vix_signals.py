"""
VIX Trader BOT — posture + holdings -> action mapper (SRS v1.4 §7.5-§7.8,
Impl Plan §4). Pure decision logic; does not place orders (see
vix_executor.py) and does not call UW except for the fade-spike check,
which needs recent UVXY/VIX history.
"""
from __future__ import annotations

from dataclasses import dataclass

from data.unusual_whales import UWError
from monitor.vix_regime import SVIX_ON, LONG_VOL_TACTICAL, FADE_SPIKE_PUTS, CASH, FLATTEN_SVIX

SELL_SVIX_ALL = "SELL_SVIX_ALL"
BUY_SVIX_SHARES = "BUY_SVIX_SHARES"
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


def _held(positions: list[dict], ticker: str, pos_type: str | None = None) -> list[dict]:
    return [
        p for p in positions
        if p.get("ticker") == ticker and (pos_type is None or p.get("type") == pos_type)
    ]


def fetch_uvxy_history(uw_client, sessions: int = 10) -> list[float] | None:
    """
    Last `sessions` UVXY regular-session daily closes (oldest first), for
    evaluate_fade_spike()'s window (SRS §7.5: "UVXY +30% in <=10
    sessions"). Confirmed live 2026-08-19: GET /api/stock/UVXY/ohlc/1d
    returns one row per session *segment* per day (market_time: "pr"
    pre-market / "r" regular / "po" post-market), not one row per day — a
    naive "last N rows" would mix pre/post segments and undercount actual
    trading days, so this filters to market_time=="r" before taking the
    tail. Returns None on any fetch/parse failure or too few closes — fail
    closed, matching evaluate_fade_spike()'s own None-history contract (no
    fade-spike entry without confirming data, per SRS §6.4).
    """
    try:
        payload = uw_client.ohlc("UVXY", candle_size="1d")
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


def decide_actions(posture: str, positions: list[dict]) -> list[Action]:
    """
    Map posture + current RH positions to actions. Conflict rule (Impl Plan
    §4): if RH holds SVIX while posture is FLATTEN_SVIX or
    LONG_VOL_TACTICAL, the action is sell first — no simultaneous
    sell+buy in one decide_actions() call, the executor re-evaluates next
    cycle after the sell confirms.
    """
    actions: list[Action] = []
    svix_shares = _held(positions, "SVIX", "share")
    uvxy_options = [p for p in positions if p.get("ticker") == "UVXY" and p.get("type") == "option"]
    vxx_options = [p for p in positions if p.get("ticker") == "VXX" and p.get("type") == "option"]

    if posture == FLATTEN_SVIX:
        if svix_shares:
            actions.append(Action(SELL_SVIX_ALL, "SVIX", "posture=FLATTEN_SVIX", svix_shares[0]))
        else:
            actions.append(Action(NOOP, "SVIX", "posture=FLATTEN_SVIX but no SVIX held"))
        return actions  # sell first — do not also propose entries this cycle

    if posture == SVIX_ON:
        if svix_shares:
            actions.append(Action(HOLD, "SVIX", "posture=SVIX_ON, already held", svix_shares[0]))
        else:
            actions.append(Action(BUY_SVIX_SHARES, "SVIX", "posture=SVIX_ON, no SVIX held"))
        return actions

    if posture == FADE_SPIKE_PUTS:
        if svix_shares:
            # Conflict: flatten the opposite-direction position first.
            actions.append(Action(SELL_SVIX_ALL, "SVIX", "posture=FADE_SPIKE_PUTS conflicts with held SVIX", svix_shares[0]))
            return actions
        if uvxy_options:
            actions.append(Action(HOLD, "UVXY", "posture=FADE_SPIKE_PUTS, put already held", uvxy_options[0]))
        elif vxx_options:
            actions.append(Action(HOLD, "VXX", "posture=FADE_SPIKE_PUTS, fallback put already held", vxx_options[0]))
        else:
            actions.append(Action(BUY_UVXY_PUT, "UVXY", "posture=FADE_SPIKE_PUTS, no put held (UVXY primary, VXX fallback in vix_options)"))
        return actions

    if posture == LONG_VOL_TACTICAL:
        if svix_shares:
            actions.append(Action(SELL_SVIX_ALL, "SVIX", "posture=LONG_VOL_TACTICAL conflicts with held SVIX", svix_shares[0]))
            return actions
        if not uvxy_options and not vxx_options:
            actions.append(Action(BUY_VXX_CALL, "VXX", "posture=LONG_VOL_TACTICAL, Aug-Oct bias, no calls held"))
        else:
            actions.append(Action(HOLD, "VXX/UVXY", "posture=LONG_VOL_TACTICAL, calls already held"))
        return actions

    # CASH
    if svix_shares:
        actions.append(Action(SELL_SVIX_ALL, "SVIX", "posture=CASH, gate/data does not support holding SVIX", svix_shares[0]))
    else:
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
