"""
monitor/svix_manual_campaign.py — SVIX manual buy-below-$20 campaign state
machine. No network needed — everything is driven through a fake Alpaca
client (fake_client fixture below) against a persisted (here, tmp_path-
isolated) state file, same isolation pattern as test_vix_ladder.py.
"""
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide

from monitor import svix_manual_campaign as smc
from monitor import vix_leading_signals

FAKE_RUNGS = [20.0, 18.0, 16.0]


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_file = tmp_path / "svix_manual_state.json"
    monkeypatch.setattr(smc, "SVIX_MANUAL_STATE_FILE", state_file)
    monkeypatch.setattr(smc, "SVIX_MANUAL_RUNGS", FAKE_RUNGS)
    monkeypatch.setattr(smc, "SVIX_MANUAL_RUNG_DOLLARS", 3000)
    monkeypatch.setattr(smc, "SVIX_MANUAL_BUDGET_DOLLARS", 9000)  # 3 rungs @ $3k
    monkeypatch.setattr(smc, "SVIX_MANUAL_STOP_PCT", 0.05)
    monkeypatch.setattr(smc, "SVIX_MANUAL_TIER1_STOP_PCT", 0.10)
    return state_file


class FakeOrder:
    def __init__(self, order_id, status="filled", filled_qty=0, filled_avg_price=None):
        self.id = order_id
        self.status = SimpleNamespace(value=status)
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class FakeClient:
    """Mimics the subset of alpaca.trading.client.TradingClient that
    monitor.vix_executor._submit_market_order() and
    svix_manual_campaign.reconcile_pending_orders() call."""

    def __init__(self):
        self.submitted = []
        self._orders = {}
        self._next_id = 0

    def submit_order(self, request):
        self._next_id += 1
        order_id = f"order-{self._next_id}"
        self.submitted.append({"symbol": request.symbol, "qty": request.qty, "side": request.side})
        order = FakeOrder(order_id, status="accepted")
        self._orders[order_id] = order
        return order

    def set_status(self, order_id, status, filled_qty=0, filled_avg_price=None):
        self._orders[order_id].status = SimpleNamespace(value=status)
        self._orders[order_id].filled_qty = filled_qty
        self._orders[order_id].filled_avg_price = filled_avg_price

    def get_order_by_id(self, order_id):
        return self._orders[order_id]


def _fake_signal(exit_level, **overrides):
    defaults = dict(tier1_divergence=False, tier2_compression=False, tier3_term_structure=False,
                     tier4_skew_confirmer=False, tier3_confirmed_days=0, score=0, exit_level=exit_level, reasons=[])
    defaults.update(overrides)
    return vix_leading_signals.LeadingSignalResult(**defaults)


# ── next_entry_rung / check_entry_alert ─────────────────────────────────

def test_no_rung_when_price_above_highest_rung(isolated_state):
    state = smc._load_state()
    assert smc.next_entry_rung(state, 25.0) is None


def test_lowest_unfired_rung_returned_first(isolated_state):
    state = smc._load_state()
    assert smc.next_entry_rung(state, 19.0) == 20.0  # crossed $20, not yet $18/$16


def test_gap_through_multiple_rungs_still_returns_only_the_first_unfired(isolated_state):
    state = smc._load_state()
    assert smc.next_entry_rung(state, 10.0) == 20.0  # gapped straight to $10 -- still offers $20 first


def test_fired_rung_excluded(isolated_state):
    state = smc._load_state()
    state["rungs_fired"] = [20.0]
    assert smc.next_entry_rung(state, 15.0) == 18.0


def test_pending_buy_rung_excluded(isolated_state):
    state = smc._load_state()
    state["pending_orders"] = [{"kind": "buy", "rung": 20.0}]
    assert smc.next_entry_rung(state, 15.0) == 18.0


def test_check_entry_alert_fires_once_per_crossing(isolated_state):
    assert smc.check_entry_alert(19.5) == 20.0
    assert smc.check_entry_alert(19.4) is None  # same rung, already alerted -- no re-fire
    assert smc.check_entry_alert(17.0) == 18.0  # a NEW, deeper crossing does alert


