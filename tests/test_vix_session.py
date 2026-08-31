"""
S0 acceptance fixtures for monitor/vix_session.py, migrated to Alpaca paper
trading (Impl Plan §2A). Alpaca authenticates with a static API key pair,
not RH's pickled OAuth token, so there's no refresh-dance/429 drill left to
pin at this layer (that risk doesn't exist for Alpaca's auth model). What's
still pinned here: DEAD when the client can't be built or the
account/positions calls fail, and the same BOOK_MISMATCH fail-closed logic
as the RH version this replaced.

No network in CI — everything below is mocked.
"""
import json
import time
from types import SimpleNamespace

import pytest

from monitor import vix_session


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    session_state = tmp_path / "session_state.json"
    shadow_book = tmp_path / "last_known_positions.json"
    monkeypatch.setattr(vix_session, "VIX_SESSION_STATE_FILE", session_state)
    monkeypatch.setattr(vix_session, "VIX_SHADOW_BOOK_FILE", shadow_book)
    return SimpleNamespace(session_state=session_state, shadow_book=shadow_book)


class _FakeAccount:
    def __init__(self, buying_power="1000.0", equity="250.0", status="ACTIVE", trading_blocked=False, account_blocked=False):
        self.buying_power = buying_power
        self.equity = equity
        self.status = status
        self.trading_blocked = trading_blocked
        self.account_blocked = account_blocked


class _FakePosition:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty


class _FakeClient:
    def __init__(self, account=None, positions=None, positions_error=None, account_error=None):
        self._account = account or _FakeAccount()
        self._positions = positions if positions is not None else []
        self._positions_error = positions_error
        self._account_error = account_error

    def get_account(self):
        if self._account_error:
            raise self._account_error
        return self._account

    def get_all_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions


def _mock_client(monkeypatch, **kwargs):
    client = _FakeClient(**kwargs)
    monkeypatch.setattr(vix_session, "_build_client", lambda: client)
    return client


def test_healthy_when_account_and_positions_ok(isolated_paths, monkeypatch):
    _mock_client(monkeypatch, positions=[])

    result = vix_session.assess()

    assert result.state == vix_session.HEALTHY
    assert result.buying_power == 1000.0
    assert result.equity == 250.0  # distinct from buying_power -- Reg-T margin capacity vs real NAV, not aliased
    assert result.client is not None


def test_dead_when_api_keys_missing(isolated_paths, monkeypatch):
    monkeypatch.setattr(vix_session, "_build_client", lambda: None)

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "ALPACA_API_KEY_ID" in result.reason


def test_dead_when_account_call_fails(isolated_paths, monkeypatch):
    _mock_client(monkeypatch, account_error=Exception("boom"))

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "account/equity check failed" in result.reason


def test_dead_when_account_blocked(isolated_paths, monkeypatch):
    _mock_client(monkeypatch, account=_FakeAccount(trading_blocked=True))

    result = vix_session.assess()

    assert result.state == vix_session.DEAD


def test_dead_when_positions_call_fails(isolated_paths, monkeypatch):
    _mock_client(monkeypatch, positions_error=Exception("boom"))

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "positions call failed" in result.reason


def test_empty_positions_with_recent_shadow_svix_is_book_mismatch(isolated_paths, monkeypatch):
    isolated_paths.shadow_book.write_text(json.dumps({
        "saved_at": time.time() - 600,  # 10 minutes old
        "positions": [{"ticker": "SVIX", "quantity": 10}],
    }))
    _mock_client(monkeypatch, positions=[])

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "BOOK_MISMATCH" in result.reason


def test_book_mismatch_refreshes_shadow_book_despite_dead(isolated_paths, monkeypatch):
    """Real bug found + fixed 2026-08-31: the shadow book used to only ever
    update on HEALTHY, so a single suspicious empty read could never be
    superseded, and every later assess() kept re-comparing against the SAME
    stale snapshot -- a self-perpetuating DEAD loop that only cleared once
    the original snapshot aged out (up to 1h), regardless of how many times
    reality had already confirmed flat in the meantime."""
    isolated_paths.shadow_book.write_text(json.dumps({
        "saved_at": time.time() - 600,
        "positions": [{"ticker": "SVIX", "quantity": 10}],
    }))
    _mock_client(monkeypatch, positions=[])

    result = vix_session.assess()
    assert result.state == vix_session.DEAD  # this cycle still pauses, unchanged

    saved_shadow = json.loads(isolated_paths.shadow_book.read_text())
    assert saved_shadow["positions"] == []  # but memory is now corrected


def test_a_second_assess_after_cooldown_goes_healthy_once_shadow_book_is_fresh(isolated_paths, monkeypatch):
    """End-to-end proof the loop is actually broken, not just that the file
    changed: simulate the cooldown having expired, then a second real
    assess() call resolves HEALTHY instead of re-triggering BOOK_MISMATCH."""
    isolated_paths.shadow_book.write_text(json.dumps({
        "saved_at": time.time() - 600,
        "positions": [{"ticker": "SVIX", "quantity": 10}],
    }))
    _mock_client(monkeypatch, positions=[])

    first = vix_session.assess()
    assert first.state == vix_session.DEAD

    # Simulate the 15-minute cooldown having elapsed (without this, the
    # second call would short-circuit on cooldown before ever re-checking
    # the shadow book at all, and the test would prove nothing new).
    state = json.loads(isolated_paths.session_state.read_text())
    state["saved_at"] = time.time() - 1000
    isolated_paths.session_state.write_text(json.dumps(state))

    second = vix_session.assess()
    assert second.state == vix_session.HEALTHY


def test_empty_positions_no_shadow_is_healthy_and_flat(isolated_paths, monkeypatch):
    _mock_client(monkeypatch, positions=[])

    result = vix_session.assess()

    assert result.state == vix_session.HEALTHY


def test_cooldown_blocks_immediate_retry_after_dead(isolated_paths, monkeypatch):
    isolated_paths.session_state.write_text(json.dumps({"state": "DEAD", "saved_at": time.time()}))

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "cooldown" in result.reason


def test_healthy_saves_shadow_book_from_positions(isolated_paths, monkeypatch):
    _mock_client(monkeypatch, positions=[_FakePosition("SVIX", "10")])

    vix_session.assess()

    saved = json.loads(isolated_paths.shadow_book.read_text())
    assert saved["positions"] == [{"ticker": "SVIX", "quantity": "10"}]
