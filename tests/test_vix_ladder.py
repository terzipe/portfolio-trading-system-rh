"""
monitor/vix_ladder.py — SVIX ladder strategy state machine. See
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md v2.0 for the full spec. No network
needed — evaluate() is driven entirely by explicit vix/nav/positions/
live_price args against a persisted (here, tmp_path-isolated) state file,
with the percentile threshold table faked via `fake_thresholds` so these
tests never touch monitor/vix_percentile.py's FRED-backed cache.

Fake threshold table used throughout: {90: 30, 92.5: 40, 95: 50, 97.5: 60,
99: 70} -- deliberately reuses the old v1.0 dollar-ladder's VIX levels
(30/40/50/60/70) so each rung's *level* behavior is directly comparable to
the pre-redesign suite; what's new here is that rungs are now identified
and tracked by percentile, not by VIX level.
"""
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderStatus

from monitor import vix_ladder, vix_percentile
from monitor.vix_signals import BUY_SVIX_RUNG, SELL_SVIX_PARTIAL

FAKE_THRESHOLDS = {90: 30.0, 92.5: 40.0, 95: 50.0, 97.5: 60.0, 99: 70.0}


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_file = tmp_path / "svix_ladder_state.json"
    monkeypatch.setattr(vix_ladder, "VIX_SVIX_LADDER_STATE_FILE", state_file)
    monkeypatch.setattr(vix_percentile, "get_thresholds", lambda: FAKE_THRESHOLDS)
    monkeypatch.setattr(vix_percentile, "is_stale", lambda: False)
    return state_file


def _held(qty):
    return [{"ticker": "SVIX", "type": "share", "quantity": qty}]


# ── No percentile data yet ──────────────────────────────────────────

def test_no_action_when_thresholds_unavailable(tmp_path, monkeypatch):
    state_file = tmp_path / "svix_ladder_state.json"
    monkeypatch.setattr(vix_ladder, "VIX_SVIX_LADDER_STATE_FILE", state_file)
    monkeypatch.setattr(vix_percentile, "get_thresholds", lambda: None)  # never refreshed
    actions = vix_ladder.evaluate(vix=90.0, nav=100_000, positions=[], live_price=90.0)
    assert actions == []
    assert vix_ladder.get_status()["armed"] is False


# ── Arming ────────────────────────────────────────────────────────────

def test_no_action_below_arm_level(isolated_state):
    actions = vix_ladder.evaluate(vix=25.0, nav=100_000, positions=[], live_price=25.0)
    assert actions == []
    assert vix_ladder.get_status()["armed"] is False


def test_arms_and_buys_first_rung_above_arm_level(isolated_state):
    actions = vix_ladder.evaluate(vix=31.0, nav=100_000, positions=[], live_price=31.0)
    assert len(actions) == 1
    assert actions[0].action == BUY_SVIX_RUNG
    assert actions[0].position["rung_percentile"] == 90
    assert actions[0].position["rung_vix_level"] == 30.0
    assert actions[0].position["target_dollars"] == 5000
    assert vix_ladder.get_status()["armed"] is True


def test_does_not_arm_new_campaign_on_stale_thresholds(isolated_state, monkeypatch):
    monkeypatch.setattr(vix_percentile, "is_stale", lambda: True)
    actions = vix_ladder.evaluate(vix=31.0, nav=100_000, positions=[], live_price=31.0)
    assert actions == []
    assert vix_ladder.get_status()["armed"] is False


def test_stale_thresholds_do_not_block_an_already_armed_campaign(isolated_state, monkeypatch):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.12)
    monkeypatch.setattr(vix_percentile, "is_stale", lambda: True)
    actions = vix_ladder.evaluate(vix=41.0, nav=1_000_000, positions=_held(166), live_price=41.0)
    assert len(actions) == 1
    assert actions[0].position["rung_percentile"] == 92.5


def test_dry_run_evaluate_does_not_persist_arm(isolated_state):
    vix_ladder.evaluate(vix=31.0, nav=100_000, positions=[], live_price=31.0, dry_run=True)
    assert vix_ladder.get_status()["armed"] is False
    assert not isolated_state.exists()


# ── Rung ladder ──────────────────────────────────────────────────────

def test_second_rung_not_bought_until_vix_reaches_it(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.12)
    actions = vix_ladder.evaluate(vix=35.0, nav=100_000, positions=_held(166), live_price=35.0)
    assert actions == []  # not pulled back, but the 92.5th rung (level 40) not reached yet


