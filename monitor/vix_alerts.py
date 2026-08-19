"""
VIX Trader BOT — alert suppression rules (SRS v1.4, Impl Plan §9).

Mirrors the pattern in loop_daily_rh.py::_should_suppress_alert() for the
equity system, kept in its own module since both loop_daily_vix.py and
loop_intraday_vix.py need the same rules. Suppressed alerts still get
printed/logged and still land in dashboard_cache.json / paper_ledger.jsonl
— they just never reach iMessage.
"""
from __future__ import annotations


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
