"""
data/market_data.py — MarketData adapter (UW_OFF_ALPACA_CUTOVER_PLAN.md P0).
No network: the "uw" backend's underlying UnusualWhalesClient is
monkeypatched out via data.unusual_whales.get_client.
"""
import json

import pytest

from data import market_data


class _FakeUWClient:
    def __init__(self):
        self.calls = []

    def last_price(self, ticker):
        self.calls.append(("last_price", ticker))
        return 28.5 if ticker == "SVIX" else None

    def ohlc(self, ticker, candle_size="1d", **params):
        self.calls.append(("ohlc", ticker, candle_size))
        return {"data": [{"close": "17.0", "market_time": "r"}]}

    def vix_term(self):
        self.calls.append(("vix_term",))
        return {"vix": 18.0, "vix3m": 19.0, "source": "synthetic_put_call_parity"}

    def option_chain(self, ticker, greeks=True):
        self.calls.append(("option_chain", ticker, greeks))
        return {"data": []}


@pytest.fixture(autouse=True)
def _reset_singleton():
    """The module-level singleton must not leak between tests."""
    market_data._client = None
    market_data._client_backend_name = None
    yield
    market_data._client = None
    market_data._client_backend_name = None


@pytest.fixture
def fake_uw(monkeypatch):
    fake = _FakeUWClient()
    monkeypatch.setattr("data.unusual_whales.get_client", lambda: fake)
    return fake


def test_default_backend_is_uw(fake_uw, monkeypatch):
    monkeypatch.delenv("DATA_BACKEND", raising=False)
    client = market_data.get_client()
    assert client.name == "uw"


def test_uw_backend_delegates_last_price(fake_uw):
    client = market_data.get_client("uw")
    assert client.last_price("SVIX") == 28.5
    assert ("last_price", "SVIX") in fake_uw.calls


def test_uw_backend_delegates_ohlc_unchanged_shape(fake_uw):
    client = market_data.get_client("uw")
    payload = client.ohlc("UVXY", candle_size="1d")
    assert payload == {"data": [{"close": "17.0", "market_time": "r"}]}


def test_uw_backend_delegates_vix_term(fake_uw):
    client = market_data.get_client("uw")
    term = client.vix_term()
    assert term["vix"] == 18.0 and term["vix3m"] == 19.0


def test_uw_backend_delegates_option_chain(fake_uw):
    client = market_data.get_client("uw")
    client.option_chain("VXX", greeks=True)
    assert ("option_chain", "VXX", True) in fake_uw.calls


def test_unknown_backend_raises_market_data_error(fake_uw):
    with pytest.raises(market_data.MarketDataError):
        market_data.get_client("alpaca_fred")  # not implemented until P1


def test_singleton_reused_for_same_backend(fake_uw):
    a = market_data.get_client("uw")
    b = market_data.get_client("uw")
    assert a is b


def test_env_var_selects_backend(fake_uw, monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "uw")
    client = market_data.get_client()
    assert client.name == "uw"


# ── dual backend (P1 shadow) ────────────────────────────────────────────

class _FakeAlpaca:
    name = "alpaca"

    def __init__(self, price=28.6, boom=False):
        self._price = price
        self._boom = boom
        self.calls = []

    def last_price(self, ticker):
        self.calls.append(ticker)
        if self._boom:
            raise market_data.MarketDataError("alpaca down")
        return self._price

    def ohlc(self, ticker, candle_size="1d", **params):
        if self._boom:
            raise market_data.MarketDataError("alpaca ohlc down")
        return {"data": [{"close": str(self._price), "market_time": "r"}]}


@pytest.fixture
def dual_client(fake_uw, monkeypatch, tmp_path):
    monkeypatch.setattr("data.market_data._shadow_log_path", lambda: tmp_path / "shadow.jsonl")
    return market_data.get_client("dual"), tmp_path / "shadow.jsonl"


def test_dual_backend_last_price_returns_alpaca_value_not_uw(dual_client, monkeypatch):
    client, _ = dual_client
    monkeypatch.setattr(client, "_secondary", _FakeAlpaca(price=99.0))
    # fake UW returns 28.5 for SVIX (see _FakeUWClient) -- last_price() was
    # flipped to Alpaca-authoritative, so dual must return 99.0, not 28.5.
    assert client.last_price("SVIX") == 99.0


def test_dual_backend_last_price_logs_uw_as_the_comparison_side(dual_client, monkeypatch):
    client, log_path = dual_client
    monkeypatch.setattr(client, "_secondary", _FakeAlpaca(price=29.925))  # +5% vs UW's 28.5
    client.last_price("SVIX")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["method"] == "last_price" and row["ticker"] == "SVIX"
    # alpaca is now authoritative/returned; uw is the comparison
    assert row["returned_value"] == 29.925 and row["returned_source"] == "alpaca"
    assert row["compared_value"] == 28.5 and row["compared_source"] == "uw"
    assert row["diff_pct"] == pytest.approx((28.5 - 29.925) / 29.925)
    assert row["compared_error"] is None


def test_dual_backend_last_price_fails_closed_on_alpaca_failure_not_uw_fallback(dual_client, monkeypatch):
    client, log_path = dual_client
    monkeypatch.setattr(client, "_secondary", _FakeAlpaca(boom=True))
    val = client.last_price("SVIX")
    # must fail CLOSED (None), never fall back to UW
    assert val is None
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["returned_value"] is None and row["returned_source"] == "alpaca"
    assert "alpaca down" in row["compared_error"]


