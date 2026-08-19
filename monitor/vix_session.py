"""
VIX Trader BOT — fail-closed Robinhood session gate (SRS v1.4 §8.2-§8.4,
Impl Plan §2A). This is the gate in front of every live order the VIX
sleeve places. Unofficial robin_stocks is not a reliable broker; `held: []`
is not proof of a flat book.

Deliberately does NOT call monitor.layer0_universe._rh_login() as-is: that
function's fallback path calls a full interactive rh.login() when the
refresh token is dead (see its docstring / final `rh.login(...)` call).
That fallback triggers Robinhood's device-approval push challenge, which
Impl Plan §2A explicitly forbids in unattended loops ("Full rh.login() /
device-approval polling"). This module re-implements only the safe
pickle-load + single-refresh-attempt portion and stops at DEAD instead of
falling through to a full login. Full interactive login stays exclusively
in rh_reauth.py, run by a human.
"""
from __future__ import annotations

import base64
import json
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from config import (
    VIX_ACCOUNT,
    VIX_SESSION_STATE_FILE,
    VIX_SHADOW_BOOK_FILE,
    VIX_SESSION_DEAD_COOLDOWN_SEC,
    VIX_TOKEN_REAUTH_WARN_SEC,
    VIX_SHADOW_BOOK_MAX_AGE_SEC,
)
from monitor.layer0_universe import _AGENTIC_ACCOUNT, _MARGIN_ACCOUNT

_PICKLE = Path.home() / ".tokens" / "robinhood.pickle"
_CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"

_ACCOUNT_NUMBERS = {"AGENTIC": _AGENTIC_ACCOUNT, "MARGIN": _MARGIN_ACCOUNT}

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
DEAD = "DEAD"


@dataclass
class SessionResult:
    state: str
    reason: str = ""
    token_exp: float | None = None
    last_refresh_at: float | None = None
    account_ok: bool = False
    buying_power: float | None = None
    rh_module: object = None  # the logged-in robin_stocks module, if HEALTHY/DEGRADED
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
    vol_tickers = {"SVIX", "VXX", "UVXY"}
    return any(p.get("ticker") in vol_tickers for p in shadow.get("positions", []))


def _save_state(result: SessionResult) -> None:
    VIX_SESSION_STATE_FILE.write_text(json.dumps(result.as_dict(), indent=2))


def _safe_session_refresh() -> tuple[object | None, float | None, str]:
    """
    Pickle-load + at most one OAuth refresh. Returns (rh_module, token_exp,
    error). Never falls through to a full rh.login() — that path is
    forbidden in unattended loops (see module docstring).
    """
    import robin_stocks.robinhood as rh
    from robin_stocks.robinhood.helper import update_session, set_login_state

    if not _PICKLE.exists():
        return None, None, "no pickle on disk"

    try:
        with open(_PICKLE, "rb") as f:
            tok = pickle.load(f)
        access = tok.get("access_token", "")
        refresh = tok.get("refresh_token", "")
        device = tok.get("device_token", "")

        parts = access.split(".")
        exp = json.loads(base64.urlsafe_b64decode(parts[1] + "==")).get("exp", 0)

        if exp > time.time():
            set_login_state(True)
            update_session("Authorization", f"Bearer {access}")
            return rh, exp, ""

        # Exactly one refresh attempt. Save the new tokens before any
        # second call — Robinhood rotates the refresh token on every use.
        resp = requests.post(
            "https://api.robinhood.com/oauth2/token/",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": _CLIENT_ID,
                "device_token": device,
                "scope": "internal",
            },
            timeout=10,
        )
        if resp.status_code == 429:
            return None, None, "429 on token refresh"

        new = resp.json()
        if "access_token" not in new or "refresh_token" not in new:
            return None, None, f"refresh did not return tokens: {new}"

        tok["access_token"] = new["access_token"]
        tok["refresh_token"] = new["refresh_token"]
        with open(_PICKLE, "wb") as f:
            pickle.dump(tok, f)

        new_parts = new["access_token"].split(".")
        new_exp = json.loads(base64.urlsafe_b64decode(new_parts[1] + "==")).get("exp", 0)

        set_login_state(True)
        update_session("Authorization", f"Bearer {new['access_token']}")
        return rh, new_exp, ""
    except Exception as exc:  # noqa: BLE001 — any failure here means DEAD, not a crash
        return None, None, f"{type(exc).__name__}: {exc}"


def assess() -> SessionResult:
    """
    Run the session_ok() checklist (Impl Plan §2A / SRS §8.2). All checks
    must pass for HEALTHY. Persists session_state.json and, only on a
    HEALTHY positions pull, the shadow book.
    """
    if _in_cooldown():
        return SessionResult(state=DEAD, reason="in post-DEAD cooldown, not retrying")

    checks = {}
    rh, exp, err = _safe_session_refresh()
    checks["token_valid_or_refreshed"] = rh is not None
    if rh is None:
        result = SessionResult(state=DEAD, reason=f"session refresh failed: {err}", checks=checks)
        _save_state(result)
        return result

    account_number = _ACCOUNT_NUMBERS.get(VIX_ACCOUNT, _AGENTIC_ACCOUNT)

    try:
        profile = rh.load_portfolio_profile(account_number=account_number)
        buying_power = float(profile.get("equity", "nan"))
        account_ok = profile.get("account_number") in (None, account_number)  # tolerate API variance
        checks["account_ok"] = bool(account_ok)
        checks["buying_power_numeric"] = buying_power == buying_power  # NaN check
    except Exception as exc:  # noqa: BLE001
        result = SessionResult(
            state=DEAD, reason=f"account/equity check failed: {exc}",
            token_exp=exp, checks=checks,
        )
        _save_state(result)
        return result

    if not checks["account_ok"] or not checks["buying_power_numeric"]:
        result = SessionResult(
            state=DEAD, reason="account_ok or buying_power check failed",
            token_exp=exp, buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    try:
        positions = rh.get_open_stock_positions(account_number=account_number)
        checks["positions_payload_structured"] = isinstance(positions, list)
    except Exception as exc:  # noqa: BLE001
        result = SessionResult(
            state=DEAD, reason=f"positions call failed: {exc}",
            token_exp=exp, buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    if not checks["positions_payload_structured"]:
        result = SessionResult(
            state=DEAD, reason="positions payload not structured (API error, not empty list)",
            token_exp=exp, buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    normalized = [
        {"ticker": p.get("symbol") or p.get("ticker", ""), "quantity": p.get("quantity")}
        for p in positions
    ]

    if not normalized and _shadow_book_has_vol_names():
        result = SessionResult(
            state=DEAD,
            reason="BOOK_MISMATCH: held:[] but shadow book had SVIX/VXX/UVXY within max age",
            token_exp=exp, buying_power=buying_power, checks=checks,
        )
        _save_state(result)
        return result

    # All checks passed — HEALTHY. held:[] here is now trustworthy.
    result = SessionResult(
        state=HEALTHY,
        reason="all checks passed",
        token_exp=exp,
        last_refresh_at=time.time(),
        account_ok=True,
        buying_power=buying_power,
        rh_module=rh,
        checks=checks,
    )
    _save_state(result)
    _save_shadow_book(normalized)

    ttl = (exp or 0) - time.time()
    if 0 < ttl < VIX_TOKEN_REAUTH_WARN_SEC:
        result.reason = f"HEALTHY, but token TTL {ttl/3600:.1f}h < warn threshold — reauth soon"

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