def test_next_rung_bought_once_vix_reaches_it(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.12)
    actions = vix_ladder.evaluate(vix=41.0, nav=100_000, positions=_held(166), live_price=41.0)
    assert len(actions) == 1
    assert actions[0].position["rung_percentile"] == 92.5
    assert actions[0].position["rung_vix_level"] == 40.0


def test_same_rung_never_bought_twice(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.12)
    # VIX dips back toward 30 then re-touches without reaching 40.
    actions = vix_ladder.evaluate(vix=30.5, nav=100_000, positions=_held(166), live_price=30.5)
    assert actions == []


def test_rung_buy_capped_at_residual_budget(isolated_state):
    # 15% of NAV=$10,000 -> $1,500 budget. First rung already used $1,200,
    # leaving only $300 -- next rung must be sized to the residual, not the
    # full $5,000.
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 12.0)  # cost basis $1,200
    actions = vix_ladder.evaluate(vix=41.0, nav=10_000, positions=_held(100), live_price=41.0)
    assert len(actions) == 1
    assert actions[0].position["target_dollars"] == pytest.approx(300.0)


def test_no_rung_buy_when_budget_exhausted(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 125, 12.0)  # cost basis $1,500 == full 15%*10000 budget
    actions = vix_ladder.evaluate(vix=41.0, nav=10_000, positions=_held(125), live_price=41.0)
    assert actions == []


# ── Top rung (99th percentile) as the implicit ceiling ────────────────

def test_gap_above_ceiling_still_buys_lowest_rung_first(isolated_state):
    # VIX gaps straight to 90 with nothing held: the ladder does not bulk-buy
    # or jump to the top -- it still starts at the lowest unbought rung.
    actions = vix_ladder.evaluate(vix=90.0, nav=1_000_000, positions=[], live_price=90.0)
    assert len(actions) == 1
    assert actions[0].position["rung_percentile"] == 90


def test_no_rung_above_99th_is_ever_bought(isolated_state):
    # All 5 configured rungs already bought, VIX pinned at 90 -> nothing
    # left in the threshold table above the 99th rung to propose.
    vix_ladder.arm_campaign(90.0)
    for pct, level in FAKE_THRESHOLDS.items():
        vix_ladder.record_rung_bought(pct, level, 100, level)
    actions = vix_ladder.evaluate(vix=90.0, nav=1_000_000, positions=_held(500), live_price=90.0)
    assert actions == []  # nothing left to propose above the 99th rung


def test_rung_at_exactly_the_threshold_is_allowed(isolated_state):
    vix_ladder.arm_campaign(70.0)  # peak == 70, so evaluating at vix=70 is 0% pullback
    for pct in (90, 92.5, 95, 97.5):
        vix_ladder.record_rung_bought(pct, FAKE_THRESHOLDS[pct], 100, FAKE_THRESHOLDS[pct])
    actions = vix_ladder.evaluate(vix=70.0, nav=1_000_000, positions=_held(400), live_price=70.0)
    assert len(actions) == 1
    assert actions[0].position["rung_percentile"] == 99  # inclusive: level == vix is allowed


def test_top_rung_is_not_a_one_way_latch(isolated_state):
    # The top rung caps the rung *level*, it does not latch buying off once
    # touched. With peak just above the 99th-percentile level (so VIX is
    # still in the not-pulled-back buying regime) a skipped lower rung
    # stays eligible as VIX rounds back down -- resume-on-the-way-down, per
    # the chosen design (vs. a hard campaign latch).
    vix_ladder.arm_campaign(72.0)          # peak 72, just over the 99th-pct level (70)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    # VIX at 70.6: not pulled back (70.6 > 72*0.97=69.84), so it's a buying
    # cycle, and the 92.5th rung (skipped earlier) is offered again.
    actions = vix_ladder.evaluate(vix=70.6, nav=1_000_000, positions=_held(100), live_price=70.6)
    assert len(actions) == 1
    assert actions[0].position["rung_percentile"] == 92.5


# ── Peak / pullback / take-profit (unchanged, raw VIX-percent terms) ──

def test_pullback_under_threshold_keeps_scaling_not_selling(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.0)
    vix_ladder.record_rung_bought(92.5, 40.0, 125, 40.0)
    # 2% pullback from peak 40 -> 39.2, under the 3% pullback threshold.
    actions = vix_ladder.evaluate(vix=39.2, nav=100_000, positions=_held(291), live_price=39.2)
    assert actions == []  # not pulled back enough, and no higher rung reached either


