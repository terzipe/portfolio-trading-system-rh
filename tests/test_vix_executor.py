"""
dry_run=True regression tests for monitor/vix_executor.py — added after a
manual S2 dry-run drill needed external robin_stocks monkeypatching
because execute_actions() had no first-class dry-run mode, and that
external approach left a stray last_flatten_session in VIX_STATE_FILE.
These tests pin the fix: dry_run must produce the same gating decisions
and an order preview, call zero real order functions, and never touch
VIX_STATE_FILE.
"""
from unittest.mock import MagicMock

from monitor import vix_executor
from monitor.vix_session import SessionResult, HEALTHY
from monitor.vix_signals import Action, SELL_SVIX_ALL


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
    import json
    assert json.loads(state_file.read_text())["last_flatten_reason"] == "test flatten"
