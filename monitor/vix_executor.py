"""
VIX Trader BOT — order executor (SRS v1.4 §8.5, §9, Impl Plan §6), migrated
from Robinhood to Alpaca paper trading. Every order here is placed directly
on the already-authenticated `client` (an alpaca.trading.client.TradingClient)
that vix_session.assess() returns. Alpaca has no "AGENTIC"/"MARGIN"
sub-account split (one paper account per API key pair), so account routing
is gone — every RH account_number= kwarg is simply dropped.

Options are submitted via OCC symbol (e.g. "UVXY260115P00054000"), built
from the ContractPick/held-position's ticker+expiry+strike+option_type.
Alpaca's limit_price is dollars-per-share (standard options quoting, same
as contract.bid/ask from vix_options.py) — NOT the $/contract convention
vix_positions.py uses for cost_basis/mid_price, so closing/rolling a held
option position must divide by 100 before building the limit order.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime

from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from config import (
    VIX_STATE_FILE, VIX_SLEEVE_MAX_PCT, VIX_SVIX_MAX_PCT,
    VIX_MAX_CONTRACTS, VIX_KILL_SWITCH, ENABLE_VIX_AUTO_SELL, ENABLE_VIX_AUTO_BUY,
    ENABLE_VIX_AUTO_ROLL,
)
from monitor import vix_kill_switch, vix_options
from monitor.vix_session import SessionResult, HEALTHY, can_flatten, can_buy
from monitor.vix_signals import (
    Action, SELL_SVIX_ALL, BUY_SVIX_SHARES, BUY_UVXY_PUT, BUY_VXX_PUT,
    BUY_VXX_CALL, BUY_UVXY_CALL, ROLL_OPTION, CLOSE_OPTION, HOLD, NOOP,
)

_MAX_ORDERS_PER_CYCLE = 3
_TIME_IN_FORCE = TimeInForce.DAY
_CONFIRMED_STATUSES = {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW}
_TERMINAL_REJECTED_STATUSES = {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.STOPPED, OrderStatus.SUSPENDED}


@dataclass
class ExecutionOutcome:
    action: Action
    executed: bool                        # True only if a real order was submitted to Alpaca
    would_execute: bool = False           # True if every gating check passed (live or dry-run)
    order_preview: dict | None = None     # the exact call args that were/would be sent
    dry_run: bool = False
    order_id: str | None = None
    price: float | None = None
    skip_reason: str = ""


def _occ_symbol(ticker: str, expiry: str, strike: float, option_type: str) -> str:
    """Build an OCC-format option symbol, e.g. ('UVXY','2026-01-15',54.0,'put')
    -> 'UVXY260115P00054000'. Inverse of vix_positions._parse_occ_symbol."""
    exp_date = datetime.strptime(expiry, "%Y-%m-%d")
    date_part = exp_date.strftime("%y%m%d")
    cp = "C" if option_type == "call" else "P"
    strike_part = f"{round(strike * 1000):08d}"
    return f"{ticker}{date_part}{cp}{strike_part}"


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


def _submit_market_order(client, symbol: str, qty: float, side: OrderSide):
    request = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=_TIME_IN_FORCE)
    return client.submit_order(request)


def _submit_limit_order(client, symbol: str, qty: float, side: OrderSide, limit_price: float):
    request = LimitOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=_TIME_IN_FORCE, limit_price=limit_price)
    return client.submit_order(request)


def _poll_order_confirmed(client, order_id: str, timeout: float = 20.0) -> bool:
    """After every order, poll status; caller stops the cycle if unconfirmed
    (no second market order same cycle) — SRS §8.5. Alpaca uses one unified
    get_order_by_id() lookup for both equity and option orders, unlike RH's
    split get_stock_order_info/get_option_order_info."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            order = client.get_order_by_id(order_id) if order_id else None
        except Exception:  # noqa: BLE001
            order = None
        status = getattr(order, "status", None)
        if status in _CONFIRMED_STATUSES:
            return True
        if status in _TERMINAL_REJECTED_STATUSES:
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
    real market) get their own message: "exceeds sleeve headroom" would be
    actively misleading there, since more NAV wouldn't fix an untradeable
    quote.
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
    real inputs, but stops short of calling Alpaca's submit_order — see
    _execute_flatten/_execute_entry. Session-scoped state
    (last_entry_session, last_flatten_session) is tracked in-memory for the
    duration of this call so the caps behave identically to a live run, but
    is never persisted to VIX_STATE_FILE, so a dry run leaves no trace that
    could confuse a subsequent real run's re-entry lock.
    """
    outcomes: list[ExecutionOutcome] = []
    if session.client is None:
        for a in actions:
            outcomes.append(ExecutionOutcome(a, False, skip_reason=f"session not usable ({session.state})"))
        return outcomes

    client = session.client
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

        auto_tripped = vix_kill_switch.is_tripped()
        if (VIX_KILL_SWITCH or auto_tripped) and not is_flatten:
            reason = "VIX_KILL_SWITCH=true" if VIX_KILL_SWITCH else "auto kill switch tripped (SVIX P&L stop)"
            outcomes.append(ExecutionOutcome(action, False, skip_reason=f"{reason} — flatten-only"))
            continue

        if is_flatten:
            if not ENABLE_VIX_AUTO_SELL:
                outcomes.append(ExecutionOutcome(action, False, skip_reason="ENABLE_VIX_AUTO_SELL=false"))
                continue
            if not can_flatten(session):
                outcomes.append(ExecutionOutcome(action, False, skip_reason=f"session {session.state} cannot flatten"))
                continue

            outcome = _execute_flatten(client, action, dry_run=dry_run)
            outcomes.append(outcome)
            if outcome.executed or outcome.would_execute:
                orders_this_cycle += 1
                state["last_flatten_reason"] = action.reason
                state["last_flatten_session"] = _session_id()
                if not dry_run:
                    _save_state(state)
                if outcome.executed:
                    confirmed = _poll_order_confirmed(client, outcome.order_id)
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

            outcome = _execute_entry(client, action, price, quantity=quantity, contract=contract, dry_run=dry_run)
            outcomes.append(outcome)
            if outcome.executed or outcome.would_execute:
                orders_this_cycle += 1
                state["last_entry_session"] = _session_id()
                if not dry_run:
                    _save_state(state)
                if outcome.executed:
                    confirmed = _poll_order_confirmed(client, outcome.order_id)
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
            roll_outcomes = _execute_roll(client, action, roll_price, dry_run=dry_run)
            outcomes.extend(roll_outcomes)
            orders_this_cycle += sum(1 for o in roll_outcomes if o.executed or o.would_execute)

            last_leg = roll_outcomes[-1]
            if last_leg.executed:
                confirmed = _poll_order_confirmed(client, last_leg.order_id)
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


def _execute_flatten(client, action: Action, dry_run: bool = False) -> ExecutionOutcome:
    try:
        if action.ticker == "SVIX" and action.position and action.position.get("type") == "share":
            qty = action.position.get("quantity", 0)
            if qty <= 0:
                return ExecutionOutcome(action, False, skip_reason="no SVIX quantity to sell")
            preview = {
                "call": "submit_order", "order_type": "market", "symbol": action.ticker,
                "quantity": qty, "side": "sell", "time_in_force": "day",
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            order = _submit_market_order(client, action.ticker, qty, OrderSide.SELL)
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=str(order.id))

        if action.action == CLOSE_OPTION and action.position:
            pos = action.position
            occ = _occ_symbol(pos["ticker"], pos["expiry"], pos["strike"], pos["option_type"])
            # Re-quote right before submission and price at the live bid —
            # a limit built from pos["mid_price"] (captured whenever
            # fetch_positions() last ran, not at submission time) can sit
            # stale on a thin contract: observed live 2026-08-20, a real
            # 6x-wide $0.02/$0.14 spread left a mid-derived close order
            # unfilled. Falls back to the old stale-mark price only if a
            # fresh quote can't be fetched — closes/rolls stay exempt from
            # ever being flat-out refused, same reason they're exempt from
            # the kill switch.
            fresh_quote = vix_options.get_contract_quote(pos["ticker"], pos["expiry"], pos["strike"], pos["option_type"])
            if fresh_quote is not None and fresh_quote[0] > 0:
                limit_price = round(fresh_quote[0], 2)
            else:
                limit_price = round(pos.get("mid_price", pos.get("cost_basis", 0)) / 100, 2)
            qty = pos.get("contracts", 1)
            preview = {
                "call": "submit_order", "order_type": "limit", "symbol": occ,
                "quantity": qty, "side": "sell", "limit_price": limit_price, "time_in_force": "day",
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            order = _submit_limit_order(client, occ, qty, OrderSide.SELL, limit_price)
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=str(order.id))

        return ExecutionOutcome(action, False, skip_reason="no matching flatten handler")
    except Exception as exc:  # noqa: BLE001
        return ExecutionOutcome(action, False, skip_reason=f"order failed: {exc}")


def _execute_entry(
    client, action: Action, price: float | None,
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
                "call": "submit_order", "order_type": "market", "symbol": action.ticker,
                "quantity": quantity, "side": "buy", "time_in_force": "day",
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            order = _submit_market_order(client, action.ticker, quantity, OrderSide.BUY)
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=str(order.id))

        if action.action in (BUY_UVXY_PUT, BUY_VXX_PUT, BUY_VXX_CALL, BUY_UVXY_CALL):
            if contract is None or not quantity or quantity <= 0:
                # Caller is expected to have already refused with a detailed
                # reason (no liquid contract found, or sized to 0) before
                # reaching here — defensive fallback, not the primary path.
                return ExecutionOutcome(action, False, skip_reason="no contract/quantity to submit — refusing")
            # Pay the ask on a buy-to-open — a marketable limit, not a
            # market order (SRS: never 0-DTE / defined-risk options only,
            # a naked market order on an option is not that).
            occ = _occ_symbol(contract.ticker, contract.expiry, contract.strike, contract.option_type)
            preview = {
                "call": "submit_order", "order_type": "limit", "symbol": occ,
                "quantity": quantity, "side": "buy", "limit_price": contract.ask, "time_in_force": "day",
            }
            if dry_run:
                return ExecutionOutcome(
                    action, False, would_execute=True, order_preview=preview, dry_run=True,
                    skip_reason="DRY RUN — order not submitted",
                )
            order = _submit_limit_order(client, occ, quantity, OrderSide.BUY, contract.ask)
            return ExecutionOutcome(action, True, would_execute=True, order_preview=preview, order_id=str(order.id))

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
    client, action: Action, spot_price: float | None, dry_run: bool = False,
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
    close_outcome = _execute_flatten(client, close_action, dry_run=dry_run)
    outcomes = [close_outcome]

    if not (close_outcome.executed or close_outcome.would_execute):
        return outcomes  # couldn't even preview/submit the close leg — stop here

    if close_outcome.executed and not dry_run:
        confirmed = _poll_order_confirmed(client, close_outcome.order_id)
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
        # on the regular entry path — the roll bypasses that function
        # entirely (quantity is preserved 1:1, not re-sized), so it needs
        # its own check here or it would submit a limit order at price=0.0.
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
        client, open_action, spot_price,
        quantity=quantity, contract=new_contract, dry_run=dry_run,
    )
    outcomes.append(open_outcome)
    return outcomes
