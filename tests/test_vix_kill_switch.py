"""
monitor/vix_kill_switch.py — SVIX P&L-based auto kill switch. Manual reset
only: once tripped, stays tripped until reset() is called explicitly.
"""
import json

import pytest

from monitor import vix_kill_switch


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_file = tmp_path / "auto_kill_switch.json"
    monkeypatch.setattr(vix_kill_switch, "VIX_AUTO_KILL_STATE_FILE", state_file)
    monkeypatch.setattr(vix_kill_switch, "VIX_SVIX_STOP_PCT", -0.15)
    return state_file


def test_trips_when_svix_breaches_stop(isolated_state):
    positions = [{"ticker": "SVIX", "type": "share", "quantity": 10, "pnl_pct": -0.20}]

    tripped = vix_kill_switch.check_and_trip(positions)

    assert tripped is True
    assert vix_kill_switch.is_tripped() is True
    info = vix_kill_switch.get_trip_info()
    assert info["pnl_pct"] == -0.20
    assert "SVIX position P&L" in info["reason"]


def test_does_not_trip_when_pnl_above_stop(isolated_state):
    positions = [{"ticker": "SVIX", "type": "share", "quantity": 10, "pnl_pct": -0.05}]

    tripped = vix_kill_switch.check_and_trip(positions)

    assert tripped is False
    assert vix_kill_switch.is_tripped() is False


def test_does_not_trip_exactly_at_stop_boundary_inclusive(isolated_state):
    # pnl_pct == VIX_SVIX_STOP_PCT should trip (<=), not just strictly below
    positions = [{"ticker": "SVIX", "type": "share", "quantity": 10, "pnl_pct": -0.15}]

    tripped = vix_kill_switch.check_and_trip(positions)

    assert tripped is True


def test_does_not_trip_when_no_svix_held(isolated_state):
    positions = [{"ticker": "UVXY", "type": "option", "contracts": 2, "pnl_pct": -0.90}]

    tripped = vix_kill_switch.check_and_trip(positions)

    assert tripped is False
    assert vix_kill_switch.is_tripped() is False


def test_does_not_trip_when_pnl_pct_is_none(isolated_state):
    positions = [{"ticker": "SVIX", "type": "share", "quantity": 10, "pnl_pct": None}]

    tripped = vix_kill_switch.check_and_trip(positions)

    assert tripped is False


def test_second_call_is_a_noop_once_tripped(isolated_state):
    positions = [{"ticker": "SVIX", "type": "share", "quantity": 10, "pnl_pct": -0.30}]

    first = vix_kill_switch.check_and_trip(positions)
    second = vix_kill_switch.check_and_trip(positions)

    assert first is True
    assert second is False  # already tripped — never re-alerts every cycle
    info = vix_kill_switch.get_trip_info()
    assert info["pnl_pct"] == -0.30  # original trip reason preserved, not overwritten


def test_reset_clears_tripped_state(isolated_state):
    positions = [{"ticker": "SVIX", "type": "share", "quantity": 10, "pnl_pct": -0.30}]
    vix_kill_switch.check_and_trip(positions)
    assert vix_kill_switch.is_tripped() is True

    vix_kill_switch.reset()

    assert vix_kill_switch.is_tripped() is False
    assert vix_kill_switch.get_trip_info() is None


def test_reset_is_safe_when_not_tripped(isolated_state):
    vix_kill_switch.reset()  # should not raise
    assert vix_kill_switch.is_tripped() is False


def test_get_trip_info_none_when_not_tripped(isolated_state):
    assert vix_kill_switch.get_trip_info() is None


def test_get_trip_info_none_on_corrupted_state_file(isolated_state):
    isolated_state.write_text("not valid json")
    assert vix_kill_switch.get_trip_info() is None