def test_dual_backend_ohlc_returns_alpaca_value_not_uw(dual_client, monkeypatch):
    client, _ = dual_client
    monkeypatch.setattr(client, "_secondary", _FakeAlpaca(price=17.53))
    # fake UW's ohlc always returns close "17.0" (see _FakeUWClient) -- dual
    # must return Alpaca's 17.53, since ohlc() was flipped to Alpaca-authoritative.
    payload = client.ohlc("UVXY")
    assert payload == {"data": [{"close": "17.53", "market_time": "r"}]}


def test_dual_backend_ohlc_logs_uw_as_the_comparison_side(dual_client, monkeypatch):
    client, log_path = dual_client
    monkeypatch.setattr(client, "_secondary", _FakeAlpaca(price=17.53))
    client.ohlc("UVXY")
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["method"] == "ohlc_last_close" and row["ticker"] == "UVXY"
    # ohlc: Alpaca is now authoritative/returned, UW is the comparison
    assert row["returned_value"] == pytest.approx(17.53) and row["returned_source"] == "alpaca"
    assert row["compared_value"] == pytest.approx(17.0) and row["compared_source"] == "uw"
    assert row["compared_error"] is None


def test_dual_backend_ohlc_fails_closed_on_alpaca_failure_not_uw_fallback(dual_client, monkeypatch):
    client, log_path = dual_client
    monkeypatch.setattr(client, "_secondary", _FakeAlpaca(boom=True))
    payload = client.ohlc("UVXY")
    # must fail CLOSED (empty), never fall back to UW's known-stale data
    assert payload == {"data": []}
    row = json.loads(log_path.read_text().splitlines()[0])
    assert "Alpaca" in row["compared_error"] or "alpaca" in row["compared_error"]


def test_dual_backend_vix_term_returns_fred_yahoo_value_not_uw(dual_client, monkeypatch):
    # vix_term() was flipped to FRED/Yahoo-authoritative -- must return
    # 14.81/18.29, NOT the fake UW's 18.0/19.0.
    client, _ = dual_client
    fake_source = type("FakeSource", (), {"vix_term": staticmethod(
        lambda: {"vix": 14.81, "vix3m": 18.29, "source": "yahoo_intraday", "error": None,
                 "warning": None, "fetched_at": 0.0}
    )})()
    monkeypatch.setattr(client, "_term_source", fake_source)
    term = client.vix_term()
    assert term["vix"] == 14.81 and term["vix3m"] == 18.29


def test_dual_backend_vix_term_logs_uw_as_the_comparison_side(dual_client, monkeypatch):
    client, log_path = dual_client
    fake_source = type("FakeSource", (), {"vix_term": staticmethod(
        lambda: {"vix": 14.81, "vix3m": 18.29, "source": "yahoo_intraday", "error": None,
                 "warning": None, "fetched_at": 0.0}
    )})()
    monkeypatch.setattr(client, "_term_source", fake_source)
    client.vix_term()
    lines = [json.loads(l) for l in log_path.read_text().splitlines()]
    vix_row = next(r for r in lines if r["method"] == "vix_term_vix")
    vix3m_row = next(r for r in lines if r["method"] == "vix_term_vix3m")
    # fred_yahoo is now authoritative/returned; uw is the comparison
    assert vix_row["returned_value"] == 14.81 and vix_row["returned_source"] == "fred_yahoo"
    assert vix_row["compared_value"] == 18.0 and vix_row["compared_source"] == "uw"
    assert vix3m_row["returned_value"] == 18.29 and vix3m_row["compared_value"] == 19.0


def test_dual_backend_vix_term_fails_closed_on_fred_yahoo_failure_not_uw_fallback(dual_client, monkeypatch):
    client, log_path = dual_client
    fake_source = type("FakeSource", (), {"vix_term": staticmethod(
        lambda: (_ for _ in ()).throw(RuntimeError("fred+yahoo both down"))
    )})()
    monkeypatch.setattr(client, "_term_source", fake_source)
    term = client.vix_term()
    # must fail CLOSED (None), never fall back to UW's proven-unreliable estimate
    assert term["vix"] is None and term["vix3m"] is None
    assert "fred+yahoo both down" in term["error"]
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["returned_value"] is None and row["returned_source"] == "fred_yahoo"
    assert row["compared_value"] == 18.0 and row["compared_source"] == "uw"  # UW comparison still logged


def test_dual_backend_option_chain_passes_through_to_uw_unchanged(dual_client):
    client, _ = dual_client
    assert client.option_chain("VXX") == {"data": []}


def test_dual_backend_secondary_unavailable_last_price_fails_closed(fake_uw, monkeypatch, tmp_path):
    monkeypatch.setattr("data.market_data._shadow_log_path", lambda: tmp_path / "shadow.jsonl")
    monkeypatch.setattr(
        "data.alpaca_quotes.AlpacaQuotes",
        lambda: (_ for _ in ()).throw(market_data.MarketDataError("no creds")),
    )
    client = market_data.get_client("dual")
    assert client._secondary is None
    # last_price is Alpaca-authoritative -- with no Alpaca available it
    # must fail closed, NOT silently fall back to UW.
    assert client.last_price("SVIX") is None


def test_dual_backend_secondary_unavailable_ohlc_fails_closed(fake_uw, monkeypatch, tmp_path):
    monkeypatch.setattr("data.market_data._shadow_log_path", lambda: tmp_path / "shadow.jsonl")
    monkeypatch.setattr(
        "data.alpaca_quotes.AlpacaQuotes",
        lambda: (_ for _ in ()).throw(market_data.MarketDataError("no creds")),
    )
    client = market_data.get_client("dual")
    # ohlc is Alpaca-authoritative -- with no Alpaca available it must fail
    # closed, NOT silently fall back to UW's known-stale ohlc data.
    assert client.ohlc("UVXY") == {"data": []}
