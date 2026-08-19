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
    def __init__(self, buying_power="1000.0", status="ACTIVE", trading_blocked=False, account_blocked=False):
        self.buying_power = buying_power
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
