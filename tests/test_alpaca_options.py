"""
data/alpaca_options.py — Alpaca-backed option_chain() (P3). No network:
alpaca-py's OptionHistoricalDataClient is monkeypatched out.
"""
from types import SimpleNamespace

import pytest

from data import market_data
from data.alpaca_options import AlpacaOptions, _parse_occ_symbol


def _snap(bid=None, ask=None, delta=None):
    quote = SimpleNamespace(bid_price=bid, ask_price=ask) if bid is not None or ask is not None else None
    greeks = SimpleNamespace(delta=delta) if delta is not None else None
    return SimpleNamespace(latest_quote=quote, greeks=greeks)


@pytest.fixture
def alpaca_options(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        "alpaca.data.historical.option.OptionHistoricalDataClient",
        lambda key, secret: SimpleNamespace(),
    )
    return AlpacaOptions()


def test_missing_credentials_raises_market_data_error(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    with pytest.raises(market_data.MarketDataError):
        AlpacaOptions()


@pytest.mark.parametrize("symbol,expiry,otype,strike", [
    ("UVXY261023P00037000", "2026-10-23", "put", 37.0),
    ("UVXY260925C00005000", "2026-09-25", "call", 5.0),
    ("VXX261016P00016500", "2026-10-16", "put", 16.5),
])
def test_parse_occ_symbol(symbol, expiry, otype, strike):
    assert _parse_occ_symbol(symbol) == (expiry, otype, strike)


def test_parse_occ_symbol_rejects_adjusted_root():
    # "UVXY1..." -- corporate-action-adjusted root, real live example that
    # motivated the root_symbol= server-side filter (see module docstring)
    assert _parse_occ_symbol("UVXY1270115P00017000") is None


def test_parse_occ_symbol_rejects_garbage():
    assert _parse_occ_symbol("not-a-symbol") is None


def test_option_chain_reshapes_into_uw_payload_shape(alpaca_options, monkeypatch):
    snapshots = {
        "UVXY261023P00037000": _snap(bid=18.96, ask=21.02, delta=-0.231),
        "UVXY260925C00005000": _snap(bid=8.10, ask=8.25),  # no greeks
    }
    alpaca_options._client.get_option_chain = lambda req: snapshots
    payload = alpaca_options.option_chain("UVXY")
    rows = {r["expires"] + r["option_type"]: r for r in payload["data"]}

    put = rows["2026-10-23put"]
    assert put["strike"] == 37.0 and put["nbbo_bid"] == 18.96 and put["nbbo_ask"] == 21.02
    assert put["delta"] == -0.231
    assert put["open_interest"] is None  # never available from this feed, see module docstring

    call = rows["2026-09-25call"]
    assert call["delta"] is None  # no greeks snapshot for this contract


def test_option_chain_skips_unparseable_symbols(alpaca_options, monkeypatch):
    snapshots = {
        "UVXY1270115P00017000": _snap(bid=1.0, ask=1.1),  # adjusted root -- should never reach here anyway
        "UVXY261023P00037000": _snap(bid=18.96, ask=21.02),
    }
    alpaca_options._client.get_option_chain = lambda req: snapshots
    payload = alpaca_options.option_chain("UVXY")
    assert len(payload["data"]) == 1


def test_option_chain_uses_root_symbol_filter(alpaca_options, monkeypatch):
    captured = {}

    def _fake_get_chain(req):
        captured["root_symbol"] = req.root_symbol
        captured["underlying_symbol"] = req.underlying_symbol
        return {}

    alpaca_options._client.get_option_chain = _fake_get_chain
    alpaca_options.option_chain("UVXY")
    assert captured["root_symbol"] == "UVXY"
    assert captured["underlying_symbol"] == "UVXY"


def test_option_chain_wraps_failures_in_market_data_error(alpaca_options, monkeypatch):
    def _boom(req):
        raise RuntimeError("network down")
    alpaca_options._client.get_option_chain = _boom
    with pytest.raises(market_data.MarketDataError):
        alpaca_options.option_chain("UVXY")


def test_option_chain_missing_quote_yields_none_bid_ask(alpaca_options, monkeypatch):
    snapshots = {"UVXY261023P00037000": _snap()}  # no quote at all
    alpaca_options._client.get_option_chain = lambda req: snapshots
    payload = alpaca_options.option_chain("UVXY")
    row = payload["data"][0]
    assert row["nbbo_bid"] is None and row["nbbo_ask"] is None
