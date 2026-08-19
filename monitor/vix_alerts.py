"""
VIX Trader BOT — alert suppression rules (SRS v1.4, Impl Plan §9).

Mirrors the pattern in loop_daily_rh.py::_should_suppress_alert() for the
equity system, kept in its own module since both loop_daily_vix.py and
loop_intraday_vix.py need the same rules. Suppressed alerts still get
printed/logged and still land in dashboard_cache.json / paper_ledger.jsonl
— they just never reach iMessage.
"""
from __future__ import annotations

import json
import time

from config import VIX_ROLL_ALERT_STATE_FILE

_ROLL_ALERT_COOLDOWN_SEC = 4 * 3600   # 4 hours
_ROLL_ALERT_PNL_DELTA_PP = 0.10       # 10 percentage points


def should_suppress(action_name: str, executed: bool, skip_reason: str) -> tuple[bool, str]:
    """Returns (suppress, reason)."""
    if executed:
        return False, ""

    # A proposed entry rejected purely because ENABLE_VIX_AUTO_BUY is off is
    # not new information — it's the expected, intentional state during the
    # Phase T phased rollout (Impl Plan §11: "Week-1: all auto flags false.
    # Week-2: sell-only. Week-3+: buys enabled"). Alerting on it every cycle
    # the posture wants to buy would just be daily noise until the flag is
    # deliberately flipped. Flatten skips (ENABLE_VIX_AUTO_SELL=false) are
    # NOT suppressed — that flag is expected to stay on, so it going false
    # would itself be worth surfacing.
    if action_name.startswith("BUY_") and skip_reason == "ENABLE_VIX_AUTO_BUY=false":
        return True, "ENABLE_VIX_AUTO_BUY=false is an intentional Phase T config choice, not new information"

    return False, ""


def _roll_position_key(position: dict) -> str:
    return f"{position.get('ticker')}|{position.get('expiry')}|{position.get('strike')}|{position.get('option_type')}"


def _load_roll_alert_state() -> dict:
    if not VIX_ROLL_ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(VIX_ROLL_ALERT_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_roll_alert_state(state: dict) -> None:
    VIX_ROLL_ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))


def should_suppress_roll(position: dict, pnl_pct: float | None) -> tuple[bool, str]:
    """
    Suppress a repeated "roll candidate" alert for the same option
    position if one already fired within the last 4 hours, unless P&L has
    moved >= 10 percentage points since that alert (Impl Plan §9: "Suppress
    duplicate rolls"). Intended for the alert-only case — a roll that
    actually executes is real news every time; callers should route
    executed outcomes straight to the alert, never through this function.

    Persisted to VIX_ROLL_ALERT_STATE_FILE, keyed by ticker/expiry/strike/
    option_type, so the daily batch and every 15-minute intraday cycle
    share the same memory — a candidate seen at 9:35, 9:50, and 10:05 only
    alerts once unless P&L moves enough in between. If pnl_pct is None
    (shouldn't happen — decide_option_management() only proposes
    ROLL_OPTION when pnl_pct is a real number — but defend anyway), the
    P&L-moved override can never fire and this falls back to the plain
    4-hour cooldown.

    This function has a side effect: on a non-suppressed call, it records
    the alert (updates state on disk). Call it at most once per candidate
    per cycle.
    """
    key = _roll_position_key(position)
    state = _load_roll_alert_state()
    prior = state.get(key)

    now = time.time()
    if prior is not None:
        age_sec = now - prior.get("alerted_at", 0)
        prior_pnl = prior.get("pnl_pct")
        pnl_moved_enough = (
            pnl_pct is not None and prior_pnl is not None
            and abs(pnl_pct - prior_pnl) >= _ROLL_ALERT_PNL_DELTA_PP
        )
        if age_sec < _ROLL_ALERT_COOLDOWN_SEC and not pnl_moved_enough:
            return True, (
                f"roll candidate for {key} last alerted {age_sec / 60:.0f} min ago "
                f"(< 4h) and P&L hasn't moved >= 10pp since"
            )

    state[key] = {"alerted_at": now, "pnl_pct": pnl_pct}
    _save_roll_alert_state(state)
    return False, ""
