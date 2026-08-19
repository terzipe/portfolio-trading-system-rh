"""
VIX Trader BOT — order executor (SRS v1.4 §8.5, §9, Impl Plan §6).

Deliberately does NOT use broker/robinhood.py for options, contrary to the
implementation plan's literal suggestion ("use the path that already works
in broker/robinhood.py"). broker/robinhood.py's login() always calls a
full rh.login(username, password) — every one of its functions triggers
that on first use in a process. That is exactly the device-approval-
challenge risk SRS §8.3 forbids in unattended loops ("Unattended loops
must not call full rh.login()"), and RH Tracker's own README already flags
this as broker/robinhood.py's known limitation. Instead, every order here
is placed directly on the already-authenticated `rh` module object that
vix_session.assess() returns (same pattern the RH README recommends for
manual equity orders) — confirmed both order_*_market and
order_*_option_limit accept account_number= and timeInForce= directly in
the installed robin_stocks version.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime

from config import (
    VIX_ACCOUNT, VIX_STATE_FILE, VIX_SLEEVE_MAX_PCT, VIX_SVIX_MAX_PCT,
    VIX_KILL_SWITCH, ENABLE_VIX_AUTO_SELL, ENABLE_VIX_AUTO_BUY,
)
from monitor.layer0_universe import _AGENTIC_ACCOUNT, _MARGIN_ACCOUNT
from monitor.vix_session import SessionResult, HEALTHY, can_flatten, can_buy
from monitor.vix_signals import (
    Action, SELL_SVIX_ALL, BUY_SVIX_SHARES, BUY_UVXY_PUT, BUY_VXX_PUT,
    BUY_VXX_CALL, BUY_UVXY_CALL, ROLL_OPTION, CLOSE_OPTION, HOLD, NOOP,
)

_ACCOUNT_NUMBERS = {"AGENTIC": _AGENTIC_ACCOUNT, "MARGIN": _MARGIN_ACCOUNT}
_MAX_ORDERS_PER_CYCLE = 3
_TIME_IN_FORCE = "gfd"


@dataclass
class ExecutionOutcome:
    action: Action
    executed: bool                        # True only if a real order was submitted to Robinhood
    would_execute: bool = False           # True if every gating check passed (live or dry-run)
    order_preview: dict | None = None     # the exact call args that were/would be sent
    dry_run: bool = False
    order_id: str | None = None
    price: float | None = None
    skip_reason: str = ""


def _account_number() -> str:
    return _ACCOUNT_NUMBERS.get(VIX_ACCOUNT, _AGENTIC_ACCOUNT)


def _load_state() -> dict:
    if not VIX_STATE_FILE.exists():
        return {}
    try:
        return json.loads(VIX_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    VIX_STATE_FILE.write_text(json.dumps(state, indent=2))


def _session_id() -> str:
    """One 'session' = one calendar trading day, per Impl Plan's
    '1 new entry per session' / 're-entry lock until next session' rule."""
    return date.today().isoformat()


def _entry_locked(state: dict) -> bool:
    return state.get("last_entry_session") == _session_id()


def _poll_order_confirmed(rh, order_id: str, timeout: float = 20.0) -> bool:
    """After every order, poll status; caller stops the cycle if unconfirmed
    (no second market order same cycle) — SRS §8.5."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = rh.get_stock_order_info(order_id) if order_id else None
        except Exception:  # noqa: BLE001
            info = None
        state = (info or {}).get("state")
        if state in ("filled", "confirmed", "queued", "partially_filled"):
            return True
        if state in ("rejected", "cancelled", "failed"):
            return False
        time.sleep(2)
    return False


def _sleeve_pct_ok(current_mv: float, proposed_mv: float, nav: float, svix_only: bool = False) -> bool:
    if nav <= 0:
        return False
    cap = VIX_SVIX_MAX_PCT if svix_only else VIX_SLEEVE_MAX_PCT
    return (current_mv + proposed_mv) / nav <= cap


