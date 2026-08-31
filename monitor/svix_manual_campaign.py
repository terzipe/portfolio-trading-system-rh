"""
VIX Trader BOT — SVIX manual buy-below-$20 campaign. A SECOND, independent
SVIX campaign alongside monitor/vix_ladder.py: that one buys SVIX at
high-VIX percentile rungs on the way down after a spike; this one buys at
low, literal SVIX price levels ($20, $18, $16, ... in $2 decrements, see
config.SVIX_MANUAL_RUNGS) and holds through calm regimes — the opposite
entry regime, so it needs its own exit discipline
(monitor/vix_leading_signals.py) instead of the ladder's budget-cap-only
risk control. Confirmed design, 2026-08-29 (see project_svix_manual_
strategy memory / the approved implementation plan).

Both campaigns trade the same ticker (SVIX) in the same Alpaca account.
Alpaca reports one aggregate SVIX position for the whole account — it has
no concept of "which campaign owns which shares." Every order this module
submits is sized off its OWN ledger (open_lots below), never off
monitor.vix_positions.fetch_positions()'s account-wide SVIX quantity.

State is fully isolated from vix_ladder.py's state file AND from
monitor.vix_executor.execute_actions()'s shared data/vix/state.json
whipsaw-guard: that file's last_entry_session/last_flatten_session flags
are load-bearing for the OPTIONS sleeve's one-entry-per-day lock, and every
flatten action routed through execute_actions() unconditionally writes into
it regardless of which strategy triggered the flatten. This module never
calls execute_actions() — it calls monitor.vix_executor._submit_market_
order() directly (already ticker/qty-explicit, safe to reuse unmodified),
so it never touches that shared file.

Entry is manual only (dashboard "Buy SVIX now" button, see
regime_trader/dashboard/app.py, via submit_entry()) — there is no auto-buy
path here. Exit is automatic (run_exit_cycle(), called from the fast
exit-poll loop), driven by vix_leading_signals.evaluate()'s exit_level:
  1 — arm a WIDE resting stop at current_price * (1 - SVIX_MANUAL_TIER1_
      STOP_PCT) -- tier 1 (divergence) is the earliest/least-confirmed
      signal, wired live 2026-08-29, so it gets the loosest response.
  2 — arm/TIGHTEN the resting stop to current_price * (1 -
      SVIX_MANUAL_STOP_PCT), closer than level 1's.
  3+ — full flatten immediately, regardless of price or stop state.
A stop only ever ratchets TIGHTER (higher price, closer to current price)
-- a fresh level-1 candidate never overrides an already-tighter level-2
stop, and price falling further naturally produces a tighter candidate
even at the same level, so protection only ever firms up, never relaxes.
Once armed (level 1 or 2), the stop is checked against live price every
cycle regardless of the CURRENT exit_level -- a stop armed on a signal
that's since gone quiet still protects the position.

Retry-until-flat: a flatten order that comes back rejected/canceled/expired
is dropped from pending_orders (shares are still open, untouched) rather
than retried in place — the NEXT run_exit_cycle() call sees shares > 0 with
no pending sell and immediately resubmits. Every resubmission goes through
the one submit_flatten() path this way, rather than a second retry
code path. "Must be quick and keep trying until all positions are exited"
is a firm requirement (his words); no retry-on-rejection logic existed
anywhere in this codebase before this module — vix_ladder.
reconcile_pending_orders() drops a rejected order without resubmitting,
which is correct for a discretionary profit-take sell but not for a
risk-driven flatten.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide

from config import (
    SVIX_MANUAL_STATE_FILE,
    SVIX_MANUAL_RUNGS,
    SVIX_MANUAL_RUNG_DOLLARS,
    SVIX_MANUAL_BUDGET_DOLLARS,
    SVIX_MANUAL_STOP_PCT,
    SVIX_MANUAL_TIER1_STOP_PCT,
)
from monitor.vix_executor import _submit_market_order
from monitor import vix_ladder  # self_heal() only -- reads the ladder's tracked qty, read-only, no mutation


def _default_state() -> dict:
    return {
        "open_lots": [],            # FIFO: [{"rung", "qty_remaining", "price", "at"}]
        "rungs_fired": [],          # rung prices actually bought or pending-bought (this campaign)
        "last_alerted_rung": None,  # alert dedup only -- does NOT gate buying
        "armed_stop": None,         # price level; flatten triggers if live price <= this
        "pending_orders": [],       # [{"kind":"buy"|"sell","order_id","qty",...}] -- submitted, not yet confirmed
    }


def _load_state() -> dict:
    if not SVIX_MANUAL_STATE_FILE.exists():
        return _default_state()
    try:
        state = json.loads(SVIX_MANUAL_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_state()
    return {**_default_state(), **state}


def _save_state(state: dict) -> None:
    SVIX_MANUAL_STATE_FILE.write_text(json.dumps(state, indent=2))


def _current_shares(state: dict) -> float:
    return sum(lot["qty_remaining"] for lot in state["open_lots"])


def _current_cost_basis(state: dict) -> float:
    return sum(lot["qty_remaining"] * lot["price"] for lot in state["open_lots"])


def _avg_cost_per_share(state: dict) -> float | None:
    shares = _current_shares(state)
    if shares <= 0:
        return None
    return _current_cost_basis(state) / shares


def next_entry_rung(state: dict, price: float) -> float | None:
    """Lowest (least extreme, i.e. highest price) unfired rung whose level
    `price` has crossed (price <= rung), or None. Mirrors
    vix_ladder._next_unbought_rung()'s "lowest-unfired-first" semantics,
    keyed on descending price instead of ascending percentile — a VIX/SVIX
    gap straight through several levels still only offers the first
    (highest-price) unfired rung, matching the ladder's one-rung-at-a-time
    convention."""
    fired = set(state["rungs_fired"])
    fired |= {p["rung"] for p in state["pending_orders"] if p["kind"] == "buy"}
    for rung in sorted(SVIX_MANUAL_RUNGS, reverse=True):
        if rung in fired:
            continue
        if price <= rung:
            return rung
        return None  # descending order: no lower rung is reached either
    return None


def _deepest_crossed_rung(price: float) -> float | None:
    """All configured rungs price has fallen through (price <= rung),
    returns the DEEPEST (lowest-price) one -- independent of rungs_fired/
    pending state. Deliberately NOT the same lookup as next_entry_rung():
    that function only ever offers the shallowest UNBOUGHT rung (so buying
    always fills in order, even after a gap) and would keep returning $20
    forever if nothing's been bought yet, no matter how far price falls --
    correct for sizing a buy, wrong for alerting a human who's DCA-ing
    manually and wants to know about every new depth reached, not just the
    first one."""
    crossed = [r for r in SVIX_MANUAL_RUNGS if price <= r]
    return min(crossed) if crossed else None


def check_entry_alert(price: float) -> float | None:
    """Called once per cycle by loop_intraday_vix.py. Returns a rung price
    to alert on if price has reached a NEW DEPTH since the last alert
    (i.e., a lower rung than any previously alerted), else None. Does NOT
    mutate rungs_fired (that's reserved for actual buys) -- only updates
    last_alerted_rung, so alerting is deduplicated without consuming a rung
    a human hasn't actually bought yet.

    Known limitation: this tracks depth, not distinct crossing EVENTS -- if
    price recovers above a rung and later falls back through the SAME rung
    again (without a full campaign exit resetting last_alerted_rung in
    between), it will NOT re-alert, since that's not a new depth. Re-
    alerting on every whipsaw would reintroduce the alert-spam this
    mechanism exists to avoid."""
    state = _load_state()
    rung = _deepest_crossed_rung(price)
    prior = state.get("last_alerted_rung")
    if rung is None or (prior is not None and rung >= prior):
        return None
    state["last_alerted_rung"] = rung
    _save_state(state)
    return rung


def remaining_budget(state: dict | None = None) -> float:
    state = state if state is not None else _load_state()
    return max(0.0, SVIX_MANUAL_BUDGET_DOLLARS - _current_cost_basis(state))


def get_status() -> dict:
    state = _load_state()
    return {
        "current_shares": _current_shares(state),
        "current_cost_basis": _current_cost_basis(state),
        "rungs_fired": state["rungs_fired"],
        "last_alerted_rung": state["last_alerted_rung"],
        "armed_stop": state["armed_stop"],
        "pending_orders": state["pending_orders"],
        "budget_remaining": remaining_budget(state),
        "budget_total": SVIX_MANUAL_BUDGET_DOLLARS,
    }


def self_heal(real_positions: list[dict]) -> bool:
    """Mirrors vix_ladder.py's self-heal #1: if this campaign's own ledger
    thinks it holds shares (open_lots non-empty) but the REAL SVIX position
    -- after excluding whatever the ladder itself owns -- is flat, something
    external sold this campaign's shares. Resets to idle (clears open_lots,
    armed_stop, rungs_fired, last_alerted_rung, same as a normal full exit)
    rather than silently drifting from reality.

    Found necessary 2026-08-31: the pre-fix "Flatten SVIX now" button sold
    this campaign's shares (it operated on the account's raw aggregate SVIX
    position, not scoped to the ladder) before the isolation fix landed --
    this module had nothing to catch the resulting ledger/reality drift.

    `real_positions` should be the account's live fetched positions (same
    shape vix_ladder.evaluate() takes -- e.g. from
    monitor.vix_positions.fetch_positions()). Uses vix_ladder.
    exclude_other_campaign_shares() in the mirror-image direction from how
    the ladder uses it: excluding the LADDER's tracked qty from the real
    position to estimate what's actually this campaign's. Returns True if a
    reset happened."""
    state = _load_state()
    tracked_qty = _current_shares(state)
    if tracked_qty <= 0:
        return False
    ladder_qty = vix_ladder.get_status()["current_shares"]
    adjusted = vix_ladder.exclude_other_campaign_shares(real_positions, ladder_qty)
    real_qty = next(
        (p.get("quantity", 0) for p in adjusted if p.get("ticker") == "SVIX" and p.get("type") == "share"), 0
    )
    if real_qty <= 0:
        _save_state(_default_state())
        return True
    return False


def submit_entry(
    client, price: float, target_dollars: float | None = None, qty_override: int | None = None,
) -> dict:
    """Called from the dashboard's "Buy SVIX now" button. Sizes off this
    campaign's OWN remaining budget (never Alpaca's aggregate SVIX
    position), submits a market buy via vix_executor's low-level order
    primitive (bypassing execute_actions()'s shared-state writes), and
    records the fill as pending until reconcile_pending_orders() confirms
    it. Returns a dict with executed/order_id/skip_reason for the caller
    (the dashboard button) to render.

    `qty_override`, when given, is an explicit share count from the
    dashboard's editable quantity field (added 2026-08-31 -- the button
    previously always used SVIX_MANUAL_RUNG_DOLLARS regardless of what the
    user wanted to buy) -- takes priority over target_dollars/the rung-
    dollar default, still budget-checked (refused outright if it exceeds
    remaining budget, never silently sized down, since a human explicitly
    chose this quantity)."""
    state = _load_state()
    rung = next_entry_rung(state, price)
    budget = remaining_budget(state)
    if budget <= 0:
        return {"executed": False, "skip_reason": f"budget exhausted (${SVIX_MANUAL_BUDGET_DOLLARS:.0f} cap)"}

    if qty_override is not None:
        if qty_override <= 0:
            return {"executed": False, "skip_reason": "quantity must be positive"}
        dollars = qty_override * price
        if dollars > budget:
            return {"executed": False, "skip_reason": f"${dollars:.2f} ({qty_override} sh @ ${price:.2f}) exceeds remaining budget (${budget:.2f})"}
        qty = qty_override
    else:
        dollars = min(target_dollars or SVIX_MANUAL_RUNG_DOLLARS, budget)
        qty = int(dollars // price) if price > 0 else 0
        if qty <= 0:
            return {"executed": False, "skip_reason": f"sized to 0 shares -- ${dollars:.2f} at ${price:.2f}/share rounds down to 0"}

    try:
        order = _submit_market_order(client, "SVIX", qty, OrderSide.BUY)
    except Exception as exc:
        return {"executed": False, "skip_reason": f"order failed: {exc}"}
    state["pending_orders"].append({
        "kind": "buy", "rung": rung, "order_id": str(order.id), "qty": qty,
        "target_dollars": dollars, "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    if rung is not None:
        state["rungs_fired"].append(rung)
    _save_state(state)
    return {"executed": True, "order_id": str(order.id), "qty": qty}


def submit_flatten(client, qty: float) -> dict:
    """Sells `qty` (this campaign's own ledger qty, never Alpaca's
    aggregate SVIX position) as a market order, bypassing
    vix_executor.execute_actions()'s shared-state wrapper exactly like
    submit_entry() does."""
    try:
        order = _submit_market_order(client, "SVIX", qty, OrderSide.SELL)
    except Exception as exc:
        return {"executed": False, "skip_reason": f"order failed: {exc}"}
    state = _load_state()
    state["pending_orders"].append({
        "kind": "sell", "order_id": str(order.id), "qty": qty,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state)
    return {"executed": True, "order_id": str(order.id), "qty": qty}


def _record_lot(rung: float | None, qty: float, price: float) -> None:
    state = _load_state()
    state["open_lots"].append({
        "rung": rung, "qty_remaining": qty, "price": price,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state)


def _consume_lots(qty: float) -> None:
    """FIFO consume (earliest/cheapest rungs first, matching this
    codebase's existing convention elsewhere -- see vix_ladder.
    record_take_profit_step()). On reaching fully flat, resets armed_stop/
    rungs_fired/last_alerted_rung so a fresh $20 crossing later starts a
    clean new campaign -- mirrors vix_ladder's own self-heal-on-full-exit
    fix."""
    state = _load_state()
    remaining = qty
    new_lots = []
    for lot in state["open_lots"]:
        if remaining <= 1e-9:
            new_lots.append(lot)
            continue
        take = min(remaining, lot["qty_remaining"])
        lot["qty_remaining"] -= take
        remaining -= take
        if lot["qty_remaining"] > 1e-9:
            new_lots.append(lot)
    state["open_lots"] = new_lots
    if not new_lots:
        state["armed_stop"] = None
        state["rungs_fired"] = []
        state["last_alerted_rung"] = None
    _save_state(state)


def reconcile_pending_orders(client) -> None:
    """Call once per real (non-dry-run) fast-loop cycle, before evaluating
    the exit response (mirrors vix_ladder.reconcile_pending_orders()'s
    submit-then-confirm pattern). See module docstring for why a rejected
    SELL is dropped here rather than retried in place -- the retry happens
    on the next run_exit_cycle() call instead."""
    state = _load_state()
    if not state["pending_orders"]:
        return

    still_pending = []
    for pending in state["pending_orders"]:
        try:
            order = client.get_order_by_id(pending["order_id"])
        except Exception:
            still_pending.append(pending)
            continue

        status = getattr(order, "status", None)
        status_value = getattr(status, "value", None)
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)

        if status_value == "filled" and filled_qty > 0:
            if pending["kind"] == "buy":
                filled_price = getattr(order, "filled_avg_price", None)
                price = float(filled_price) if filled_price else pending["target_dollars"] / filled_qty
                _record_lot(pending.get("rung"), filled_qty, price)
            else:
                _consume_lots(filled_qty)
        elif status_value in ("rejected", "canceled", "expired"):
            pass  # dropped -- shares (if a sell) stay open, next cycle resubmits; see docstring
        else:
            still_pending.append(pending)

    state = _load_state()  # re-load: _record_lot()/_consume_lots() above may have just written
    state["pending_orders"] = still_pending
    _save_state(state)


def run_exit_cycle(client, live_price: float, signal_result) -> dict:
    """Called once per fast-loop cycle (loop_svix_exit_monitor.py) while
    this campaign might hold a position. Reconciles pending orders first,
    then evaluates the tiered exit response against `signal_result`
    (a monitor.vix_leading_signals.LeadingSignalResult), then submits a
    flatten if warranted and none is already in flight. `dry_run` is
    intentionally not offered here -- reconcile_pending_orders() only acts
    on orders this same module previously submitted for real, so there's
    nothing to preview; callers wanting a dry run should not call this at
    all (see loop_svix_exit_monitor.py's --dry-run handling).

    Returns a status dict for the caller's alerting/dashboard-cache logic:
    action is one of "none" (nothing to do), "armed" (a stop was just armed
    for the first time, level 1 or 2), "tightened" (an already-armed stop
    just ratcheted closer to price), "flatten_submitted",
    "flatten_in_progress" (a sell is already pending from a prior cycle),
    or "flatten_failed" (order submission itself raised -- see
    detail.skip_reason).
    """
    reconcile_pending_orders(client)
    state = _load_state()

    shares = _current_shares(state)
    has_pending_sell = any(p["kind"] == "sell" for p in state["pending_orders"])

    if shares <= 0:
        return {"action": "none", "shares_remaining": 0, "armed_stop": state["armed_stop"]}

    need_flatten = False
    stop_action = None

    if signal_result.exit_level >= 3:
        need_flatten = True
    else:
        stop_pct = {1: SVIX_MANUAL_TIER1_STOP_PCT, 2: SVIX_MANUAL_STOP_PCT}.get(signal_result.exit_level)
        if stop_pct is not None:
            candidate_stop = round(live_price * (1 - stop_pct), 4)
            current_stop = state["armed_stop"]
            # Ratchet only -- a candidate closer to live_price (higher)
            # replaces the armed stop; a looser candidate (e.g. a fresh
            # level-1 reading after level 2 already tightened it) never
            # widens it back out.
            if current_stop is None:
                state["armed_stop"] = candidate_stop
                _save_state(state)
                stop_action = "armed"
            elif candidate_stop > current_stop:
                state["armed_stop"] = candidate_stop
                _save_state(state)
                stop_action = "tightened"

        # Whether or not this cycle armed/tightened anything, any
        # already-armed stop (from this cycle or a prior one, on a signal
        # that may have since gone quiet) still protects the position.
        state = _load_state()
        if state["armed_stop"] is not None and live_price <= state["armed_stop"]:
            need_flatten = True

    if not need_flatten:
        if stop_action is not None:
            return {"action": stop_action, "shares_remaining": shares, "armed_stop": state["armed_stop"]}
        return {"action": "none", "shares_remaining": shares, "armed_stop": state["armed_stop"]}

    if has_pending_sell:
        return {"action": "flatten_in_progress", "shares_remaining": shares, "armed_stop": state["armed_stop"]}

    result = submit_flatten(client, shares)
    action = "flatten_submitted" if result.get("executed") else "flatten_failed"
    return {"action": action, "detail": result, "shares_remaining": shares, "armed_stop": state["armed_stop"]}
