"""
dry_run=True regression tests for monitor/vix_executor.py — added after a
manual S2 dry-run drill needed external robin_stocks monkeypatching
because execute_actions() had no first-class dry-run mode, and that
external approach left a stray last_flatten_session in VIX_STATE_FILE.
These tests pin the fix: dry_run must produce the same gating decisions
and an order preview, call zero real order functions, and never touch
VIX_STATE_FILE.
"""
import json
from unittest.mock import MagicMock

from monitor import vix_executor
from monitor.vix_executor import _size_svix_shares, _size_option_contracts, _size_and_explain_option
from monitor.vix_options import ContractPick
from monitor.vix_session import SessionResult, HEALTHY, DEAD
from monitor.vix_signals import (
    Action, SELL_SVIX_ALL, BUY_SVIX_SHARES, BUY_UVXY_PUT, BUY_VXX_CALL, CLOSE_OPTION,
)


def _healthy_session(rh_module):
    return SessionResult(state=HEALTHY, reason="ok", buying_power=10000.0, rh_module=rh_module)


def test_dry_run_flatten_previews_order_without_calling_robin_stocks(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(SELL_SVIX_ALL, "SVIX", "test flatten", {"type": "share", "quantity": 10})

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=True)

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.executed is False
    assert o.would_execute is True
    assert o.dry_run is True
    assert o.order_preview == {
        "call": "order_sell_market", "symbol": "SVIX", "quantity": 10,
        "account_number": vix_executor._account_number(), "timeInForce": "gfd",
    }
    rh.order_sell_market.assert_not_called()


def test_dry_run_never_writes_state_file(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(SELL_SVIX_ALL, "SVIX", "test flatten", {"type": "share", "quantity": 10})

    vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=True)

    assert not state_file.exists(), "dry_run must never persist last_flatten_session/last_entry_session"


def test_live_run_still_writes_state_file_and_calls_robin_stocks(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    rh = MagicMock()
    rh.order_sell_market.return_value = {"id": "abc123"}
    rh.get_stock_order_info.return_value = {"state": "confirmed"}
    session = _healthy_session(rh)
    action = Action(SELL_SVIX_ALL, "SVIX", "test flatten", {"type": "share", "quantity": 10})

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=False)

    assert outcomes[0].executed is True
    rh.order_sell_market.assert_called_once()
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
    # Today's real Agentic account state: NAV=$193.67, SVIX~$26.10 ->
    # 5% cap = $9.68, can't afford one share. Must refuse, not round up.
    qty, mv = _size_svix_shares(nav=193.67, sleeve_mv=0, price=26.10)
    assert qty == 0