def test_pullback_at_threshold_evaluates_take_profit(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)  # avg cost $30/share, cost basis $3000
    # 3% pullback from peak 40 -> exactly 38.8, pulled_back is true (<=).
    # live_price way above avg cost to trigger the first take-profit step.
    actions = vix_ladder.evaluate(vix=38.8, nav=100_000, positions=_held(100), live_price=38.0)
    assert len(actions) == 1
    assert actions[0].action == SELL_SVIX_PARTIAL


def test_no_take_profit_when_pnl_below_first_threshold(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    # live_price only 10% above cost -- below the +25% first threshold.
    actions = vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(100), live_price=33.0)
    assert actions == []


def test_take_profit_sells_quarter_of_peak_shares(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)  # campaign_peak_shares = 100
    actions = vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(100), live_price=37.5)  # +25% pnl
    assert len(actions) == 1
    assert actions[0].position["sell_quantity"] == 25  # 25% of 100
    assert actions[0].position["step"] == 1


def test_final_take_profit_step_sells_all_remaining_not_a_rounded_quarter(isolated_state):
    # 166 peak shares: 3 steps of floor(0.25*166)=41 leaves 166-123=43 on
    # the 4th step -- must sell all 43, not another 41 (which would strand
    # 2 shares forever).
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.0)
    state = vix_ladder._load_state()
    state["take_profit_steps_done"] = 3
    state["open_lots"] = [{"percentile": 90, "vix_level": 30.0, "qty_remaining": 43, "price": 30.0, "at": "t"}]
    vix_ladder._save_state(state)

    actions = vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(43), live_price=200.0)  # huge pnl, well past +100%
    assert len(actions) == 1
    assert actions[0].position["sell_quantity"] == 43
    assert actions[0].position["step"] == 4


def test_take_profit_dry_run_does_not_persist(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(100), live_price=37.5, dry_run=True)
    assert vix_ladder.get_status()["take_profit_steps_done"] == 0


# ── The whipsaw case: re-arm after partial take-profit, counter resets ──

def test_rearm_after_partial_takeprofit_resumes_buying_new_high(isolated_state):
    # Your exact whipsaw example: 2 rungs bought (90th, 92.5th) on the way
    # up, then some sold via take-profit on the pullback, then VIX spikes
    # back up past the next rung -- the remaining shares must not be
    # "stuck"; buying should resume.
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    vix_ladder.record_rung_bought(92.5, 40.0, 100, 40.0)
    vix_ladder.record_take_profit_step(1, 50)  # sold a quarter of 200, 150 shares remain

    # VIX pushes to a brand-new campaign high past the next rung (95th, 50).
    actions = vix_ladder.evaluate(vix=51.0, nav=1_000_000, positions=_held(150), live_price=51.0)
    assert len(actions) == 1
    assert actions[0].action == BUY_SVIX_RUNG
    assert actions[0].position["rung_percentile"] == 95


