"""
dry_run=True regression tests for monitor/vix_executor.py, migrated to
Alpaca paper trading. Every order goes through Alpaca's single
client.submit_order(OrderRequest) instead of RH's distinct
order_*_market/order_*_option_limit calls, and confirmation polling is a
single unified client.get_order_by_id() instead of RH's
get_stock_order_info/get_option_order_info split — so the "must poll the
right lookup" regression tests collapse into "must poll get_order_by_id
with the returned order_id", but the safety-gating tests (dry_run,
kill switch, session state, roll two-leg sequencing) are otherwise
unchanged in spirit from the RH version this replaces.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.enums import OrderStatus

from monitor import vix_executor
from monitor.vix_executor import _size_svix_shares, _size_option_contracts, _size_and_explain_option, _occ_symbol
from monitor.vix_options import ContractPick
from monitor.vix_session import SessionResult, HEALTHY, DEAD
from monitor.vix_signals import (
    Action, SELL_SVIX_ALL, BUY_SVIX_SHARES, BUY_UVXY_PUT, BUY_VXX_CALL, CLOSE_OPTION, ROLL_OPTION,
)


def _healthy_session(client):
    return SessionResult(state=HEALTHY, reason="ok", buying_power=10000.0, client=client)


def _fake_client(order_id="order-1", order_status=OrderStatus.FILLED):
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(id=order_id)
    client.get_order_by_id.return_value = SimpleNamespace(status=order_status)
    return client


def _last_request(client):
    return client.submit_order.call_args.args[0]


def test_dry_run_flatten_previews_order_without_calling_alpaca(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(SELL_SVIX_ALL, "SVIX", "test flatten", {"type": "share", "quantity": 10})

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=True)

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.executed is False
    assert o.would_execute is True
    assert o.dry_run is True
    assert o.order_preview == {
        "call": "submit_order", "order_type": "market", "symbol": "SVIX",
        "quantity": 10, "side": "sell", "time_in_force": "day",
    }
    client.submit_order.assert_not_called()


def test_dry_run_never_writes_state_file(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(SELL_SVIX_ALL, "SVIX", "test flatten", {"type": "share", "quantity": 10})

    vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=True)

    assert not state_file.exists(), "dry_run must never persist last_flatten_session/last_entry_session"


def test_live_run_still_writes_state_file_and_calls_alpaca(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    client = _fake_client(order_id="abc123")
    session = _healthy_session(client)
    action = Action(SELL_SVIX_ALL, "SVIX", "test flatten", {"type": "share", "quantity": 10})

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=False)

    assert outcomes[0].executed is True
    assert outcomes[0].order_id == "abc123"
    req = _last_request(client)
    assert req.symbol == "SVIX" and float(req.qty) == 10 and req.side.value == "sell"
    assert state_file.exists()
    assert json.loads(state_file.read_text())["last_flatten_reason"] == "test flatten"


# ── Position sizing (BUY_SVIX_SHARES) ───────────────────────────────────

def test_size_svix_shares_bound_by_svix_cap_when_sleeve_is_clear():
    # NAV=$10,000, SVIX cap 5% = $500, price=$26 -> floor(500/26) = 19
    qty, mv = _size_svix_shares(nav=10000, sleeve_mv=0, price=26.0)
    assert qty == 19
    assert mv == 19 * 26.0


def test_size_svix_shares_bound_by_tighter_sleeve_headroom():
    # sleeve already holds $300 of other vol positions (VXX/UVXY options).
    # Sleeve cap 5%*10000=$500 -> headroom $200, tighter than the $500 SVIX
    # cap alone -> floor(200/26) = 7, not 19.
    qty, mv = _size_svix_shares(nav=10000, sleeve_mv=300, price=26.0)
    assert qty == 7


def test_size_svix_shares_zero_when_headroom_below_one_share():
    qty, mv = _size_svix_shares(nav=10000, sleeve_mv=480, price=26.0)  # $20 headroom
    assert qty == 0
    assert mv == 0.0


def test_size_svix_shares_zero_on_real_small_nav():
    # Real small-account case: NAV=$193.67, SVIX~$26.10 -> 5% cap = $9.68,
    # can't afford one share. Must refuse, not round up.
    qty, mv = _size_svix_shares(nav=193.67, sleeve_mv=0, price=26.10)
    assert qty == 0


def test_dry_run_entry_previews_correctly_sized_order(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_SVIX_SHARES, "SVIX", "test entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    o = outcomes[0]
    assert o.executed is False
    assert o.would_execute is True
    assert o.order_preview == {
        "call": "submit_order", "order_type": "market", "symbol": "SVIX",
        "quantity": 19, "side": "buy", "time_in_force": "day",
    }
    client.submit_order.assert_not_called()


def test_zero_quantity_entry_is_refused_cleanly_not_submitted(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_SVIX_SHARES, "SVIX", "test entry")

    outcomes = vix_executor.execute_actions([action], session, nav=193.67, sleeve_mv=0, quote_fn=lambda t: 26.10, dry_run=True)

    o = outcomes[0]
    assert o.executed is False
    assert o.would_execute is False
    assert "sized to 0 shares" in o.skip_reason
    client.submit_order.assert_not_called()


def test_live_entry_calls_submit_order_with_computed_quantity(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    client = _fake_client(order_id="buy123")
    session = _healthy_session(client)
    action = Action(BUY_SVIX_SHARES, "SVIX", "test entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=False)

    assert outcomes[0].executed is True
    req = _last_request(client)
    assert req.symbol == "SVIX" and float(req.qty) == 19 and req.side.value == "buy" and req.time_in_force.value == "day"
    assert state_file.exists()
    assert json.loads(state_file.read_text())["last_entry_session"] == vix_executor._session_id()


# ── OCC option symbol builder ───────────────────────────────────────────

def test_occ_symbol_builds_expected_format():
    assert _occ_symbol("UVXY", "2026-01-15", 54.0, "put") == "UVXY260115P00054000"
    assert _occ_symbol("VXX", "2026-02-19", 20.0, "call") == "VXX260219C00020000"


# ── Option contract entries (BUY_UVXY_PUT / BUY_VXX_CALL) ──────────────

_FAKE_PUT = ContractPick(
    ticker="UVXY", option_type="put", strike=54.0, expiry="2099-01-15", dte=14,
    bid=1.10, ask=1.20, mid=1.15, delta=-0.35, open_interest=500, fallback=False,
)
_FAKE_CALL = ContractPick(
    ticker="VXX", option_type="call", strike=20.0, expiry="2099-02-19", dte=35,
    bid=1.30, ask=1.40, mid=1.35, delta=0.48, open_interest=300, fallback=False,
)
# A thin fallback strike with no real market (bid=ask=0) — must refuse with
# a specific message, not a generic "exceeds sleeve headroom" one that
# would wrongly suggest more NAV would help.
_FAKE_ZERO_PREMIUM_PUT = ContractPick(
    ticker="VXX", option_type="put", strike=18.0, expiry="2099-01-15", dte=17,
    bid=0.0, ask=0.0, mid=0.0, delta=-0.1, open_interest=10, fallback=True,
)


def test_size_and_explain_zero_premium_contract_is_refused_with_specific_reason():
    qty, mv, reason = _size_and_explain_option(_FAKE_ZERO_PREMIUM_PUT, nav=10000, sleeve_mv=0)
    assert qty == 0
    assert mv == 0.0
    assert "no real market" in reason
    assert "exceeds sleeve headroom" not in reason


def test_size_option_contracts_bound_by_max_contracts_cap(monkeypatch):
    monkeypatch.setattr(vix_executor, "VIX_MAX_CONTRACTS", 3)
    # premium=$115/contract, sleeve headroom huge -> would allow far more
    # than 3 by dollars alone; VIX_MAX_CONTRACTS caps it at 3.
    qty = _size_option_contracts(nav=1_000_000, sleeve_mv=0, premium_per_contract=115.0)
    assert qty == 3


def test_size_option_contracts_bound_by_sleeve_headroom():
    # NAV=$10,000, sleeve cap 5%=$500, premium=$115/contract -> floor(500/115)=4
    qty = _size_option_contracts(nav=10000, sleeve_mv=0, premium_per_contract=115.0)
    assert qty == 4


def test_size_option_contracts_zero_when_no_headroom():
    qty = _size_option_contracts(nav=10000, sleeve_mv=500, premium_per_contract=115.0)
    assert qty == 0


def test_dry_run_put_entry_picks_contract_and_previews_option_order(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: _FAKE_PUT)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is True
    assert o.order_preview == {
        "call": "submit_order", "order_type": "limit", "symbol": "UVXY990115P00054000",
        "quantity": 4,  # floor(500/(1.15*100))=4
        "side": "buy", "limit_price": 1.20, "time_in_force": "day",
    }
    client.submit_order.assert_not_called()


def test_dry_run_call_entry_picks_contract_and_previews_option_order(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_call", lambda ticker: _FAKE_CALL)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_VXX_CALL, "VXX", "test tactical long-vol entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 20.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is True
    assert o.order_preview["call"] == "submit_order"
    assert o.order_preview["symbol"] == "VXX990219C00020000"
    assert o.order_preview["limit_price"] == 1.40
    client.submit_order.assert_not_called()


def test_no_liquid_contract_found_is_refused_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: None)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is False
    assert "no liquid" in o.skip_reason
    client.submit_order.assert_not_called()


def test_option_zero_quantity_entry_is_refused_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: _FAKE_PUT)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    # Tiny NAV -> $115/contract premium exceeds 5% sleeve headroom -> 0 contracts
    outcomes = vix_executor.execute_actions([action], session, nav=100, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is False
    assert "sized to 0 contracts" in o.skip_reason
    client.submit_order.assert_not_called()


def test_live_put_entry_calls_submit_order_with_contract_terms(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: _FAKE_PUT)

    client = _fake_client(order_id="opt123")
    session = _healthy_session(client)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=False)

    assert outcomes[0].executed is True
    req = _last_request(client)
    assert req.symbol == "UVXY990115P00054000"
    assert float(req.qty) == 4
    assert req.side.value == "buy"
    assert float(req.limit_price) == 1.20
    # Regression: confirmation must poll the returned order_id via the
    # single unified get_order_by_id, same as an equity order.
    client.get_order_by_id.assert_called_once_with("opt123")


def test_close_option_flatten_builds_per_share_limit_price_from_per_contract_mid(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    client = _fake_client(order_id="close123")
    session = _healthy_session(client)
    # mid_price/cost_basis are $/contract (RH Tracker convention, from
    # vix_positions.py) -- Alpaca's limit_price is $/share, so the executor
    # must divide by 100 before submitting.
    position = {
        "type": "option", "ticker": "UVXY", "contracts": 2, "cost_basis": 230.0,
        "mid_price": 115.0, "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
    }
    action = Action(CLOSE_OPTION, "UVXY", "test stop", position)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=230, quote_fn=lambda t: 60.0, dry_run=False)

    assert outcomes[0].executed is True
    req = _last_request(client)
    assert req.symbol == "UVXY990115P00054000"
    assert float(req.qty) == 2
    assert req.side.value == "sell"
    assert float(req.limit_price) == 1.15
    client.get_order_by_id.assert_called_once_with("close123")


# ── Kill switch drill (Impl Plan §11 step 4) ────────────────────────────
# "Rehearse kill switch while HEALTHY (sells allowed) and while DEAD (alert only)."

def test_kill_switch_still_allows_flatten_when_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "VIX_KILL_SWITCH", True)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(SELL_SVIX_ALL, "SVIX", "kill switch drill: flatten", {"type": "share", "quantity": 10})

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=True)

    assert outcomes[0].would_execute is True
    assert outcomes[0].order_preview is not None


def test_kill_switch_blocks_entries_when_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "VIX_KILL_SWITCH", True)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(BUY_SVIX_SHARES, "SVIX", "kill switch drill: entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    assert outcomes[0].would_execute is False
    assert "VIX_KILL_SWITCH=true" in outcomes[0].skip_reason
    client.submit_order.assert_not_called()


def test_kill_switch_plus_dead_session_is_alert_only_zero_orders(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "VIX_KILL_SWITCH", True)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    dead_session = SessionResult(state=DEAD, reason="session build failed", client=None)
    flatten = Action(SELL_SVIX_ALL, "SVIX", "kill switch + dead", {"type": "share", "quantity": 10})
    entry = Action(BUY_SVIX_SHARES, "SVIX", "kill switch + dead")

    outcomes = vix_executor.execute_actions([flatten, entry], dead_session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    assert all(not o.executed and not o.would_execute for o in outcomes)
    assert all("session not usable" in o.skip_reason for o in outcomes)


def test_dead_session_produces_zero_orders_for_any_proposed_action():
    dead_session = SessionResult(state=DEAD, reason="account/equity check failed", client=None)
    flatten = Action(SELL_SVIX_ALL, "SVIX", "dead session drill: flatten", {"type": "share", "quantity": 10})
    entry = Action(BUY_SVIX_SHARES, "SVIX", "dead session drill: entry")

    outcomes = vix_executor.execute_actions([flatten, entry], dead_session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    assert all(not o.executed for o in outcomes)
    assert len(outcomes) == 2
    # Confirms the executor never even reaches Alpaca — session.client is
    # None, so there's no client object to call submit_order on at all.


# ── ROLL_OPTION (close old contract, open a fresh one, same quantity) ──

_HELD_UVXY_PUT = {
    "type": "option", "ticker": "UVXY", "contracts": 3, "cost_basis": 345.0,
    "mid_price": 155.0, "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
}
_FRESH_PUT_REPLACEMENT = ContractPick(
    ticker="UVXY", option_type="put", strike=55.0, expiry="2099-02-19", dte=18,
    bid=1.10, ask=1.20, mid=1.15, delta=-0.32, open_interest=400, fallback=False,
)
# pick_put() can return a real contract with bid=ask=0 (no real market) on a
# thin fallback strike — the roll's open leg must refuse this itself since
# it bypasses _size_and_explain_option() (where this same check lives for
# the regular entry path).
_ZERO_PREMIUM_REPLACEMENT = ContractPick(
    ticker="VXX", option_type="put", strike=18.0, expiry="2099-01-15", dte=17,
    bid=0.0, ask=0.0, mid=0.0, delta=-0.1, open_interest=10, fallback=True,
)


def test_roll_disabled_by_default_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_ROLL", False)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(ROLL_OPTION, "UVXY", "roll candidate: +25%", _HELD_UVXY_PUT)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=345, quote_fn=lambda t: 60.0, dry_run=True)

    assert len(outcomes) == 1
    assert outcomes[0].would_execute is False
    assert "ENABLE_VIX_AUTO_ROLL=false" in outcomes[0].skip_reason
    client.submit_order.assert_not_called()


def test_dry_run_roll_previews_both_legs(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_ROLL", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price, primary_ticker, fallback_ticker: _FRESH_PUT_REPLACEMENT)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(ROLL_OPTION, "UVXY", "roll candidate: +25%", _HELD_UVXY_PUT)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=345, quote_fn=lambda t: 60.0, dry_run=True)

    assert len(outcomes) == 2
    close_leg, open_leg = outcomes
    assert close_leg.order_preview["symbol"] == "UVXY990115P00054000"  # the OLD contract
    assert close_leg.order_preview["quantity"] == 3
    assert close_leg.order_preview["limit_price"] == 1.55  # 155.0 mid_price / 100
    assert open_leg.order_preview["symbol"] == "UVXY990219P00055000"  # the NEW contract
    assert open_leg.order_preview["quantity"] == 3  # same quantity, not resized
    client.submit_order.assert_not_called()


def test_roll_stops_after_close_if_no_replacement_found(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_ROLL", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price, primary_ticker, fallback_ticker: None)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(ROLL_OPTION, "UVXY", "roll candidate: +25%", _HELD_UVXY_PUT)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=345, quote_fn=lambda t: 60.0, dry_run=True)

    assert len(outcomes) == 2
    assert outcomes[0].would_execute is True  # close leg still previewed
    assert outcomes[1].would_execute is False
    assert "flat on this leg, not doubled" in outcomes[1].skip_reason


def test_roll_refuses_zero_premium_replacement_not_doubled_or_stuck(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_ROLL", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price, primary_ticker, fallback_ticker: _ZERO_PREMIUM_REPLACEMENT)

    client = _fake_client()
    session = _healthy_session(client)
    action = Action(ROLL_OPTION, "UVXY", "roll candidate: +25%", _HELD_UVXY_PUT)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=345, quote_fn=lambda t: 60.0, dry_run=True)

    assert len(outcomes) == 2
    assert outcomes[0].would_execute is True  # close leg still previewed
    assert outcomes[1].would_execute is False
    assert "no real market" in outcomes[1].skip_reason
    assert "not doubled" in outcomes[1].skip_reason
    client.submit_order.assert_not_called()


def test_live_roll_calls_both_legs_with_unified_order_polling(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_ROLL", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price, primary_ticker, fallback_ticker: _FRESH_PUT_REPLACEMENT)

    client = _fake_client()
    close_order = SimpleNamespace(id="close-roll-1")
    open_order = SimpleNamespace(id="open-roll-1")
    client.submit_order.side_effect = [close_order, open_order]
    session = _healthy_session(client)
    action = Action(ROLL_OPTION, "UVXY", "roll candidate: +25%", _HELD_UVXY_PUT)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=345, quote_fn=lambda t: 60.0, dry_run=False)

    assert len(outcomes) == 2
    assert outcomes[0].executed is True
    assert outcomes[1].executed is True
    close_req, open_req = (c.args[0] for c in client.submit_order.call_args_list)
    assert close_req.symbol == "UVXY990115P00054000" and close_req.side.value == "sell" and float(close_req.limit_price) == 1.55
    assert open_req.symbol == "UVXY990219P00055000" and open_req.side.value == "buy" and float(open_req.limit_price) == 1.20
    # Both legs use the same unified get_order_by_id polling.
    assert client.get_order_by_id.call_count == 2
    client.get_order_by_id.assert_any_call("close-roll-1")
    client.get_order_by_id.assert_any_call("open-roll-1")


def test_roll_requires_healthy_session_not_just_flatten_eligible(monkeypatch):
    from monitor.vix_session import DEGRADED  # not exported alongside HEALTHY/DEAD above
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_ROLL", True)
    degraded_session = SessionResult(state=DEGRADED, reason="soft error", client=MagicMock())
    action = Action(ROLL_OPTION, "UVXY", "roll candidate: +25%", _HELD_UVXY_PUT)

    outcomes = vix_executor.execute_actions([action], degraded_session, nav=10000, sleeve_mv=345, quote_fn=lambda t: 60.0, dry_run=True)

    assert outcomes[0].would_execute is False
    assert "!= HEALTHY" in outcomes[0].skip_reason
