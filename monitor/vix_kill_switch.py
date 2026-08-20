"""
VIX Trader BOT — SVIX P&L-based auto kill switch. Separate from the static
VIX_KILL_SWITCH env flag (a config value read fresh at process start): this
is dynamic, code-tripped state that must persist across cycles and
processes once triggered, so it lives in its own file rather than as an
importable constant.

Trips flatten-only mode when the held SVIX position's own P&L breaches
VIX_SVIX_STOP_PCT — closing the gap flagged in VIX_OPERATIONS_GUIDE.md
(that constant existed in config since the scaffold's first commit but was
never wired into any decision logic). Manual reset only, by design — a real
stop-out should not silently self-heal just because price ticks back up.
"""
from __future__ import annotations

import json
import time

from config import VIX_AUTO_KILL_STATE_FILE, VIX_SVIX_STOP_PCT


def _held_svix_share(positions: list[dict]) -> dict | None:
    for p in positions:
        if p.get("ticker") == "SVIX" and p.get("type") == "share":
            return p
    return None


def check_and_trip(positions: list[dict]) -> bool:
    """
    Evaluates the held SVIX position's pnl_pct against VIX_SVIX_STOP_PCT.
    Returns True only on the cycle that newly trips the switch — a no-op
    (False) if already tripped (never re-alerts every cycle), SVIX isn't
    held, or pnl_pct is None (fail closed: no data, no trip).
    """
    if is_tripped():
        return False

    svix = _held_svix_share(positions)
    if svix is None:
        return False

    pnl_pct = svix.get("pnl_pct")
    if pnl_pct is None:
        return False

    if pnl_pct <= VIX_SVIX_STOP_PCT:
        payload = {
            "tripped_at": time.time(),
            "reason": f"SVIX position P&L {pnl_pct:.1%} <= stop {VIX_SVIX_STOP_PCT:.1%}",
            "pnl_pct": pnl_pct,
        }
        VIX_AUTO_KILL_STATE_FILE.write_text(json.dumps(payload, indent=2))
        return True

    return False


def is_tripped() -> bool:
    return VIX_AUTO_KILL_STATE_FILE.exists()


def get_trip_info() -> dict | None:
    if not VIX_AUTO_KILL_STATE_FILE.exists():
        return None
    try:
        return json.loads(VIX_AUTO_KILL_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def reset() -> None:
    """The only way out once tripped — deliberate, manual."""
    if VIX_AUTO_KILL_STATE_FILE.exists():
        VIX_AUTO_KILL_STATE_FILE.unlink()
