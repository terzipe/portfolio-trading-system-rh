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
    VIX_MAX_CONTRACTS, VIX_KILL_SWITCH, ENABLE_VIX_AUTO_SELL, ENABLE_VIX_AUTO_BUY,
    ENABLE_VIX_AUTO_ROLL,
)
from monitor import vix_options
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


def _poll_order_confirmed(rh, order_id: str, is_option: bool = False, timeout: float = 20.0) -> bool:
    """After every order, poll status; caller stops the cycle if unconfirmed
    (no second market order same cycle) — SRS §8.5. robin_stocks uses a
    distinct lookup for option orders (get_option_order_info) vs equity
    orders (get_stock_order_info) — calling the wrong one for CLOSE_OPTION
    or a BUY_*_PUT/CALL fill would silently mis-report every option order
    as unconfirmed."""
    lookup = rh.get_option_order_info if is_option else rh.get_stock_order_info
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = lookup(order_id) if order_id else None
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


def _size_svix_shares(nav: float, sleeve_mv: float, price: float) -> tuple[int, float]:
    """
    Target dollar allocation is the tighter of VIX_SVIX_MAX_PCT (SVIX alone)
    and whatever headroom remains under VIX_SLEEVE_MAX_PCT (all vol
    products combined, including any held VXX/UVXY options). Only called
    when entering fresh — vix_signals.decide_actions() only proposes
    BUY_SVIX_SHARES when no SVIX is currently held (SRS §7.7/§8.5) — so the
    SVIX cap alone, not current_svix_mv + proposed, bounds it directly.
    Rounds down; a target that can't afford even one share returns (0, 0.0)
    rather than rounding up past either cap.
    """
    if nav <= 0 or price <= 0:
        return 0, 0.0
    svix_cap_mv = VIX_SVIX_MAX_PCT * nav
    sleeve_headroom_mv = max(0.0, VIX_SLEEVE_MAX_PCT * nav - sleeve_mv)
    target_mv = min(svix_cap_mv, sleeve_headroom_mv)
    quantity = int(target_mv // price)
    return quantity, quantity * price


def _size_option_contracts(nav: float, sleeve_mv: float, premium_per_contract: float) -> int:
    """
    UVXY/VXX puts and calls are not SVIX, so only the general sleeve cap
    applies (VIX_SVIX_MAX_PCT is SVIX-specific) — bounded further by
    VIX_MAX_CONTRACTS regardless of how much dollar headroom remains.
    premium_per_contract is dollars per contract (RH Tracker convention:
    quoted mid * 100), matching vix_positions.py's cost_basis convention.
    Rounds down; refuses (0) rather than exceeding either cap.
    """
    if nav <= 0 or premium_per_contract <= 0:
        return 0
    sleeve_headroom_mv = max(0.0, VIX_SLEEVE_MAX_PCT * nav - sleeve_mv)
    by_headroom = int(sleeve_headroom_mv // premium_per_contract)
    return max(0, min(VIX_MAX_CONTRACTS, by_headroom))


def _size_and_explain_option(contract, nav: float, sleeve_mv: float) -> tuple[int, float, str]:
    """
    Shared by the put and call entry branches. Returns (quantity,
    proposed_mv, skip_reason) — skip_reason is "" iff quantity > 0.
    Zero-premium contracts (bid=ask=0, e.g. a thin fallback strike with no
    real market — hit live 2026-08-19 on a VXX put fallback) get their own
    message: "exceeds sleeve headroom" would be actively misleading there,
    since more NAV wouldn't fix an untradeable quote.
    """
    premium = contract.mid * 100
    if premium <= 0:
        return 0, 0.0, (
            f"{contract.ticker} {contract.expiry} {contract.strike}{contract.option_type[0]} "
            f"has no real market (bid=${contract.bid:.2f}, ask=${contract.ask:.2f}) — refusing to size a trade on it"
        )
    quantity = _size_option_contracts(nav, sleeve_mv, premium)
    proposed_mv = quantity * premium
    if quantity <= 0:
        return 0, 0.0, (
            f"sized to 0 contracts — {contract.ticker} {contract.expiry} {contract.strike}"
            f"{contract.option_type[0]} at ${premium:.0f}/contract exceeds sleeve headroom or "
            f"VIX_MAX_CONTRACTS={VIX_MAX_CONTRACTS} (NAV=${nav:.2f}, sleeve_mv=${sleeve_mv:.2f})"
        )
    return quantity, proposed_mv, ""


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
                    confirmed = _poll_order_confirmed(rh, outcome.order_id, is_option=(action.action == CLOSE_OPTION))
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
            quantity = None
            contract = None

            if action.action == BUY_SVIX_SHARES:
                if price is None or price <= 0:
                    outcomes.append(ExecutionOutcome(action, False, skip_reason="no price to size SVIX buy"))
                    continue
                quantity, proposed_mv = _size_svix_shares(nav, sleeve_mv, price)
                if quantity <= 0:
                    outcomes.append(ExecutionOutcome(
                        action, False,
                        skip_reason=(
                            f"sized to 0 shares — target allocation ${proposed_mv:.2f} at ${price:.2f}/share "
                            f"rounds down to 0 (NAV=${nav:.2f}, sleeve_mv=${sleeve_mv:.2f}, "
                            f"caps: SVIX {VIX_SVIX_MAX_PCT:.0%}, sleeve {VIX_SLEEVE_MAX_PCT:.0%})"
                        ),
                    ))
                    continue

            elif action.action in (BUY_UVXY_PUT, BUY_VXX_PUT):
                if price is None or price <= 0:
                    outcomes.append(ExecutionOutcome(action, False, skip_reason=f"no spot price for {action.ticker} to pick a put"))
                    continue
                contract = vix_options.pick_put(spot_price=price)
                if contract is None:
                    outcomes.append(ExecutionOutcome(action, False, skip_reason="no liquid UVXY/VXX put found in 10-21 DTE window"))
                    continue
                quantity, proposed_mv, size_reason = _size_and_explain_option(contract, nav, sleeve_mv)
                if quantity <= 0:
                    outcomes.append(ExecutionOutcome(action, False, skip_reason=size_reason))
                    continue

            elif action.action in (BUY_VXX_CALL, BUY_UVXY_CALL):
                contract = vix_options.pick_call(ticker=action.ticker)
                if contract is None:
                    outcomes.append(ExecutionOutcome(action, False, skip_reason=f"no liquid {action.ticker} call found in 21-45 DTE, delta 0.40-0.60 window"))
                    continue
                quantity, proposed_mv, size_reason = _size_and_explain_option(contract, nav, sleeve_mv)
                if quantity <= 0:
                    outcomes.append(ExecutionOutcome(action, False, skip_reason=size_reason))
                    continue

            else:
                proposed_mv = 0.0

            svix_only = action.action == BUY_SVIX_SHARES
            if not _sleeve_pct_ok(sleeve_mv, proposed_mv, nav, svix_only=svix_only):
                outcomes.append(ExecutionOutcome(action, False, skip_reason="sleeve % cap would be exceeded"))
                continue

            outcome = _execute_entry(rh, action, account_number, price, quantity=quantity, contract=contract, dry_run=dry_run)
            outcomes.append(outcome)
            if outcome.executed or outcome.would_execute:
                orders_this_cycle += 1
                state["last_entry_session"] = _session_id()
                if not dry_run:
                    _save_state(state)
                if outcome.executed:
                    confirmed = _poll_order_confirmed(rh, outcome.order_id, is_option=(action.action != BUY_SVIX_SHARES))
                    if not confirmed:
                        outcomes[-1].skip_reason = "order unconfirmed — stopping cycle, no second market order"
                        break
            continue

        if is_roll:
            if not ENABLE_VIX_AUTO_ROLL:
                outcomes.append(ExecutionOutcome(action, False, skip_reason="ENABLE_VIX_AUTO_ROLL=false"))
                continue
            # A roll's second leg opens new risk (a fresh contract), so it
            # needs the same HEALTHY bar as any other entry — not just the
            # looser HEALTHY-or-DEGRADED bar that flatten-only actions get.
            if not can_buy(session):
                outcomes.append(ExecutionOutcome(action, False, skip_reason=f"session {session.state} != HEALTHY, no rolls (second leg opens new risk)"))
                continue
            if orders_this_cycle + 2 > _MAX_ORDERS_PER_CYCLE:
                outcomes.append(ExecutionOutcome(action, False, skip_reason="roll needs up to 2 orders (close+open); cycle budget would be exceeded"))
                continue

            roll_price = quote_fn(action.ticker) if action.ticker else None
            roll_outcomes = _execute_roll(rh, action, account_number, roll_price, dry_run=dry_run)
            outcomes.extend(roll_outcomes)
            orders_this_cycle += sum(1 for o in roll_outcomes if o.executed or o.would_execute)

            last_leg = roll_outcomes[-1]
            if last_leg.executed:
                confirmed = _poll_order_confirmed(rh, last_leg.order_id, is_option=True)
                if not confirmed:
                    outcomes[-1].skip_reason = "roll open leg unconfirmed — stopping cycle, no further orders"
                    break
            elif not dry_run and any(o.executed for o in roll_outcomes):
                # Close leg went through live but the open leg was refused
                # or failed for a real reason (no liquid replacement, etc.)
                # — sleeve is now flat on this leg, not doubled. Stop the
                # cycle rather than risk stacking further orders on top of
                # an already-eventful one.
                break
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


def _execute_entry(
    rh, action: Action, account_number: str, price: float | None,
    quantity: int | None = None, contract=None, dry_run: bool = False,
) -> ExecutionOutcome:
    try:
        if action.action == BUY_SVIX_SHARES:
            if price is None or price <= 0:
                return ExecutionOutcome(action, False, skip_reason="no price to size SVIX buy")
            if not quantity or quantity <= 0:
                # Caller (execute_actions) is expected to have already
                # refused a 0-quantity size with a detailed reason before
                # reaching here — this is a defensive fallback, not the
                # primary path.
                return ExecutionOutcome(action, False, skip_reason="sized to 0 shares — refusing to submit")
            preview = {
                "call": "order_buy_market", "symbol": action.ticker, "quantity": quantity,
                "account_number": account_number, "timeInForce": _TIME_IN_FORCE,
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            resp = rh.order_buy_market(
                action.ticker, quantity, account_number=account_number, timeInForce=_TIME_IN_FORCE
            )
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=(resp or {}).get("id"))

        if action.action in (BUY_UVXY_PUT, BUY_VXX_PUT, BUY_VXX_CALL, BUY_UVXY_CALL):
            if contract is None or not quantity or quantity <= 0:
                # Caller is expected to have already refused with a detailed
                # reason (no liquid contract found, or sized to 0) before
                # reaching here — defensive fallback, not the primary path.
                return ExecutionOutcome(action, False, skip_reason="no contract/quantity to submit — refusing")
            # Pay the ask on a buy-to-open — a marketable limit, not a
            # market order (SRS: never 0-DTE / defined-risk options only,
            # a naked market order on an option is not that).
            preview = {
                "call": "order_buy_option_limit", "position_effect": "open", "credit_or_debit": "debit",
                "price": contract.ask, "symbol": contract.ticker, "quantity": quantity,
                "expiry": contract.expiry, "strike": contract.strike, "option_type": contract.option_type,
                "account_number": account_number, "timeInForce": _TIME_IN_FORCE,
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            resp = rh.order_buy_option_limit(
                "open", "debit", contract.ask, contract.ticker, quantity,
                contract.expiry, contract.strike, contract.option_type,
                account_number=account_number, timeInForce=_TIME_IN_FORCE,
            )
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=(resp or {}).get("id"))

        return ExecutionOutcome(action, False, skip_reason="unrecognized entry action")
    except Exception as exc:  # noqa: BLE001
        return ExecutionOutcome(action, False, skip_reason=f"order failed: {exc}")


def _roll_entry_action_for(contract) -> str:
    """Map a freshly-picked ContractPick back to the BUY_* constant
    _execute_entry() dispatches on. _execute_entry()'s option branch
    doesn't actually discriminate by ticker within that branch (it reads
    contract.ticker for the real order symbol), so any of the 4 constants
    would work mechanically — this just keeps the Action/outcome's own
    `.action` label semantically honest for logs and the dashboard."""
    if contract.option_type == "put":
        return BUY_UVXY_PUT if contract.ticker == "UVXY" else BUY_VXX_PUT
    return BUY_UVXY_CALL if contract.ticker == "UVXY" else BUY_VXX_CALL


def _execute_roll(
    rh, action: Action, account_number: str, spot_price: float | None, dry_run: bool = False,
) -> list[ExecutionOutcome]:
    """
    Roll = close the existing contract, then open a similar contract in a
    fresh DTE window at the same quantity (SRS §9: "+25% roll candidate").
    Two-leg, sequential, and conservative by construction:
      - The open leg is only attempted after the close leg is confirmed
        (live) or would-execute (dry-run) — never risks holding both the
        old and new contract at once.
      - If the close leg fails/is refused, stop — no open leg at all.
      - If the close leg goes through but no liquid replacement contract
        exists, stop — the position is now flat on this leg (not doubled,
        not stuck), and that is reported explicitly rather than silently.
      - Quantity is preserved 1:1 from the closed position — a roll
        refreshes an existing position, it does not resize it, so this
        does not re-run sleeve %/VIX_MAX_CONTRACTS sizing (a same-size
        replacement cannot increase net exposure beyond what was already
        held and already passed those checks on entry).
    Returns 1 outcome (close only) if the roll stops after leg 1, or 2
    outcomes (close + open) if it proceeds to leg 2.
    """
    pos = action.position
    if not pos:
        return [ExecutionOutcome(action, False, skip_reason="no position attached to roll action")]

    ticker = pos.get("ticker")
    option_type = pos.get("option_type")
    quantity = pos.get("contracts", 0)
    if not quantity or quantity <= 0:
        return [ExecutionOutcome(action, False, skip_reason="no contracts to roll")]

    close_action = Action(CLOSE_OPTION, ticker, action.reason, pos)
    close_outcome = _execute_flatten(rh, close_action, account_number, dry_run=dry_run)
    outcomes = [close_outcome]

    if not (close_outcome.executed or close_outcome.would_execute):
        return outcomes  # couldn't even preview/submit the close leg — stop here

    if close_outcome.executed and not dry_run:
        confirmed = _poll_order_confirmed(rh, close_outcome.order_id, is_option=True)
        if not confirmed:
            outcomes[0].skip_reason = "roll close leg unconfirmed — stopping before opening the new leg"
            return outcomes

    if option_type == "put":
        if spot_price is None or spot_price <= 0:
            outcomes.append(ExecutionOutcome(action, False, skip_reason=f"closed old contract, but no spot price for {ticker} to pick a replacement put"))
            return outcomes
        other_ticker = "VXX" if ticker == "UVXY" else "UVXY"
        new_contract = vix_options.pick_put(spot_price=spot_price, primary_ticker=ticker, fallback_ticker=other_ticker)
    elif option_type == "call":
        new_contract = vix_options.pick_call(ticker=ticker)
    else:
        outcomes.append(ExecutionOutcome(action, False, skip_reason=f"closed old contract, but unrecognized option_type {option_type!r} to roll into"))
        return outcomes

    if new_contract is None:
        outcomes.append(ExecutionOutcome(
            action, False,
            skip_reason="closed old contract, but found no liquid replacement — sleeve is now flat on this leg, not doubled",
        ))
        return outcomes

    if new_contract.mid <= 0:
        # Same "no real market" edge case _size_and_explain_option() guards
        # on the regular entry path (hit live 2026-08-19 on a thin VXX put)
        # — the roll bypasses that function entirely (quantity is preserved
        # 1:1, not re-sized), so it needs its own check here or it would
        # submit a limit order at price=0.0.
        outcomes.append(ExecutionOutcome(
            action, False,
            skip_reason=(
                f"closed old contract, but the replacement {new_contract.ticker} {new_contract.expiry} "
                f"{new_contract.strike}{new_contract.option_type[0]} has no real market "
                f"(bid=${new_contract.bid:.2f}, ask=${new_contract.ask:.2f}) — sleeve is now flat on this "
                f"leg, not doubled and not stuck holding a worthless order"
            ),
        ))
        return outcomes

    open_action = Action(_roll_entry_action_for(new_contract), new_contract.ticker, action.reason)
    open_outcome = _execute_entry(
        rh, open_action, account_number, spot_price,
        quantity=quantity, contract=new_contract, dry_run=dry_run,
    )
    outcomes.append(open_outcome)
    return outcomes
