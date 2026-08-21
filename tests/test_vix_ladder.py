"""
monitor/vix_ladder.py — SVIX ladder strategy state machine. See
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md for the full spec. No network
needed — evaluate() is driven entirely by explicit vix/nav/positions/
live_price args against a persisted (here, tmp_path-isolated) state file.
"""
import pytest

from monitor import vix_ladder
from monitor.vix_signals import BUY_SVIX_RUNG, SELL_SVIX_PARTIAL


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_file = tmp_path / "svix_ladder_state.json"
    monkeypatch.setattr(vix_ladder, "VIX_SVIX_LADDER_STATE_FILE", state_file)
    return state_file


def _held(qty):
    return [{"ticker": "SVIX", "type": "share", "quantity": qty}]


# ── Arming ────────────────────────────────────────────────────────────

def test_no_action_below_arm_level(isolated_state):
    actions = vix_ladder.evaluate(vix=25.0, nav=100_000, positions=[], live_price=25.0)
    assert actions == []
    assert vix_ladder.get_status()["armed"] is False


def test_arms_and_buys_first_rung_above_arm_level(isolated_state):
    actions = vix_ladder.evaluate(vix=31.0, nav=100_000, positions=[], live_price=31.0)
    assert len(actions) == 1
    assert actions[0].action == BUY_SVIX_RUNG
    assert actions[0].position["rung_level"] == 30
    assert actions[0].position["target_dollars"] == 5000
    assert vix_ladder.get_status()["armed"] is True


def test_dry_run_evaluate_does_not_persist_arm(isolated_state):
    vix_ladder.evaluate(vix=31.0, nav=100_000, positions=[], live_price=31.0, dry_run=True)
    assert vix_ladder.get_status()["armed"] is False
    assert not isolated_state.exists()


# ── Rung ladder ──────────────────────────────────────────────────────

def test_second_rung_not_bought_until_vix_reaches_it(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 166, 30.12)
    actions = vix_ladder.evaluate(vix=35.0, nav=100_000, positions=_held(166), live_price=35.0)
    assert actions == []  # not pulled back, but 40 not reached yet either


def test_next_rung_bought_once_vix_reaches_it(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 166, 30.12)
    actions = vix_ladder.evaluate(vix=41.0, nav=100_000, positions=_held(166), live_price=41.0)
    assert len(actions) == 1
    assert actions[0].position["rung_level"] == 40


def test_same_rung_never_bought_twice(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 166, 30.12)
    # VIX dips back toward 30 then re-touches without reaching 40.
    actions = vix_ladder.evaluate(vix=30.5, nav=100_000, positions=_held(166), live_price=30.5)
    assert actions == []


def test_rung_buy_capped_at_residual_budget(isolated_state):
    # 15% of NAV=$10,000 -> $1,500 budget. First rung already used $1,200,
    # leaving only $300 -- next rung must be sized to the residual, not the
    # full $5,000.
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 100, 12.0)  # cost basis $1,200
    actions = vix_ladder.evaluate(vix=41.0, nav=10_000, positions=_held(100), live_price=41.0)
    assert len(actions) == 1
    assert actions[0].position["target_dollars"] == pytest.approx(300.0)


def test_no_rung_buy_when_budget_exhausted(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 125, 12.0)  # cost basis $1,500 == full 15%*10000 budget
    actions = vix_ladder.evaluate(vix=41.0, nav=10_000, positions=_held(125), live_price=41.0)
    assert actions == []


# ── Peak / pullback / take-profit ────────────────────────────────────

def test_pullback_under_threshold_keeps_scaling_not_selling(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 166, 30.0)
    vix_ladder.record_rung_bought(40, 125, 40.0)
    # 2% pullback from peak 40 -> 39.2, under the 3% pullback threshold.
    actions = vix_ladder.evaluate(vix=39.2, nav=100_000, positions=_held(291), live_price=39.2)
    assert actions == []  # not pulled back enough, and no higher rung reached either


def test_pullback_at_threshold_evaluates_take_profit(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)  # avg cost $30/share, cost basis $3000
    # 3% pullback from peak 40 -> exactly 38.8, pulled_back is true (<=).
    # live_price way above avg cost to trigger the first take-profit step.
    actions = vix_ladder.evaluate(vix=38.8, nav=100_000, positions=_held(100), live_price=38.0)
    assert len(actions) == 1
    assert actions[0].action == SELL_SVIX_PARTIAL


def test_no_take_profit_when_pnl_below_first_threshold(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)
    # live_price only 10% above cost -- below the +25% first threshold.
    actions = vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(100), live_price=33.0)
    assert actions == []


