"""
S0 acceptance fixtures for monitor/vix_session.py (Impl Plan §2A):
  - expired JWT + good refresh -> HEALTHY, pickle updated once
  - 429 on refresh -> DEAD, zero rh.login() calls
  - empty positions + shadow SVIX 10 min old -> BOOK_MISMATCH, DEAD
  - empty positions + empty/old shadow + all other checks pass -> HEALTHY, actually flat

No network in CI — everything below is mocked.
"""
import base64
import json
import pickle
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from monitor import vix_session


def _fake_jwt(exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    pickle_path = tmp_path / "robinhood.pickle"
    session_state = tmp_path / "session_state.json"
    shadow_book = tmp_path / "last_known_positions.json"
    monkeypatch.setattr(vix_session, "_PICKLE", pickle_path)
    monkeypatch.setattr(vix_session, "VIX_SESSION_STATE_FILE", session_state)
    monkeypatch.setattr(vix_session, "VIX_SHADOW_BOOK_FILE", shadow_book)
    return SimpleNamespace(pickle=pickle_path, session_state=session_state, shadow_book=shadow_book)


def _write_pickle(path, access_exp: float, refresh: str = "r1", device: str = "d1"):
    tok = {"access_token": _fake_jwt(access_exp), "refresh_token": refresh, "device_token": device}
    with open(path, "wb") as f:
        pickle.dump(tok, f)


def _mock_rh(monkeypatch, equity="1000.0", account_number="725024723", positions=None):
    import robin_stocks.robinhood as rh_real

    login_mock = MagicMock()
    monkeypatch.setattr(rh_real, "login", login_mock)
    monkeypatch.setattr(
        rh_real, "load_portfolio_profile",
        lambda account_number=None: {"equity": equity, "account_number": account_number},
    )
    monkeypatch.setattr(
        rh_real, "get_open_stock_positions",
        lambda account_number=None: positions if positions is not None else [],
    )
    return login_mock


def test_expired_jwt_good_refresh_healthy(isolated_paths, monkeypatch):
    _write_pickle(isolated_paths.pickle, access_exp=time.time() - 3600)
    new_access = _fake_jwt(time.time() + 3600)

    class FakeResp:
        status_code = 200
        def json(self):
            return {"access_token": new_access, "refresh_token": "r2"}

    monkeypatch.setattr(vix_session.requests, "post", lambda *a, **k: FakeResp())
    login_mock = _mock_rh(monkeypatch)

    result = vix_session.assess()

    assert result.state == vix_session.HEALTHY
    assert login_mock.call_count == 0
    saved = pickle.load(open(isolated_paths.pickle, "rb"))
    assert saved["access_token"] == new_access


def test_429_on_refresh_is_dead_no_login(isolated_paths, monkeypatch):
    _write_pickle(isolated_paths.pickle, access_exp=time.time() - 3600)

    class FakeResp:
        status_code = 429
        def json(self):
            return {}

    monkeypatch.setattr(vix_session.requests, "post", lambda *a, **k: FakeResp())
    login_mock = _mock_rh(monkeypatch)

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert login_mock.call_count == 0


def test_empty_positions_with_recent_shadow_svix_is_book_mismatch(isolated_paths, monkeypatch):
    _write_pickle(isolated_paths.pickle, access_exp=time.time() + 3600)
    isolated_paths.shadow_book.write_text(json.dumps({
        "saved_at": time.time() - 600,  # 10 minutes old
        "positions": [{"ticker": "SVIX", "quantity": 10}],
    }))
    _mock_rh(monkeypatch, positions=[])

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "BOOK_MISMATCH" in result.reason


def test_empty_positions_no_shadow_is_healthy_and_flat(isolated_paths, monkeypatch):
    _write_pickle(isolated_paths.pickle, access_exp=time.time() + 3600)
    _mock_rh(monkeypatch, positions=[])

    result = vix_session.assess()

    assert result.state == vix_session.HEALTHY


def test_cooldown_blocks_immediate_retry_after_dead(isolated_paths, monkeypatch):
    isolated_paths.session_state.write_text(json.dumps({"state": "DEAD", "saved_at": time.time()}))

    result = vix_session.assess()

    assert result.state == vix_session.DEAD
    assert "cooldown" in result.reason