def test_rung_buy_after_takeprofit_resets_step_counter(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    vix_ladder.record_take_profit_step(1, 25)
    assert vix_ladder.get_status()["take_profit_steps_done"] == 1

    vix_ladder.record_rung_bought(95, 50.0, 50, 50.0)
    assert vix_ladder.get_status()["take_profit_steps_done"] == 0  # restart-on-rearm, confirmed 2026-08-20


def test_rearm_take_profit_chunks_use_new_larger_peak_shares(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    vix_ladder.record_take_profit_step(1, 25)  # 75 shares remain, peak_shares still 100
    vix_ladder.record_rung_bought(95, 50.0, 100, 50.0)  # peak_shares now 200, counter reset to 0

    status = vix_ladder.get_status()
    assert status["campaign_peak_shares"] == 200
    assert status["take_profit_steps_done"] == 0
    # A future take-profit step now sells 25% of 200 (=50), not 25% of the
    # original 100 -- confirmed via the sizing math directly.


# ── Self-healing reconciliation ─────────────────────────────────────

def test_selfheal_resets_when_real_position_is_flat_but_state_isnt(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.0)
    assert vix_ladder.get_status()["armed"] is True

    # Real broker position shows zero SVIX (e.g. manual flatten happened
    # out of band) -- evaluate() must self-heal back to idle.
    actions = vix_ladder.evaluate(vix=20.0, nav=100_000, positions=[], live_price=20.0)
    assert actions == []
    assert vix_ladder.get_status()["armed"] is False


def test_selfheal_does_not_fire_in_dry_run(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.0)
    vix_ladder.evaluate(vix=20.0, nav=100_000, positions=[], live_price=20.0, dry_run=True)
    # Real persisted state is untouched by the dry_run call.
    assert vix_ladder.get_status()["armed"] is True


# ── Full reset ───────────────────────────────────────────────────────

def test_reset_campaign_clears_everything(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(90, 30.0, 166, 30.0)
    vix_ladder.reset_campaign()
    status = vix_ladder.get_status()
    assert status["armed"] is False
    assert status["rungs_bought"] == []
    assert status["current_shares"] == 0


# ── Pending-order tracking (fixes the real race hit live 2026-08-21) ───

def _fake_client(status, filled_qty=0, filled_avg_price=None):
    order = SimpleNamespace(status=status, filled_qty=filled_qty, filled_avg_price=filled_avg_price)
    client = SimpleNamespace(get_order_by_id=lambda order_id: order)
    return client


def test_record_rung_submitted_does_not_create_a_real_holding(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    status = vix_ladder.get_status()
    assert status["current_shares"] == 0  # not a real holding yet
    assert status["rungs_bought"] == []
    assert len(status["pending_orders"]) == 1


def test_next_unbought_rung_skips_a_rung_with_a_pending_buy(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    # A cycle running before the order-1 fill confirms must not propose a
    # second buy at the 90th rung -- it should skip straight to 92.5.
    actions = vix_ladder.evaluate(vix=41.0, nav=1_000_000, positions=[], live_price=41.0)
    assert len(actions) == 1
    assert actions[0].position["rung_percentile"] == 92.5


def test_selfheal_does_not_fire_while_a_buy_is_merely_pending(isolated_state):
    # The exact race hit live 2026-08-21: order submitted after-hours,
    # still queued (not filled). A cycle runs in that gap and sees the
    # real broker position at zero SVIX. Before the fix, this triggered
    # self-heal and wiped the campaign; now it must not, since the
    # pending order was never recorded as a real holding in the first
    # place.
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    vix_ladder.evaluate(vix=31.5, nav=1_000_000, positions=[], live_price=31.5)

    status = vix_ladder.get_status()
    assert status["armed"] is True  # NOT reset
    assert len(status["pending_orders"]) == 1  # still tracked, awaiting fill


def test_reconcile_finalizes_a_filled_buy_into_a_real_holding(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    client = _fake_client(OrderStatus.FILLED, filled_qty=166, filled_avg_price="30.12")
    vix_ladder.reconcile_pending_orders(client)

    status = vix_ladder.get_status()
    assert status["current_shares"] == 166
    assert status["rungs_bought"] == [90]
    assert status["pending_orders"] == []


def test_reconcile_leaves_a_still_unfilled_order_pending(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    client = _fake_client(OrderStatus.ACCEPTED)
    vix_ladder.reconcile_pending_orders(client)

    status = vix_ladder.get_status()
    assert status["current_shares"] == 0
    assert len(status["pending_orders"]) == 1


def test_reconcile_drops_a_rejected_order_without_creating_a_holding(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    client = _fake_client(OrderStatus.REJECTED)
    vix_ladder.reconcile_pending_orders(client)

    status = vix_ladder.get_status()
    assert status["current_shares"] == 0
    assert status["rungs_bought"] == []
    assert status["pending_orders"] == []
    # The rung is available again for a future attempt.
    actions = vix_ladder.evaluate(vix=31.0, nav=1_000_000, positions=[], live_price=31.0)
    assert actions[0].position["rung_percentile"] == 90


def test_reconcile_finalizes_a_filled_takeprofit_sell(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(90, 30.0, 100, 30.0)
    vix_ladder.record_takeprofit_submitted(1, "sell-order-1", 25)

    client = _fake_client(OrderStatus.FILLED, filled_qty=25)
    vix_ladder.reconcile_pending_orders(client)

    status = vix_ladder.get_status()
    assert status["current_shares"] == 75
    assert status["take_profit_steps_done"] == 1
    assert status["pending_orders"] == []


def test_reconcile_survives_a_client_lookup_error(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_submitted(90, 30.0, "order-1", 5000)

    class _FailingClient:
        def get_order_by_id(self, order_id):
            raise Exception("boom")

    vix_ladder.reconcile_pending_orders(_FailingClient())  # must not raise

    status = vix_ladder.get_status()
    assert len(status["pending_orders"]) == 1  # unchanged, retry next cycle


def test_reconcile_noop_when_nothing_pending(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.reconcile_pending_orders(_fake_client(OrderStatus.FILLED, filled_qty=1))  # should not touch state
    assert vix_ladder.get_status()["pending_orders"] == []