def test_check_entry_alert_does_not_consume_rungs_fired(isolated_state):
    smc.check_entry_alert(19.5)
    state = smc._load_state()
    assert state["rungs_fired"] == []  # alert-only -- buying is still available


# ── submit_entry ─────────────────────────────────────────────────────────

def test_submit_entry_sizes_and_records_pending(isolated_state):
    client = FakeClient()
    result = smc.submit_entry(client, price=19.5)
    assert result["executed"] is True
    assert result["qty"] == int(3000 // 19.5)
    state = smc._load_state()
    assert len(state["pending_orders"]) == 1
    assert state["pending_orders"][0]["rung"] == 20.0
    assert state["rungs_fired"] == [20.0]


def test_submit_entry_refuses_when_budget_exhausted(isolated_state):
    client = FakeClient()
    state = smc._load_state()
    state["open_lots"] = [{"rung": 20.0, "qty_remaining": 100, "price": 90.0, "at": "x"}]  # cost basis $9000 == cap
    smc._save_state(state)
    result = smc.submit_entry(client, price=19.5)
    assert result["executed"] is False
    assert "budget exhausted" in result["skip_reason"]


def test_submit_entry_sizes_to_zero_shares_rejected(isolated_state):
    client = FakeClient()
    result = smc.submit_entry(client, price=999_999.0)  # dollars // price rounds to 0
    assert result["executed"] is False
    assert "0 shares" in result["skip_reason"]


def test_submit_entry_qty_override_uses_exact_quantity(isolated_state):
    client = FakeClient()
    result = smc.submit_entry(client, price=28.0, qty_override=107)
    assert result["executed"] is True
    assert result["qty"] == 107
    state = smc._load_state()
    assert state["pending_orders"][0]["qty"] == 107
    assert state["pending_orders"][0]["target_dollars"] == 107 * 28.0


def test_submit_entry_qty_override_refuses_when_it_exceeds_budget(isolated_state):
    client = FakeClient()
    result = smc.submit_entry(client, price=28.0, qty_override=1000)  # 1000*28=$28,000 >> $9000 cap
    assert result["executed"] is False
    assert "exceeds remaining budget" in result["skip_reason"]
    assert client.submitted == []  # never reached order submission


def test_submit_entry_qty_override_rejects_non_positive_quantity(isolated_state):
    client = FakeClient()
    result = smc.submit_entry(client, price=28.0, qty_override=0)
    assert result["executed"] is False
    assert "positive" in result["skip_reason"]


def test_submit_entry_qty_override_does_not_get_silently_resized(isolated_state):
    """An explicit human-chosen quantity is refused outright if it doesn't
    fit the budget -- never silently sized down to whatever DOES fit,
    unlike the default rung-dollar path's min(target_dollars, budget)."""
    client = FakeClient()
    state = smc._load_state()
    state["open_lots"] = [{"rung": 20.0, "qty_remaining": 100, "price": 88.0, "at": "x"}]  # cost basis $8800
    smc._save_state(state)  # remaining budget = $200
    result = smc.submit_entry(client, price=28.0, qty_override=50)  # 50*28=$1400 > $200 remaining
    assert result["executed"] is False
    assert client.submitted == []


# ── reconcile_pending_orders ─────────────────────────────────────────────

def test_reconcile_finalizes_filled_buy_into_open_lots(isolated_state):
    client = FakeClient()
    smc.submit_entry(client, price=20.0)
    order_id = smc._load_state()["pending_orders"][0]["order_id"]
    client.set_status(order_id, "filled", filled_qty=150, filled_avg_price=20.0)

    smc.reconcile_pending_orders(client)
    state = smc._load_state()
    assert state["pending_orders"] == []
    assert smc._current_shares(state) == 150


def test_reconcile_drops_rejected_buy(isolated_state):
    client = FakeClient()
    smc.submit_entry(client, price=20.0)
    order_id = smc._load_state()["pending_orders"][0]["order_id"]
    client.set_status(order_id, "rejected")

    smc.reconcile_pending_orders(client)
    state = smc._load_state()
    assert state["pending_orders"] == []
    assert smc._current_shares(state) == 0


def test_reconcile_leaves_shares_open_on_rejected_sell(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    result = smc.submit_flatten(client, 100)
    order_id = result["order_id"]
    client.set_status(order_id, "rejected")

    smc.reconcile_pending_orders(client)
    state = smc._load_state()
    assert state["pending_orders"] == []
    assert smc._current_shares(state) == 100  # still open -- next run_exit_cycle() will resubmit


def test_full_exit_resets_campaign_state(isolated_state):
    state = smc._load_state()
    state["rungs_fired"] = [20.0, 18.0]
    state["armed_stop"] = 19.0
    state["last_alerted_rung"] = 18.0
    smc._save_state(state)
    smc._record_lot(20.0, 100, 20.0)

    client = FakeClient()
    result = smc.submit_flatten(client, 100)
    client.set_status(result["order_id"], "filled", filled_qty=100, filled_avg_price=19.5)
    smc.reconcile_pending_orders(client)

    state = smc._load_state()
    assert smc._current_shares(state) == 0
    assert state["armed_stop"] is None
    assert state["rungs_fired"] == []
    assert state["last_alerted_rung"] is None


# ── self_heal() — isolation from vix_ladder.py, mirror image of
# vix_ladder.exclude_other_campaign_shares() (found 2026-08-31: the pre-fix
# "Flatten SVIX now" button sold this campaign's shares out from under it,
# with nothing here to catch the drift) ─────────────────────────────────

def _mock_ladder_shares(monkeypatch, qty):
    monkeypatch.setattr(smc.vix_ladder, "get_status", lambda: {"current_shares": qty})


def test_self_heal_noop_when_not_tracking_any_shares(isolated_state, monkeypatch):
    _mock_ladder_shares(monkeypatch, 0)
    assert smc.self_heal(real_positions=[]) is False


def test_self_heal_resets_when_real_position_flat(isolated_state, monkeypatch):
    smc._record_lot(20.0, 50, 20.0)
    _mock_ladder_shares(monkeypatch, 0)  # ladder holds nothing either
    real_positions = [{"ticker": "SVIX", "type": "share", "quantity": 0}]

    assert smc.self_heal(real_positions) is True
    state = smc._load_state()
    assert smc._current_shares(state) == 0


def test_self_heal_does_not_fire_when_own_shares_still_present(isolated_state, monkeypatch):
    """Campaign tracks 30, ladder separately tracks 20 -- real aggregate is
    50. After excluding the ladder's 20, this campaign's own 30 are still
    accounted for -- must NOT reset."""
    smc._record_lot(20.0, 30, 20.0)
    _mock_ladder_shares(monkeypatch, 20)
    real_positions = [{"ticker": "SVIX", "type": "share", "quantity": 50}]

    assert smc.self_heal(real_positions) is False
    state = smc._load_state()
    assert smc._current_shares(state) == 30  # untouched


def test_self_heal_fires_when_only_ladders_shares_remain(isolated_state, monkeypatch):
    """This campaign tracked 30 (externally sold), but the ladder still
    legitimately holds its own 20 -- real aggregate is 20, all of it the
    ladder's. Must correctly distinguish that from "still holding its own"
    and reset."""
    smc._record_lot(20.0, 30, 20.0)
    _mock_ladder_shares(monkeypatch, 20)
    real_positions = [{"ticker": "SVIX", "type": "share", "quantity": 20}]  # only the ladder's

    assert smc.self_heal(real_positions) is True
    state = smc._load_state()
    assert smc._current_shares(state) == 0


def test_self_heal_no_svix_entry_in_real_positions_treated_as_flat(isolated_state, monkeypatch):
    smc._record_lot(20.0, 30, 20.0)
    _mock_ladder_shares(monkeypatch, 0)
    assert smc.self_heal(real_positions=[{"ticker": "UVXY", "type": "option", "quantity": 1}]) is True


# ── run_exit_cycle ─────────────────────────────────────────────────────

def test_run_exit_cycle_noop_when_flat(isolated_state):
    client = FakeClient()
    result = smc.run_exit_cycle(client, 19.0, _fake_signal(exit_level=0))
    assert result["action"] == "none"
    assert result["shares_remaining"] == 0


def test_run_exit_cycle_arms_stop_on_tier2(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    result = smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=2))
    assert result["action"] == "armed"
    assert result["armed_stop"] == round(19.5 * 0.95, 4)
    assert client.submitted == []  # arming is not an order


def test_run_exit_cycle_does_not_rearm_once_stop_already_set(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=2))
    first_stop = smc._load_state()["armed_stop"]
    result = smc.run_exit_cycle(client, 19.0, _fake_signal(exit_level=2))
    assert result["action"] == "none"
    assert smc._load_state()["armed_stop"] == first_stop


