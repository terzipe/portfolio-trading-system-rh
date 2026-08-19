"""
monitor/vix_ledger.py — Alpaca "gold copy" balance/P&L for the dashboard.
Every function fails closed (empty/None on API error, never raises), same
convention as vix_options.get_contract_mark(). No network in CI —
everything below is mocked.
"""
from types import SimpleNamespace

import pytest

from monitor import vix_ledger


class _FakeAccount:
    def __init__(self, equity="1000.0", cash="500.0", portfolio_value="1000.0", buying_power="2000.0"):
        self.equity = equity
        self.cash = cash
        self.portfolio_value = portfolio_value
        self.buying_power = buying_power


class _FakePosition:
    def __init__(self, symbol, qty, market_value, unrealized_pl, unrealized_plpc, cost_basis):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value
        self.unrealized_pl = unrealized_pl
        self.unrealized_plpc = unrealized_plpc
        self.cost_basis = cost_basis


class _FakeClient:
    def __init__(self, account=None, positions=None, account_error=None, positions_error=None, activity_pages=None, activities_error=None):
        self._account = account
        self._positions = positions if positions is not None else []
        self._account_error = account_error
        self._positions_error = positions_error
        self._activity_pages = list(activity_pages or [])
        self._activities_error = activities_error
        self.get_calls = []

    def get_account(self):
        if self._account_error:
            raise self._account_error
        return self._account

    def get_all_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions

    def get(self, path, data=None):
        self.get_calls.append((path, data))
        if self._activities_error:
            raise self._activities_error
        if not self._activity_pages:
            return []
        return self._activity_pages.pop(0)


# ── account_snapshot ─────────────────────────────────────────────────────

def test_account_snapshot_float_casts_fields():
    client = _FakeClient(account=_FakeAccount())
    snap = vix_ledger.account_snapshot(client)
    assert snap == {"equity": 1000.0, "cash": 500.0, "portfolio_value": 1000.0, "buying_power": 2000.0}


def test_account_snapshot_fails_closed_on_error():
    client = _FakeClient(account_error=Exception("boom"))
    assert vix_ledger.account_snapshot(client) is None


# ── positions_snapshot ───────────────────────────────────────────────────

def test_positions_snapshot_sums_unrealized_across_positions():
    client = _FakeClient(positions=[
        _FakePosition("SVIX", "10", "270.0", "10.0", "0.038", "260.0"),
        _FakePosition("UVXY", "2", "230.0", "-15.0", "-0.061", "245.0"),
    ])
    snap = vix_ledger.positions_snapshot(client)
    assert snap["total_unrealized_dollars"] == pytest.approx(-5.0)
    assert snap["by_ticker"]["SVIX"]["unrealized_pl"] == 10.0
    assert snap["by_ticker"]["UVXY"]["qty"] == 2.0


def test_positions_snapshot_fails_closed_on_error():
    client = _FakeClient(positions_error=Exception("boom"))
    assert vix_ledger.positions_snapshot(client) is None


def test_positions_snapshot_empty_when_flat():
    client = _FakeClient(positions=[])
    snap = vix_ledger.positions_snapshot(client)
    assert snap == {"by_ticker": {}, "total_unrealized_dollars": 0.0}


# ── fetch_fill_activities ────────────────────────────────────────────────

def test_fetch_fill_activities_normalizes_and_sorts_oldest_first():
    client = _FakeClient(activity_pages=[[
        {"id": "2", "symbol": "SVIX", "side": "sell", "qty": "5", "price": "27.0", "transaction_time": "2026-08-19T15:00:00Z", "order_id": "o2"},
        {"id": "1", "symbol": "SVIX", "side": "buy", "qty": "10", "price": "26.0", "transaction_time": "2026-08-19T09:30:00Z", "order_id": "o1"},
    ]])
    activities = vix_ledger.fetch_fill_activities(client)
    assert [a["order_id"] for a in activities] == ["o1", "o2"]
    assert activities[0]["qty"] == 10.0 and activities[0]["side"] == "buy"


