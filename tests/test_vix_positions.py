import pytest

from monitor.vix_positions import unrealized_pnl


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