def test_run_exit_cycle_arms_wide_stop_on_tier1(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    result = smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=1))
    assert result["action"] == "armed"
    assert result["armed_stop"] == round(19.5 * 0.90, 4)  # SVIX_MANUAL_TIER1_STOP_PCT=0.10, wider than tier2's 0.05
    assert client.submitted == []


def test_run_exit_cycle_tier2_tightens_an_existing_tier1_stop(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=1))  # arms wide: 17.55
    result = smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=2))  # tier 2 confirms -- tighten
    assert result["action"] == "tightened"
    assert result["armed_stop"] == round(19.5 * 0.95, 4)  # 18.525 -- tighter than 17.55


def test_run_exit_cycle_tier1_never_loosens_an_existing_tier2_stop(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=2))  # arms tight: 18.525
    result = smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=1))  # tier 2 quiets, only tier 1 confirms now
    assert result["action"] == "none"  # NOT "armed" or "tightened" -- 17.55 would be looser, rejected
    assert result["armed_stop"] == round(19.5 * 0.95, 4)  # untouched


def test_run_exit_cycle_price_drop_at_same_level_does_not_loosen_the_stop(isolated_state):
    """A stop is a fixed price level once armed, not a percentage that
    slides with price -- if price later falls, a FRESH same-level candidate
    (price * (1 - pct)) is numerically lower/looser than the original
    (since both use the same pct off a now-lower price), even though the
    original stop is now much CLOSER to the new price than it used to be.
    The ratchet correctly keeps the original (now-tighter-relative-to-
    current-price) stop rather than replacing it with the fresh, looser
    one."""
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=1))  # arms wide off 19.5: 17.55
    result = smc.run_exit_cycle(client, 18.0, _fake_signal(exit_level=1))  # price fell -- fresh candidate off 18.0
    candidate = round(18.0 * 0.90, 4)  # 16.2 -- looser than 17.55 despite the lower price
    assert candidate < round(19.5 * 0.90, 4)
    assert result["action"] == "none"  # ratchet correctly rejects the looser candidate
    assert result["armed_stop"] == round(19.5 * 0.90, 4)  # unchanged at 17.55