def test_dry_run_entry_previews_correctly_sized_order(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_SVIX_SHARES, "SVIX", "test entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    o = outcomes[0]
    assert o.executed is False
    assert o.would_execute is True
    assert o.order_preview == {
        "call": "order_buy_market", "symbol": "SVIX", "quantity": 19,
        "account_number": vix_executor._account_number(), "timeInForce": "gfd",
    }
    rh.order_buy_market.assert_not_called()


def test_zero_quantity_entry_is_refused_cleanly_not_submitted(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_SVIX_SHARES, "SVIX", "test entry")

    outcomes = vix_executor.execute_actions([action], session, nav=193.67, sleeve_mv=0, quote_fn=lambda t: 26.10, dry_run=True)

    o = outcomes[0]
    assert o.executed is False
    assert o.would_execute is False
    assert "sized to 0 shares" in o.skip_reason
    rh.order_buy_market.assert_not_called()


def test_live_entry_calls_order_buy_market_with_computed_quantity(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    rh = MagicMock()
    rh.order_buy_market.return_value = {"id": "buy123"}
    rh.get_stock_order_info.return_value = {"state": "confirmed"}
    session = _healthy_session(rh)
    action = Action(BUY_SVIX_SHARES, "SVIX", "test entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=False)

    assert outcomes[0].executed is True
    rh.order_buy_market.assert_called_once_with(
        "SVIX", 19, account_number=vix_executor._account_number(), timeInForce="gfd"
    )
    assert state_file.exists()
    assert json.loads(state_file.read_text())["last_entry_session"] == vix_executor._session_id()


# ── Option contract entries (BUY_UVXY_PUT / BUY_VXX_CALL) ──────────────

_FAKE_PUT = ContractPick(
    ticker="UVXY", option_type="put", strike=54.0, expiry="2099-01-15", dte=14,
    bid=1.10, ask=1.20, mid=1.15, delta=-0.35, open_interest=500, fallback=False,
)
_FAKE_CALL = ContractPick(
    ticker="VXX", option_type="call", strike=20.0, expiry="2099-02-19", dte=35,
    bid=1.30, ask=1.40, mid=1.35, delta=0.48, open_interest=300, fallback=False,
)
# Hit live 2026-08-19: a thin VXX put fallback with bid=ask=0 (no real
# market) — must refuse with a specific message, not a generic "exceeds
# sleeve headroom" one that would wrongly suggest more NAV would help.
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

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is True
    assert o.order_preview == {
        "call": "order_buy_option_limit", "position_effect": "open", "credit_or_debit": "debit",
        "price": 1.20, "symbol": "UVXY", "quantity": 4,  # floor(500/(1.15*100))=4
        "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
        "account_number": vix_executor._account_number(), "timeInForce": "gfd",
    }
    rh.order_buy_option_limit.assert_not_called()


def test_dry_run_call_entry_picks_contract_and_previews_option_order(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_call", lambda ticker: _FAKE_CALL)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_VXX_CALL, "VXX", "test tactical long-vol entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 20.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is True
    assert o.order_preview["call"] == "order_buy_option_limit"
    assert o.order_preview["symbol"] == "VXX"
    assert o.order_preview["price"] == 1.40
    rh.order_buy_option_limit.assert_not_called()


def test_no_liquid_contract_found_is_refused_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: None)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is False
    assert "no liquid" in o.skip_reason
    rh.order_buy_option_limit.assert_not_called()


def test_option_zero_quantity_entry_is_refused_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: _FAKE_PUT)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    # Tiny NAV -> $115/contract premium exceeds 5% sleeve headroom -> 0 contracts
    outcomes = vix_executor.execute_actions([action], session, nav=100, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=True)

    o = outcomes[0]
    assert o.would_execute is False
    assert "sized to 0 contracts" in o.skip_reason
    rh.order_buy_option_limit.assert_not_called()


def test_live_put_entry_calls_order_buy_option_limit_with_contract_terms(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)
    monkeypatch.setattr(vix_executor.vix_options, "pick_put", lambda spot_price: _FAKE_PUT)

    rh = MagicMock()
    rh.order_buy_option_limit.return_value = {"id": "opt123"}
    rh.get_option_order_info.return_value = {"state": "confirmed"}
    session = _healthy_session(rh)
    action = Action(BUY_UVXY_PUT, "UVXY", "test fade-spike entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 60.0, dry_run=False)

    assert outcomes[0].executed is True
    rh.order_buy_option_limit.assert_called_once_with(
        "open", "debit", 1.20, "UVXY", 4, "2099-01-15", 54.0, "put",
        account_number=vix_executor._account_number(), timeInForce="gfd",
    )
    # Regression: option order confirmation must poll get_option_order_info,
    # never get_stock_order_info (that was the bug this test pins).
    rh.get_option_order_info.assert_called_once_with("opt123")
    rh.get_stock_order_info.assert_not_called()


def test_close_option_flatten_polls_option_order_info_not_stock(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", state_file)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    rh = MagicMock()
    rh.order_sell_option_limit.return_value = {"id": "close123"}
    rh.get_option_order_info.return_value = {"state": "confirmed"}
    session = _healthy_session(rh)
    position = {
        "type": "option", "ticker": "UVXY", "contracts": 2, "cost_basis": 230.0,
        "mid_price": 1.15, "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
    }
    action = Action(CLOSE_OPTION, "UVXY", "test stop", position)

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=230, quote_fn=lambda t: 60.0, dry_run=False)

    assert outcomes[0].executed is True
    rh.get_option_order_info.assert_called_once_with("close123")
    rh.get_stock_order_info.assert_not_called()


# ── Kill switch drill (Impl Plan §11 step 4) ────────────────────────────
# "Rehearse kill switch while HEALTHY (sells allowed) and while DEAD (alert only)."

def test_kill_switch_still_allows_flatten_when_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "VIX_KILL_SWITCH", True)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(SELL_SVIX_ALL, "SVIX", "kill switch drill: flatten", {"type": "share", "quantity": 10})

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=250, quote_fn=lambda t: 26.0, dry_run=True)

    assert outcomes[0].would_execute is True
    assert outcomes[0].order_preview is not None


def test_kill_switch_blocks_entries_when_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "VIX_KILL_SWITCH", True)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_BUY", True)

    rh = MagicMock()
    session = _healthy_session(rh)
    action = Action(BUY_SVIX_SHARES, "SVIX", "kill switch drill: entry")

    outcomes = vix_executor.execute_actions([action], session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    assert outcomes[0].would_execute is False
    assert "VIX_KILL_SWITCH=true" in outcomes[0].skip_reason
    rh.order_buy_market.assert_not_called()


def test_kill_switch_plus_dead_session_is_alert_only_zero_orders(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_executor, "VIX_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(vix_executor, "VIX_KILL_SWITCH", True)
    monkeypatch.setattr(vix_executor, "ENABLE_VIX_AUTO_SELL", True)

    dead_session = SessionResult(state=DEAD, reason="session refresh failed", rh_module=None)
    flatten = Action(SELL_SVIX_ALL, "SVIX", "kill switch + dead", {"type": "share", "quantity": 10})
    entry = Action(BUY_SVIX_SHARES, "SVIX", "kill switch + dead")

    outcomes = vix_executor.execute_actions([flatten, entry], dead_session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    assert all(not o.executed and not o.would_execute for o in outcomes)
    assert all("session not usable" in o.skip_reason for o in outcomes)


# ── 429 drill (Impl Plan test matrix: "429 on device-approval poll -> DEAD,
# zero rh.login(), zero orders") ────────────────────────────────────────
# The session-layer 429 behavior (DEAD, zero rh.login() calls) is pinned in
# tests/test_vix_session.py::test_429_on_refresh_is_dead_no_login. This
# closes the other half: once vix_session.assess() reports DEAD from that
# 429, the executor must place zero orders regardless of what the regime
# engine proposed.

def test_dead_session_from_429_produces_zero_orders_for_any_proposed_action():
    dead_session = SessionResult(state=DEAD, reason="429 on token refresh", rh_module=None)
    flatten = Action(SELL_SVIX_ALL, "SVIX", "429 drill: flatten", {"type": "share", "quantity": 10})
    entry = Action(BUY_SVIX_SHARES, "SVIX", "429 drill: entry")

    outcomes = vix_executor.execute_actions([flatten, entry], dead_session, nav=10000, sleeve_mv=0, quote_fn=lambda t: 26.0, dry_run=True)

    assert all(not o.executed for o in outcomes)
    assert len(outcomes) == 2
    # Confirms the executor never even reaches robin_stocks — session.rh_module
    # is None, so there's no rh object to call order functions on at all.
