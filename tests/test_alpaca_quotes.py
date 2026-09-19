"""
data/alpaca_quotes.py — Alpaca-backed last_price()/ohlc() (P1). No network:
the alpaca-py StockHistoricalDataClient is monkeypatched out.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from data import market_data


class _FakeTrade:
    def __init__(self, price):
        self.price = price


class _FakeBar:
    def __init__(self, close, ts):
        self.close = close
        self.timestamp = ts


class _FakeBarSet(dict):
    """Mimics alpaca-py's BarSet: dict-like + a .data attribute."""
    def __init__(self, data):
        super().__init__(data)
        self.data = data


@pytest.fixture
def alpaca_quotes(monkeypatch):
    from data.alpaca_quotes import AlpacaQuotes

    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        "alpaca.data.historical.StockHistoricalDataClient", lambda key, secret: SimpleNamespace()
    )
    return AlpacaQuotes()


def test_missing_credentials_raises_market_data_error(monkeypatch):
    from data.alpaca_quotes import AlpacaQuotes

    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    with pytest.raises(market_data.MarketDataError):
        AlpacaQuotes()


def test_last_price_returns_float(alpaca_quotes):
    alpaca_quotes._client.get_stock_latest_trade = lambda req: {"SVIX": _FakeTrade(28.5)}
    assert alpaca_quotes.last_price("SVIX") == 28.5


def test_last_price_wraps_failures_in_market_data_error(alpaca_quotes):
    def _boom(req):
        raise RuntimeError("network down")
    alpaca_quotes._client.get_stock_latest_trade = _boom
    with pytest.raises(market_data.MarketDataError):
        alpaca_quotes.last_price("SVIX")


def test_ohlc_reshapes_into_uw_payload_shape(alpaca_quotes):
    bars = _FakeBarSet({"UVXY": [
        _FakeBar(17.0, datetime(2026, 9, 17, tzinfo=timezone.utc)),
        _FakeBar(17.5, datetime(2026, 9, 18, tzinfo=timezone.utc)),
    ]})
    alpaca_quotes._client.get_stock_bars = lambda req: bars
    payload = alpaca_quotes.ohlc("UVXY", candle_size="1d")
    assert payload == {"data": [
        {"close": "17.0", "market_time": "r"},
        {"close": "17.5", "market_time": "r"},
    ]}


def test_ohlc_rejects_non_daily_candle_size(alpaca_quotes):
    with pytest.raises(market_data.MarketDataError):
        alpaca_quotes.ohlc("UVXY", candle_size="1h")


def test_ohlc_empty_when_symbol_missing_from_response(alpaca_quotes):
    alpaca_quotes._client.get_stock_bars = lambda req: _FakeBarSet({})
    assert alpaca_quotes.ohlc("UVXY") == {"data": []}