def test_run_exit_cycle_flattens_on_tier3_regardless_of_stop(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    result = smc.run_exit_cycle(client, 19.8, _fake_signal(exit_level=3))
    assert result["action"] == "flatten_submitted"
    assert client.submitted == [{"symbol": "SVIX", "qty": 100, "side": OrderSide.SELL}]


def test_run_exit_cycle_flattens_when_price_breaches_armed_stop(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.5, _fake_signal(exit_level=2))  # arms stop at 18.525
    result = smc.run_exit_cycle(client, 18.5, _fake_signal(exit_level=0))  # price breaches, signal itself gone quiet
    assert result["action"] == "flatten_submitted"


def test_run_exit_cycle_does_not_resubmit_while_flatten_in_flight(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.8, _fake_signal(exit_level=3))  # submits flatten, still "filled"->accepted (pending)
    result = smc.run_exit_cycle(client, 19.8, _fake_signal(exit_level=3))
    assert result["action"] == "flatten_in_progress"
    assert len(client.submitted) == 1  # no duplicate order


def test_run_exit_cycle_resubmits_after_rejected_flatten(isolated_state):
    smc._record_lot(20.0, 100, 20.0)
    client = FakeClient()
    smc.run_exit_cycle(client, 19.8, _fake_signal(exit_level=3))
    order_id = smc._load_state()["pending_orders"][0]["order_id"]
    client.set_status(order_id, "rejected")

    result = smc.run_exit_cycle(client, 19.8, _fake_signal(exit_level=3))
    assert result["action"] == "flatten_submitted"
    assert len(client.submitted) == 2  # resubmitted
