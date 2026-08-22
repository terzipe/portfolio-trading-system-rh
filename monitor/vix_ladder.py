"""
VIX Trader BOT — SVIX ladder strategy (replaces the old contango-carry
SVIX_ON/FLATTEN_SVIX posture entirely; see
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md for the full spec this implements).

Unlike the posture engine (a fresh, memoryless decision every cycle), this
is a genuinely stateful, multi-cycle campaign: scale into SVIX in $5,000
tranches at VIX rungs of 30, 40, 50, ... as VIX climbs, stop scaling and
start taking profit in quarters once a 3% pullback from the campaign high
confirms a peak, and — the key fix for whipsaw price action — resume
buying new (higher) rungs if VIX later makes a fresh campaign high, using
budget freed up by any earlier take-profit sells. State persists across
loop cycles/days in VIX_SVIX_LADDER_STATE_FILE.

State-mutation split, deliberately:
  - evaluate() persists two kinds of "pure observation" updates directly,
    with no execution risk: tracking campaign_peak_vix as new highs are
    observed, and self-healing back to idle if real fetched positions show
    zero SVIX held while the campaign thinks it's still open (e.g. after a
    manual "Flatten SVIX now" click, or a trade placed directly in Alpaca).
  - Trade-related state (a rung actually bought, a take-profit step actually
    taken) is only finalized into open_lots/rung_levels_bought once a real
    fill is confirmed via reconcile_pending_orders() — never on order
    *submission*. This closes a real bug hit live 2026-08-21: recording a
    rung as bought right after submission (order accepted, not yet filled)
    left a window — order submitted after-hours, sitting queued — where a
    scheduled cycle ran, saw the real broker position still at zero SVIX
    (not filled yet) while state said shares existed, and the self-heal
    logic (below) misread that as a stale record and reset the whole
    campaign, silently forgetting about a real position that filled minutes
    later. record_rung_submitted()/record_takeprofit_submitted() track an
    order as *pending* (in state["pending_orders"], separate from
    open_lots/rung_levels_bought, so self-heal and _next_unbought_rung()
    both correctly ignore it until it's real) — reconcile_pending_orders()
    is what promotes a pending order to a real holding once Alpaca actually
    reports it filled, or drops it if rejected/canceled/expired.

Known limitation: self-heal reconciliation only handles the "real shares
are zero but state thinks otherwise" case. A partial out-of-band sell
(some, not all, shares sold manually) is not detected/reconciled — out of
scope for now.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from config import (
    VIX_SVIX_LADDER_STATE_FILE,
    VIX_LADDER_ARM_LEVEL,
    VIX_LADDER_MAX_ARM_LEVEL,
    VIX_LADDER_RUNG_STEP,
    VIX_LADDER_RUNG_DOLLARS,
    VIX_LADDER_BUDGET_PCT,
    VIX_LADDER_PULLBACK_PCT,
    VIX_LADDER_TP_STEPS,
)
from monitor.vix_signals import Action, BUY_SVIX_RUNG, SELL_SVIX_PARTIAL


def _default_state() -> dict:
    return {
        "armed": False,
        "campaign_peak_vix": None,
        "campaign_peak_shares": 0.0,
        "rung_levels_bought": [],
        "open_lots": [],  # FIFO: [{"level","qty_remaining","price","at"}]
        "take_profit_steps_done": 0,
        "pending_orders": [],  # [{"kind":"buy"|"sell","order_id",...}] -- submitted, not yet confirmed filled
    }


def _load_state() -> dict:
    if not VIX_SVIX_LADDER_STATE_FILE.exists():
        return _default_state()
    try:
        state = json.loads(VIX_SVIX_LADDER_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_state()
    return {**_default_state(), **state}


def _save_state(state: dict) -> None:
    VIX_SVIX_LADDER_STATE_FILE.write_text(json.dumps(state, indent=2))


def _current_shares(state: dict) -> float:
    return sum(lot["qty_remaining"] for lot in state["open_lots"])


def _current_cost_basis(state: dict) -> float:
    return sum(lot["qty_remaining"] * lot["price"] for lot in state["open_lots"])


def _avg_cost_per_share(state: dict) -> float | None:
    shares = _current_shares(state)
    if shares <= 0:
        return None
    return _current_cost_basis(state) / shares


def _held_svix_shares(positions: list[dict]) -> dict | None:
    for p in positions:
        if p.get("ticker") == "SVIX" and p.get("type") == "share":
            return p
    return None


def _next_unbought_rung(state: dict, vix: float) -> float | None:
    # Excludes rungs with a pending (submitted-but-unconfirmed) buy too --
    # otherwise a second cycle running before the first order's fill
    # confirms would submit a duplicate buy at the same level.
    #
    # Tail guard: rungs above VIX_LADDER_MAX_ARM_LEVEL are never eligible, so
    # a VIX blowout into the extreme tail (70+) stops adding new risk. The
    # ceiling is on the rung level, not a one-way latch -- if VIX later rounds
    # back down under the ceiling, rungs at/below it that were skipped on the
    # way up become eligible again (resume-on-the-way-down, per design).
    level = VIX_LADDER_ARM_LEVEL
    bought = set(state["rung_levels_bought"])
    bought |= {p["level"] for p in state["pending_orders"] if p["kind"] == "buy"}
    ceiling = min(vix, VIX_LADDER_MAX_ARM_LEVEL)
    while level <= ceiling:
        if level not in bought:
            return level
        level += VIX_LADDER_RUNG_STEP
    return None


def arm_campaign(vix: float) -> None:
    state = _default_state()
    state["armed"] = True
    state["campaign_peak_vix"] = vix
    _save_state(state)


def record_rung_bought(level: float, qty: float, price: float) -> None:
    """Finalizes a CONFIRMED fill into real state. Not called directly by
    the loop scripts anymore — only from reconcile_pending_orders() once a
    pending buy is actually confirmed filled by Alpaca. Kept as its own
    function (rather than inlined) since it's independently unit-tested and
    is also the natural "finalize" step reconcile_pending_orders() calls."""
    state = _load_state()
    state["rung_levels_bought"].append(level)
    state["open_lots"].append({
        "level": level, "qty_remaining": qty, "price": price,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    state["campaign_peak_shares"] += qty
    state["take_profit_steps_done"] = 0  # restart-on-rearm, confirmed 2026-08-20
    _save_state(state)


def record_take_profit_step(step_n: int, sell_qty: float) -> None:
    """Finalizes a CONFIRMED take-profit fill. Same note as
    record_rung_bought() above — called from reconcile_pending_orders()
    once confirmed, not directly by the loop scripts. Consumes sell_qty
    from open_lots FIFO (earliest/cheapest rungs first, matching the
    codebase's existing FIFO convention — see
    vix_ledger.fifo_realized_pnl()). campaign_peak_shares is a permanent
    high-water mark and is not decremented here."""
    state = _load_state()
    remaining_to_sell = sell_qty
    new_lots = []
    for lot in state["open_lots"]:
        if remaining_to_sell <= 1e-9:
            new_lots.append(lot)
            continue
        take = min(remaining_to_sell, lot["qty_remaining"])
        lot["qty_remaining"] -= take
        remaining_to_sell -= take
        if lot["qty_remaining"] > 1e-9:
            new_lots.append(lot)
    state["open_lots"] = new_lots
    state["take_profit_steps_done"] = step_n
    _save_state(state)


def record_rung_submitted(level: float, order_id: str, target_dollars: float) -> None:
    """Called by the loop scripts right after execute_actions() confirms a
    BUY_SVIX_RUNG order was *accepted* by Alpaca — not yet a real fill.
    Tracked in pending_orders, separate from open_lots/rung_levels_bought,
    so it can't be mistaken for a real holding (by self-heal) or leave a
    rung level open to a duplicate buy (by _next_unbought_rung()) while the
    fill is still in flight."""
    state = _load_state()
    state["pending_orders"].append({
        "kind": "buy", "level": level, "order_id": order_id, "target_dollars": target_dollars,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state)


def record_takeprofit_submitted(step_n: int, order_id: str, sell_quantity: float) -> None:
    """Same as record_rung_submitted(), for the SELL_SVIX_PARTIAL side."""
    state = _load_state()
    state["pending_orders"].append({
        "kind": "sell", "step": step_n, "order_id": order_id, "sell_quantity": sell_quantity,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state)


def reconcile_pending_orders(client) -> None:
    """Call once per real (non-dry-run) cycle, before evaluate(). Checks
    every pending order's real status and finalizes it (via
    record_rung_bought()/record_take_profit_step()) once Alpaca actually
    reports a fill, or drops it if rejected/canceled/expired. This is what
    makes a real fill visible to the campaign at all — closes the race
    condition described in the module docstring, hit live 2026-08-21."""
    state = _load_state()
    if not state["pending_orders"]:
        return

    still_pending = []
    for pending in state["pending_orders"]:
        try:
            order = client.get_order_by_id(pending["order_id"])
        except Exception:  # noqa: BLE001
            still_pending.append(pending)
            continue

        status = getattr(order, "status", None)
        status_value = getattr(status, "value", None)
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)

        if status_value == "filled" and filled_qty > 0:
            filled_price = getattr(order, "filled_avg_price", None)
            if pending["kind"] == "buy":
                price = float(filled_price) if filled_price else pending["target_dollars"] / filled_qty
                record_rung_bought(pending["level"], filled_qty, price)
            else:
                record_take_profit_step(pending["step"], filled_qty)
        elif status_value in ("rejected", "canceled", "expired"):
            pass  # dropped -- never finalized, nothing to undo
        else:
            still_pending.append(pending)

    state = _load_state()  # re-load: record_*() above may have just written
    state["pending_orders"] = still_pending
    _save_state(state)


def reset_campaign() -> None:
    _save_state(_default_state())


def get_status() -> dict:
    state = _load_state()
    return {
        "armed": state["armed"],
        "campaign_peak_vix": state["campaign_peak_vix"],
        "campaign_peak_shares": state["campaign_peak_shares"],
        "rung_levels_bought": state["rung_levels_bought"],
        "current_shares": _current_shares(state),
        "current_cost_basis": _current_cost_basis(state),
        "take_profit_steps_done": state["take_profit_steps_done"],
        "pending_orders": state["pending_orders"],
    }


def evaluate(
    vix: float | None, nav: float, positions: list[dict], live_price: float | None,
    dry_run: bool = False,
) -> list[Action]:
    """
    What should happen this cycle, given the current market and persisted
    campaign state. Returns at most one candidate action (one rung buy, or
    one take-profit sell) — never both, and never more than one buy per
    cycle even across a VIX gap, so a single cycle can't fire off multiple
    market orders at once (mirrors execute_actions()'s own per-cycle order
    cap elsewhere in this codebase).

    dry_run=True previews the same decision (reading real state) but skips
    every persist — arming, peak-VIX tracking, and the self-heal reset all
    stay in-memory only for this call, so a --dry-run drill can't arm a
    real campaign or nudge real state, matching how execute_actions()'s own
    dry_run already guarantees no trace (see loop_daily_vix.py/
    loop_intraday_vix.py — a real bug caught during the auto-kill-switch
    work: without this, a dry-run cycle that happened to see VIX>30 would
    have armed a real campaign).
    """
    if vix is None:
        return []

    state = _load_state()
    held = _held_svix_shares(positions)
    held_qty = held.get("quantity", 0.0) if held else 0.0

    # Self-heal: real position is flat but state thinks otherwise (e.g. a
    # manual flatten happened out of band). Reset before doing anything else.
    if state["armed"] and held_qty <= 0 and _current_shares(state) > 0:
        if not dry_run:
            reset_campaign()
            state = _load_state()
        else:
            state = _default_state()

    if not state["armed"]:
        if vix > VIX_LADDER_ARM_LEVEL:
            if not dry_run:
                arm_campaign(vix)
                state = _load_state()
            else:
                state = {**_default_state(), "armed": True, "campaign_peak_vix": vix}
        else:
            return []

    # Track the campaign's running VIX high — a pure observation, safe to
    # persist immediately regardless of what (if anything) gets traded —
    # but still skipped in dry_run, which must leave no trace at all.
    if state["campaign_peak_vix"] is None or vix > state["campaign_peak_vix"]:
        state["campaign_peak_vix"] = vix
        if not dry_run:
            _save_state(state)

    peak_vix = state["campaign_peak_vix"]
    pulled_back = vix <= peak_vix * (1 - VIX_LADDER_PULLBACK_PCT)

    if not pulled_back:
        next_rung = _next_unbought_rung(state, vix)
        if next_rung is None:
            return []
        budget_remaining = VIX_LADDER_BUDGET_PCT * nav - _current_cost_basis(state)
        if budget_remaining <= 0:
            return []
        target_dollars = min(VIX_LADDER_RUNG_DOLLARS, budget_remaining)
        return [Action(
            BUY_SVIX_RUNG, "SVIX",
            f"ladder rung {next_rung:.0f}: VIX={vix:.2f}, target=${target_dollars:.2f}",
            {"target_dollars": target_dollars, "rung_level": next_rung},
        )]

    # Pulled back — evaluate take-profit steps against currently-held shares.
    if live_price is None or live_price <= 0:
        return []
    avg_cost = _avg_cost_per_share(state)
    if avg_cost is None:
        return []
    pnl_pct = (live_price - avg_cost) / avg_cost
    done = state["take_profit_steps_done"]
    if done >= len(VIX_LADDER_TP_STEPS):
        return []
    threshold = VIX_LADDER_TP_STEPS[done]
    if pnl_pct < threshold:
        return []

    is_final_step = done == len(VIX_LADDER_TP_STEPS) - 1
    if is_final_step:
        sell_qty = _current_shares(state)  # close out cleanly, no rounding remainder left stuck
    else:
        sell_qty = int(0.25 * state["campaign_peak_shares"])
        sell_qty = min(sell_qty, _current_shares(state))
    if sell_qty <= 0:
        return []

    return [Action(
        SELL_SVIX_PARTIAL, "SVIX",
        f"take-profit step {done + 1}/{len(VIX_LADDER_TP_STEPS)}: pnl={pnl_pct:.0%} >= {threshold:.0%}",
        {"sell_quantity": sell_qty, "step": done + 1},
    )]