def test_fetch_fill_activities_paginates_until_short_page():
    full_page = [
        {"id": str(i), "symbol": "SVIX", "side": "buy", "qty": "1", "price": "26.0", "transaction_time": f"2026-08-19T09:{i:02d}:00Z", "order_id": f"o{i}"}
        for i in range(100)
    ]
    short_page = [
        {"id": "100", "symbol": "SVIX", "side": "buy", "qty": "1", "price": "26.0", "transaction_time": "2026-08-19T11:00:00Z", "order_id": "o100"},
    ]
    client = _FakeClient(activity_pages=[full_page, short_page])
    activities = vix_ledger.fetch_fill_activities(client)
    assert len(activities) == 101
    assert len(client.get_calls) == 2
    assert client.get_calls[1][1]["page_token"] == "99"  # last id of the first (full) page


def test_fetch_fill_activities_fails_closed_on_error():
    client = _FakeClient(activities_error=Exception("boom"))
    assert vix_ledger.fetch_fill_activities(client) == []


def test_fetch_fill_activities_skips_rows_missing_required_fields():
    client = _FakeClient(activity_pages=[[
        {"id": "1", "symbol": "SVIX", "side": "buy", "qty": "10", "price": "26.0", "transaction_time": "t", "order_id": "o1"},
        {"id": "2", "symbol": None, "side": "buy", "qty": "10", "price": "26.0"},  # malformed, no symbol
    ]])
    activities = vix_ledger.fetch_fill_activities(client)
    assert len(activities) == 1


# ── fifo_realized_pnl ────────────────────────────────────────────────────

def test_fifo_realized_pnl_single_lot_full_close():
    activities = [
        {"symbol": "SVIX", "side": "buy", "qty": 10.0, "price": 26.0},
        {"symbol": "SVIX", "side": "sell", "qty": 10.0, "price": 27.0},
    ]
    result = vix_ledger.fifo_realized_pnl(activities)
    assert result["total_dollars"] == pytest.approx(10.0)  # (27-26)*10
    assert result["by_ticker"]["SVIX"] == pytest.approx(10.0)
    assert result["matched_trades"] == 1


def test_fifo_realized_pnl_multi_lot_fifo_order():
    activities = [
        {"symbol": "UVXY", "side": "buy", "qty": 2.0, "price": 100.0},
        {"symbol": "UVXY", "side": "buy", "qty": 3.0, "price": 110.0},
        {"symbol": "UVXY", "side": "sell", "qty": 4.0, "price": 120.0},
    ]
    result = vix_ledger.fifo_realized_pnl(activities)
    # First 2 from lot1 (100): 2*(120-100)=40; next 2 from lot2 (110): 2*(120-110)=20
    assert result["total_dollars"] == pytest.approx(60.0)
    assert result["matched_trades"] == 2


def test_fifo_realized_pnl_partial_fill_leaves_remaining_lot():
    activities = [
        {"symbol": "SVIX", "side": "buy", "qty": 10.0, "price": 26.0},
        {"symbol": "SVIX", "side": "sell", "qty": 4.0, "price": 27.0},
    ]
    result = vix_ledger.fifo_realized_pnl(activities)
    assert result["total_dollars"] == pytest.approx(4.0)  # (27-26)*4, 6 shares still open


def test_fifo_realized_pnl_no_activities_is_zero():
    result = vix_ledger.fifo_realized_pnl([])
    assert result == {"total_dollars": 0.0, "by_ticker": {}, "matched_trades": 0}


def test_fifo_realized_pnl_ignores_symbol_with_no_matching_buy():
    # Sells with no prior lot (e.g. a position opened before this account's
    # tracked history) contribute nothing rather than crashing.
    activities = [{"symbol": "SVIX", "side": "sell", "qty": 5.0, "price": 27.0}]
    result = vix_ledger.fifo_realized_pnl(activities)
    assert result == {"total_dollars": 0.0, "by_ticker": {}, "matched_trades": 0}
