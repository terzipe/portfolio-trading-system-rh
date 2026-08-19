"""
VIX Trader BOT — fail-closed Alpaca paper-trading session gate (SRS v1.4
§8.2-§8.4, Impl Plan §2A). This is the gate in front of every order the VIX
sleeve places. `held: []` is not proof of a flat book — same BOOK_MISMATCH
philosophy as the Robinhood version this replaces.

Migrated from Robinhood: Alpaca paper trading authenticates with a static
API key pair (ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY), not a pickled OAuth
token. There is no refresh dance and no device-approval challenge to avoid,
so this module is a straight construct-client-then-run-checklist, unlike
the RH version's pickle-load + single-refresh-attempt dance.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from alpaca.trading.client import TradingClient

from config import (
    ALPACA_API_KEY_ID,
    ALPACA_API_SECRET_KEY,
    VIX_SESSION_STATE_FILE,
    VIX_SHADOW_BOOK_FILE,
    VIX_SESSION_DEAD_COOLDOWN_SEC,
    VIX_SHADOW_BOOK_MAX_AGE_SEC,
)

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
DEAD = "DEAD"

_VOL_TICKERS = {"SVIX", "VXX", "UVXY"}


@dataclass
class SessionResult:
    state: str
    reason: str = ""
    token_exp: float | None = None  # kept for dashboard TTL widget compat; always None for Alpaca (no expiring token)
    last_refresh_at: float | None = None
    account_ok: bool = False
    buying_power: float | None = None
    client: object = None  # the authenticated Alpaca TradingClient, if HEALTHY/DEGRADED
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "token_exp": self.token_exp,
            "last_refresh_at": self.last_refresh_at,
            "account_ok": self.account_ok,
            "buying_power": self.buying_power,
            "checks": self.checks,
            "saved_at": time.time(),
        }


def _in_cooldown() -> bool:
    """After a DEAD result, wait >= VIX_SESSION_DEAD_COOLDOWN_SEC before
    trying again — Impl Plan §2A: 'After DEAD, cooldown >= 15 minutes. No
    inner retry loop.'"""
    if not VIX_SESSION_STATE_FILE.exists():
        return False
    try:
        prev = json.loads(VIX_SESSION_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if prev.get("state") != DEAD:
        return False
    saved_at = prev.get("saved_at", 0)
    return (time.time() - saved_at) < VIX_SESSION_DEAD_COOLDOWN_SEC


def _load_shadow_book() -> dict | None:
    if not VIX_SHADOW_BOOK_FILE.exists():
        return None
    try:
        return json.loads(VIX_SHADOW_BOOK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_shadow_book(positions: list[dict]) -> None:
    """Only ever called after a HEALTHY positions pull (SRS §8.2)."""
    payload = {"saved_at": time.time(), "positions": positions}
    VIX_SHADOW_BOOK_FILE.write_text(json.dumps(payload, indent=2))


def _shadow_book_has_vol_names(max_age_sec: int = VIX_SHADOW_BOOK_MAX_AGE_SEC) -> bool:
    shadow = _load_shadow_book()
    if not shadow:
        return False
    if (time.time() - shadow.get("saved_at", 0)) > max_age_sec:
        return False
    return any(p.get("ticker") in _VOL_TICKERS for p in shadow.get("positions", []))


def _save_state(result: SessionResult) -> None:
    VIX_SESSION_STATE_FILE.write_text(json.dumps(result.as_dict(), indent=2))


def _build_client():
    """Construct the paper-trading TradingClient. Returns None if API keys
    are missing — that's a DEAD condition, not a crash."""
    if not ALPACA_API_KEY_ID or not ALPACA_API_SECRET_KEY:
        return None
    return TradingClient(ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, paper=True)


def assess() -> SessionResult:
    """
    Run the session_ok() checklist (Impl Plan §2A / SRS §8.2). All checks
    must pass for HEALTHY. Persists session_state.json and, only on a
    HEALTHY positions pull, the shadow book.
    """
    if _in_cooldown():
        return SessionResult(state=DEAD, reason="in post-DEAD cooldown, not retrying")

    checks = {}
    client = _build_client()
    checks["client_built"] = client is not None
    if client is None:
        result = SessionResult(state=DEAD, reason="ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not set", checks=checks)
        _save_state(result)
        return result

    try:
        account = client.get_account()
        buying_power = float(account.buying_power)
        account_ok = account.status == "ACTIVE" and not account.trading_blocked and not account.account_blocked
        checks["account_ok"] = bool(account_ok)
        checks["buying_power_numeric"] = buying_power == buying_power  # NaN check
    except Exception as exc:  # noqa: BLE001
        result = SessionResult(state=DEAD, reason=f"account/equity check failed: {exc}", checks=checks)
        _save_state(result)
        return result

    if not checks["account_ok"] or not checks["buying_power_numeric"]:
        result = SessionResult(
            state=DEAD, reason="account_ok or buying_power check failed",
            buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    try:
        positions = client.get_all_positions()
        checks["positions_payload_structured"] = isinstance(positions, list)
    except Exception as exc:  # noqa: BLE001
        result = SessionResult(
            state=DEAD, reason=f"positions call failed: {exc}",
            buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    if not checks["positions_payload_structured"]:
        result = SessionResult(
            state=DEAD, reason="positions payload not structured (API error, not empty list)",
            buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    normalized = [
        {"ticker": p.symbol, "quantity": p.qty}
        for p in positions
    ]

    if not normalized and _shadow_book_has_vol_names():
        result = SessionResult(
            state=DEAD,
            reason="BOOK_MISMATCH: held:[] but shadow book had SVIX/VXX/UVXY within max age",
            buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    # All checks passed — HEALTHY. held:[] here is now trustworthy.
    result = SessionResult(
        state=HEALTHY,
        reason="all checks passed",
        last_refresh_at=time.time(),
        account_ok=True,
        buying_power=buying_power,
        client=client,
        checks=checks,
    )
    _save_state(result)
    _save_shadow_book(normalized)
    return result


def can_flatten(result: SessionResult) -> bool:
    """Flatten is allowed HEALTHY or DEGRADED-with-trusted-shadow-book (SRS §8.1/§8.3)."""
    if result.state == HEALTHY:
        return True
    if result.state == DEGRADED and _shadow_book_has_vol_names():
        return True
    return False


def can_buy(result: SessionResult) -> bool:
    return result.state == HEALTHY


if __name__ == "__main__":
    r = assess()
    print(f"state={r.state} reason={r.reason!r} buying_power={r.buying_power}")