def test_take_profit_sells_quarter_of_peak_shares(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)  # campaign_peak_shares = 100
    actions = vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(100), live_price=37.5)  # +25% pnl
    assert len(actions) == 1
    assert actions[0].position["sell_quantity"] == 25  # 25% of 100
    assert actions[0].position["step"] == 1


def test_final_take_profit_step_sells_all_remaining_not_a_rounded_quarter(isolated_state):
    # 166 peak shares: 3 steps of floor(0.25*166)=41 leaves 166-123=43 on
    # the 4th step -- must sell all 43, not another 41 (which would strand
    # 2 shares forever).
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 166, 30.0)
    state = vix_ladder._load_state()
    state["take_profit_steps_done"] = 3
    state["open_lots"] = [{"level": 30, "qty_remaining": 43, "price": 30.0, "at": "t"}]
    vix_ladder._save_state(state)

    actions = vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(43), live_price=200.0)  # huge pnl, well past +100%
    assert len(actions) == 1
    assert actions[0].position["sell_quantity"] == 43
    assert actions[0].position["step"] == 4


def test_take_profit_dry_run_does_not_persist(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)
    vix_ladder.evaluate(vix=38.0, nav=100_000, positions=_held(100), live_price=37.5, dry_run=True)
    assert vix_ladder.get_status()["take_profit_steps_done"] == 0


# ── The whipsaw case: re-arm after partial take-profit, counter resets ──

def test_rearm_after_partial_takeprofit_resumes_buying_new_high(isolated_state):
    # Your exact whipsaw example: 2 rungs bought (30, 40) on the way up,
    # then some sold via take-profit on the pullback, then VIX spikes back
    # up past the next rung -- the remaining shares must not be "stuck";
    # buying should resume.
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)
    vix_ladder.record_rung_bought(40, 100, 40.0)
    vix_ladder.record_take_profit_step(1, 50)  # sold a quarter of 200, 150 shares remain

    # VIX pushes to a brand-new campaign high past the next rung (50).
    actions = vix_ladder.evaluate(vix=51.0, nav=1_000_000, positions=_held(150), live_price=51.0)
    assert len(actions) == 1
    assert actions[0].action == BUY_SVIX_RUNG
    assert actions[0].position["rung_level"] == 50


def test_rung_buy_after_takeprofit_resets_step_counter(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)
    vix_ladder.record_take_profit_step(1, 25)
    assert vix_ladder.get_status()["take_profit_steps_done"] == 1

    vix_ladder.record_rung_bought(50, 50, 50.0)
    assert vix_ladder.get_status()["take_profit_steps_done"] == 0  # restart-on-rearm, confirmed 2026-08-20


def test_rearm_take_profit_chunks_use_new_larger_peak_shares(isolated_state):
    vix_ladder.arm_campaign(40.0)
    vix_ladder.record_rung_bought(30, 100, 30.0)
    vix_ladder.record_take_profit_step(1, 25)  # 75 shares remain, peak_shares still 100
    vix_ladder.record_rung_bought(50, 100, 50.0)  # peak_shares now 200, counter reset to 0

    status = vix_ladder.get_status()
    assert status["campaign_peak_shares"] == 200
    assert status["take_profit_steps_done"] == 0
    # A future take-profit step now sells 25% of 200 (=50), not 25% of the
    # original 100 -- confirmed via the sizing math directly.


# ── Self-healing reconciliation ─────────────────────────────────────

def test_selfheal_resets_when_real_position_is_flat_but_state_isnt(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 166, 30.0)
    assert vix_ladder.get_status()["armed"] is True

    # Real broker position shows zero SVIX (e.g. manual flatten happened
    # out of band) -- evaluate() must self-heal back to idle.
    actions = vix_ladder.evaluate(vix=20.0, nav=100_000, positions=[], live_price=20.0)
    assert actions == []
    assert vix_ladder.get_status()["armed"] is False


def test_selfheal_does_not_fire_in_dry_run(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 166, 30.0)
    vix_ladder.evaluate(vix=20.0, nav=100_000, positions=[], live_price=20.0, dry_run=True)
    # Real persisted state is untouched by the dry_run call.
    assert vix_ladder.get_status()["armed"] is True


# ── Full reset ───────────────────────────────────────────────────────

def test_reset_campaign_clears_everything(isolated_state):
    vix_ladder.arm_campaign(31.0)
    vix_ladder.record_rung_bought(30, 166, 30.0)
    vix_ladder.reset_campaign()
    status = vix_ladder.get_status()
    assert status["armed"] is False
    assert status["rung_levels_bought"] == []
    assert status["current_shares"] == 0