def execute_actions(
    actions: list[Action],
    session: SessionResult,
    nav: float,
    sleeve_mv: float,
    quote_fn,  # callable(ticker) -> last price, used for market-order sizing/paper price
    dry_run: bool = False,
) -> list[ExecutionOutcome]:
    """
    Attempt to execute a batch of decided actions. Order of enforcement:
    kill switch > session state > per-action flag > per-cycle/per-session
    caps > sleeve % > confirmation poll.

    dry_run=True runs every real gating check (kill switch, session state,
    ENABLE_VIX_AUTO_SELL/BUY, sleeve %, per-cycle/per-session caps) against
    real inputs, but stops short of calling any robin_stocks order
    function — see _execute_flatten/_execute_entry. Session-scoped state
    (last_entry_session, last_flatten_session) is tracked in-memory for the
    duration of this call so the caps behave identically to a live run, but
    is never persisted to VIX_STATE_FILE, so a dry run leaves no trace that
    could confuse a subsequent real run's re-entry lock.
    """
    outcomes: list[ExecutionOutcome] = []
    if session.rh_module is None:
        for a in actions:
            outcomes.append(ExecutionOutcome(a, False, skip_reason=f"session not usable ({session.state})"))
        return outcomes

    rh = session.rh_module
    account_number = _account_number()
    state = _load_state()
    orders_this_cycle = 0

    for action in actions:
        if orders_this_cycle >= _MAX_ORDERS_PER_CYCLE:
            outcomes.append(ExecutionOutcome(action, False, skip_reason="max 3 live orders per cycle reached"))
            continue

        if action.action in (HOLD, NOOP):
            outcomes.append(ExecutionOutcome(action, False, skip_reason="no-op"))
            continue

        is_flatten = action.action in (SELL_SVIX_ALL, CLOSE_OPTION)
        is_entry = action.action in (BUY_SVIX_SHARES, BUY_UVXY_PUT, BUY_VXX_PUT, BUY_VXX_CALL, BUY_UVXY_CALL)
        is_roll = action.action == ROLL_OPTION

        if VIX_KILL_SWITCH and not is_flatten:
            outcomes.append(ExecutionOutcome(action, False, skip_reason="VIX_KILL_SWITCH=true — flatten-only"))
            continue

        if is_flatten:
            if not ENABLE_VIX_AUTO_SELL:
                outcomes.append(ExecutionOutcome(action, False, skip_reason="ENABLE_VIX_AUTO_SELL=false"))
                continue
            if not can_flatten(session):
                outcomes.append(ExecutionOutcome(action, False, skip_reason=f"session {session.state} cannot flatten"))
                continue

            outcome = _execute_flatten(rh, action, account_number, dry_run=dry_run)
            outcomes.append(outcome)
            if outcome.executed or outcome.would_execute:
                orders_this_cycle += 1
                state["last_flatten_reason"] = action.reason
                state["last_flatten_session"] = _session_id()
                if not dry_run:
                    _save_state(state)
                if outcome.executed:
                    confirmed = _poll_order_confirmed(rh, outcome.order_id)
                    if not confirmed:
                        outcomes[-1].skip_reason = "order unconfirmed — stopping cycle, no second market order"
                        break
            continue

        if is_entry:
            if not ENABLE_VIX_AUTO_BUY:
                outcomes.append(ExecutionOutcome(action, False, skip_reason="ENABLE_VIX_AUTO_BUY=false"))
                continue
            if not can_buy(session):
                outcomes.append(ExecutionOutcome(action, False, skip_reason=f"session {session.state} != HEALTHY, no buys"))
                continue
            if state.get("last_flatten_session") == _session_id():
                outcomes.append(ExecutionOutcome(action, False, skip_reason="re-entry locked — flattened this session"))
                continue
            if _entry_locked(state):
                outcomes.append(ExecutionOutcome(action, False, skip_reason="1 new entry per session already used"))
                continue

            price = quote_fn(action.ticker) if action.ticker else None
            proposed_mv = 0.0  # sized by caller before calling this in a real S3 build; scaffold logs intent
            svix_only = action.action == BUY_SVIX_SHARES
            if not _sleeve_pct_ok(sleeve_mv, proposed_mv, nav, svix_only=svix_only):
                outcomes.append(ExecutionOutcome(action, False, skip_reason="sleeve % cap would be exceeded"))
                continue

            outcome = _execute_entry(rh, action, account_number, price, dry_run=dry_run)
            outcomes.append(outcome)
            if outcome.executed or outcome.would_execute:
                orders_this_cycle += 1
                state["last_entry_session"] = _session_id()
                if not dry_run:
                    _save_state(state)
                if outcome.executed:
                    confirmed = _poll_order_confirmed(rh, outcome.order_id)
                    if not confirmed:
                        outcomes[-1].skip_reason = "order unconfirmed — stopping cycle, no second market order"
                        break
            continue

        if is_roll:
            outcomes.append(ExecutionOutcome(action, False, skip_reason="ROLL_OPTION requires ENABLE_VIX_AUTO_ROLL — not implemented in scaffold, alert only"))
            continue

    return outcomes


