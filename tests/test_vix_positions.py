from types import SimpleNamespace

import pytest
from alpaca.trading.enums import AssetClass

from monitor.vix_positions import unrealized_pnl, fetch_positions, _parse_occ_symbol


def test_unrealized_pnl_shares_only():
    positions = [
        {"ticker": "SVIX", "type": "share", "quantity": 2.0, "cost_basis": 26.0, "mid_price": 27.0},
    ]
    result = unrealized_pnl(positions)
    assert result["total_dollars"] == 2.0  # (27-26)*2
    assert result["total_pct"] == pytest.approx(2.0 / 52.0)
    assert "SVIX" in result["by_ticker"]


def test_unrealized_pnl_includes_options_now_that_mark_is_wired():
    positions = [
        {
            "ticker": "UVXY", "type": "option", "contracts": 2.0, "cost_basis": 115.0,
            "mid_price": 130.0, "pnl_pct": 130 / 115 - 1,
            "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
        },
    ]
    result = unrealized_pnl(positions)
    assert result["total_dollars"] == 30.0  # (130-115)*2
    key = "UVXY 2099-01-15 54.0p"
    assert key in result["by_ticker"]
    assert result["by_ticker"][key]["quantity"] == 2.0


def test_unrealized_pnl_skips_positions_with_no_mark():
    positions = [
        {"ticker": "SVIX", "type": "share", "quantity": 2.0, "cost_basis": 26.0, "mid_price": None},
        {
            "ticker": "UVXY", "type": "option", "contracts": 1.0, "cost_basis": 100.0,
            "mid_price": None, "pnl_pct": None,
            "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
        },
    ]
    result = unrealized_pnl(positions)
    assert result["total_dollars"] == 0.0
    assert result["by_ticker"] == {}


def test_unrealized_pnl_mixed_shares_and_options():
    positions = [
        {"ticker": "SVIX", "type": "share", "quantity": 10.0, "cost_basis": 26.0, "mid_price": 27.0},
        {
            "ticker": "UVXY", "type": "option", "contracts": 2.0, "cost_basis": 115.0,
            "mid_price": 100.0, "pnl_pct": 100 / 115 - 1,
            "expiry": "2099-01-15", "strike": 54.0, "option_type": "put",
        },
    ]
    result = unrealized_pnl(positions)
    # shares: (27-26)*10 = 10; options: (100-115)*2 = -30
    assert result["total_dollars"] == -20.0
    assert len(result["by_ticker"]) == 2


# ── OCC symbol parsing (Alpaca migration) ───────────────────────────────

def test_parse_occ_symbol_put():
    assert _parse_occ_symbol("UVXY260115P00054000") == ("UVXY", "2026-01-15", 54.0, "put")


def test_parse_occ_symbol_call():
    assert _parse_occ_symbol("VXX260219C00020000") == ("VXX", "2026-02-19", 20.0, "call")


def test_parse_occ_symbol_multi_digit_strike():
    assert _parse_occ_symbol("SVIX260115C00123450") == ("SVIX", "2026-01-15", 123.45, "call")


def test_parse_occ_symbol_rejects_malformed():
    assert _parse_occ_symbol("bad") is None
    assert _parse_occ_symbol("UVXY260115X00054000") is None  # not C/P


# ── fetch_positions() against a fake Alpaca client ──────────────────────

class _FakePosition:
    def __init__(self, asset_class, symbol, qty, avg_entry_price):
        self.asset_class = asset_class
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price


class _FakeAlpacaClient:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


class _FakeUW:
    def __init__(self, price=None):
        self._price = price

    def last_price(self, ticker):
        return self._price


def test_fetch_positions_share(monkeypatch):
    import monitor.vix_positions as vp
    monkeypatch.setattr(vp, "get_client", lambda: _FakeUW(price=27.0))
    client = _FakeAlpacaClient([_FakePosition(AssetClass.US_EQUITY, "SVIX", "10", "26.0")])

    positions = fetch_positions(client)

    assert len(positions) == 1
    p = positions[0]
    assert p["ticker"] == "SVIX" and p["type"] == "share" and p["quantity"] == 10.0
    assert p["cost_basis"] == 26.0 and p["mid_price"] == 27.0


def test_fetch_positions_ignores_non_vol_equity(monkeypatch):
    import monitor.vix_positions as vp
    monkeypatch.setattr(vp, "get_client", lambda: _FakeUW(price=100.0))
    client = _FakeAlpacaClient([_FakePosition(AssetClass.US_EQUITY, "AAPL", "5", "150.0")])

    positions = fetch_positions(client)

    assert positions == []


def test_fetch_positions_option_parses_occ_and_gets_mark(monkeypatch):
    import monitor.vix_positions as vp
    monkeypatch.setattr(vp, "get_client", lambda: _FakeUW())
    monkeypatch.setattr(vp.vix_options, "get_contract_mark", lambda ticker, expiry, strike, option_type: 1.15)
    client = _FakeAlpacaClient([_FakePosition(AssetClass.US_OPTION, "UVXY260115P00054000", "2", "1.10")])

    positions = fetch_positions(client)

    assert len(positions) == 1
    p = positions[0]
    assert p["ticker"] == "UVXY" and p["type"] == "option"
    assert p["expiry"] == "2026-01-15" and p["strike"] == 54.0 and p["option_type"] == "put"
    assert p["contracts"] == 2.0
    assert p["cost_basis"] == pytest.approx(110.0)  # 1.10 * 100
    assert p["mid_price"] == pytest.approx(115.0)  # 1.15 * 100
    assert p["pnl_pct"] == pytest.approx((115.0 - 110.0) / 110.0)


def test_fetch_positions_option_ignores_non_vol_underlying(monkeypatch):
    import monitor.vix_positions as vp
    monkeypatch.setattr(vp, "get_client", lambda: _FakeUW())
    client = _FakeAlpacaClient([_FakePosition(AssetClass.US_OPTION, "AAPL260115P00150000", "1", "5.0")])

    positions = fetch_positions(client)

    assert positions == []


def test_fetch_positions_fails_closed_on_client_error(monkeypatch):
    import monitor.vix_positions as vp
    monkeypatch.setattr(vp, "get_client", lambda: _FakeUW())

    class _FailingClient:
        def get_all_positions(self):
            raise Exception("boom")

    positions = fetch_positions(_FailingClient())

    assert positions == []