def _execute_flatten(rh, action: Action, account_number: str, dry_run: bool = False) -> ExecutionOutcome:
    try:
        if action.ticker == "SVIX" and action.position and action.position.get("type") == "share":
            qty = action.position.get("quantity", 0)
            if qty <= 0:
                return ExecutionOutcome(action, False, skip_reason="no SVIX quantity to sell")
            preview = {
                "call": "order_sell_market", "symbol": action.ticker, "quantity": qty,
                "account_number": account_number, "timeInForce": _TIME_IN_FORCE,
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            resp = rh.order_sell_market(
                action.ticker, qty, account_number=account_number, timeInForce=_TIME_IN_FORCE
            )
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=(resp or {}).get("id"))

        if action.action == CLOSE_OPTION and action.position:
            pos = action.position
            preview = {
                "call": "order_sell_option_limit", "position_effect": "close", "credit_or_debit": "credit",
                "price": pos.get("mid_price", pos.get("cost_basis", 0)), "symbol": pos["ticker"],
                "quantity": pos.get("contracts", 1), "expiry": pos["expiry"], "strike": pos["strike"],
                "option_type": pos["option_type"], "account_number": account_number, "timeInForce": _TIME_IN_FORCE,
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            resp = rh.order_sell_option_limit(
                "close", "credit", pos.get("mid_price", pos.get("cost_basis", 0)),
                pos["ticker"], pos.get("contracts", 1), pos["expiry"], pos["strike"], pos["option_type"],
                account_number=account_number, timeInForce=_TIME_IN_FORCE,
            )
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=(resp or {}).get("id"))

        return ExecutionOutcome(action, False, skip_reason="no matching flatten handler")
    except Exception as exc:  # noqa: BLE001
        return ExecutionOutcome(action, False, skip_reason=f"order failed: {exc}")


def _execute_entry(rh, action: Action, account_number: str, price: float | None, dry_run: bool = False) -> ExecutionOutcome:
    try:
        if action.action == BUY_SVIX_SHARES:
            if price is None or price <= 0:
                return ExecutionOutcome(action, False, skip_reason="no price to size SVIX buy")
            # Scaffold: quantity sizing (nav * cap / price) is the caller's
            # job once real position sizing is wired in S3 — placeholder
            # of 0 shares intentionally refuses to submit (or preview) an
            # order until that's implemented, fail-closed rather than
            # guessing a size. dry_run does not change this — there is
            # nothing meaningful to preview yet.
            return ExecutionOutcome(action, False, skip_reason="position sizing not wired yet (S3 TODO) — refusing to guess quantity")

        # Options entries (BUY_UVXY_PUT / BUY_VXX_PUT / BUY_VXX_CALL / BUY_UVXY_CALL)
        # require a ContractPick from vix_options — caller is expected to
        # have run vix_options.pick_put()/pick_call() and attached it to
        # the Action before calling execute_actions(). Scaffold refuses to
        # guess a contract, dry_run or not.
        return ExecutionOutcome(action, False, skip_reason="option contract not attached to action (S3 TODO)")
    except Exception as exc:  # noqa: BLE001
        return ExecutionOutcome(action, False, skip_reason=f"order failed: {exc}")
