"""
get_contract_mark() regression tests — this closes the gap that used to
leave option positions' pnl_pct permanently None (vix_positions.py), which
blocked ROLL_OPTION/CLOSE_OPTION from ever firing and kept unrealized P&L
scoped to shares only.
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from monitor import vix_options

_FIXTURE = Path(__file__).parent / "fixtures" / "uw" / "uvxy_chain.json"
_MID_DTE_EXPIRY = (date.today() + timedelta(days=30)).isoformat()  # within VIX_CALL_MIN/MAX_DTE (21-45)


class _FakeUW:
    def __init__(self, chain):
        self._chain = chain

    def option_chain(self, ticker, greeks=True):
        return self._chain


def test_get_contract_mark_finds_exact_match(monkeypatch):
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "54", "option_type": "put", "nbbo_bid": "1.10", "nbbo_ask": "1.20"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    mark = vix_options.get_contract_mark("UVXY", "2099-01-15", 54.0, "put")

    assert mark == 1.15  # (1.10+1.20)/2


def test_get_contract_mark_none_when_no_match(monkeypatch):
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "54", "option_type": "put", "nbbo_bid": "1.10", "nbbo_ask": "1.20"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    # Wrong strike
    assert vix_options.get_contract_mark("UVXY", "2099-01-15", 60.0, "put") is None
    # Wrong expiry
    assert vix_options.get_contract_mark("UVXY", "2099-02-19", 54.0, "put") is None
    # Wrong option_type
    assert vix_options.get_contract_mark("UVXY", "2099-01-15", 54.0, "call") is None


def test_get_contract_mark_none_on_zero_premium(monkeypatch):
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "18", "option_type": "put", "nbbo_bid": "0", "nbbo_ask": "0"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.get_contract_mark("VXX", "2099-01-15", 18.0, "put") is None


def test_get_contract_mark_none_on_chain_fetch_failure(monkeypatch):
    from data.unusual_whales import UWError

    class _FailingUW:
        def option_chain(self, ticker, greeks=True):
            raise UWError("boom")

    monkeypatch.setattr(vix_options, "get_client", lambda: _FailingUW())

    assert vix_options.get_contract_mark("UVXY", "2099-01-15", 54.0, "put") is None


def test_get_contract_mark_tolerates_strike_float_representation_drift(monkeypatch):
    # e.g. RH reports strike_price as 54.0 exactly, UW's string "54" parses
    # to the same float -- but guard the small tolerance regardless.
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "54.00", "option_type": "put", "nbbo_bid": "1.10", "nbbo_ask": "1.20"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.get_contract_mark("UVXY", "2099-01-15", 54.0, "put") == 1.15


def test_get_contract_quote_finds_exact_match(monkeypatch):
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "54", "option_type": "put", "nbbo_bid": "1.10", "nbbo_ask": "1.20"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.get_contract_quote("UVXY", "2099-01-15", 54.0, "put") == (1.10, 1.20)


def test_get_contract_quote_none_when_no_match(monkeypatch):
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "54", "option_type": "put", "nbbo_bid": "1.10", "nbbo_ask": "1.20"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.get_contract_quote("UVXY", "2099-01-15", 60.0, "put") is None


def test_get_contract_quote_none_on_chain_fetch_failure(monkeypatch):
    from data.unusual_whales import UWError

    class _FailingUW:
        def option_chain(self, ticker, greeks=True):
            raise UWError("boom")

    monkeypatch.setattr(vix_options, "get_client", lambda: _FailingUW())

    assert vix_options.get_contract_quote("UVXY", "2099-01-15", 54.0, "put") is None


def test_get_contract_quote_returns_raw_zero_premium_not_none(monkeypatch):
    # Unlike get_contract_mark() (which treats a zero-premium match as "no
    # mark"), get_contract_quote() returns the raw (0.0, 0.0) for a real
    # match with no market -- callers decide what that means for their own
    # use (e.g. vix_executor's close-leg re-quote falls back to a stale
    # price rather than submitting a $0.00 limit order).
    chain = {"data": [
        {"expires": "2099-01-15", "strike": "18", "option_type": "put", "nbbo_bid": "0", "nbbo_ask": "0"},
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.get_contract_quote("VXX", "2099-01-15", 18.0, "put") == (0.0, 0.0)


def test_get_contract_mark_against_real_captured_chain():
    """Integration-style: use the real UVXY chain fixture captured live
    2026-08-18, confirm a known real contract is found correctly."""
    chain = json.loads(_FIXTURE.read_text())

    class _RealFixtureUW:
        def option_chain(self, ticker, greeks=True):
            return chain

    import monitor.vix_options as vo
    original_get_client = vo.get_client
    vo.get_client = lambda: _RealFixtureUW()
    try:
        mark = vo.get_contract_mark("UVXY", "2027-01-15", 30.0, "call")
    finally:
        vo.get_client = original_get_client

    assert mark == pytest.approx(3.70)  # (3.30 + 4.10) / 2, from the captured fixture row


# ── pick_call() liquidity filtering (fixed 2026-08-26 — previously picked
# purely on delta proximity with no liquidity check at all) ────────────

def _call(strike, delta, bid, ask, oi=200):
    return {
        "expires": _MID_DTE_EXPIRY, "option_type": "call", "strike": str(strike),
        "delta": str(delta), "nbbo_bid": str(bid), "nbbo_ask": str(ask), "open_interest": oi,
    }


def test_pick_call_returns_the_best_delta_match_when_liquid(monkeypatch):
    chain = {"data": [
        _call(strike=20, delta=0.50, bid=1.00, ask=1.02, oi=500),  # mid_target=0.50, exact match, liquid
        _call(strike=25, delta=0.30, bid=1.00, ask=1.02, oi=500),  # off-target
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    pick = vix_options.pick_call("VXX")
    assert pick is not None
    assert pick.strike == 20.0


def test_pick_call_skips_an_illiquid_best_match_for_the_next_liquid_one(monkeypatch):
    chain = {"data": [
        # Best delta match (0.50, dead center) but a wide 20% spread -- illiquid.
        _call(strike=20, delta=0.50, bid=1.00, ask=1.20, oi=500),
        # Second-best delta match (0.45), tight spread and healthy OI -- liquid.
        _call(strike=22, delta=0.45, bid=1.00, ask=1.02, oi=500),
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    pick = vix_options.pick_call("VXX")
    assert pick is not None
    assert pick.strike == 22.0  # skipped the illiquid top pick


def test_pick_call_none_when_nothing_in_the_pool_is_liquid(monkeypatch):
    chain = {"data": [
        _call(strike=20, delta=0.50, bid=1.00, ask=1.20, oi=500),   # wide spread
        _call(strike=22, delta=0.45, bid=1.00, ask=1.02, oi=10),    # thin OI
    ]}
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.pick_call("VXX") is None


def test_pick_call_thin_open_interest_alone_is_illiquid(monkeypatch):
    chain = {"data": [_call(strike=20, delta=0.50, bid=1.00, ask=1.02, oi=10)]}  # tight spread, oi<50
    monkeypatch.setattr(vix_options, "get_client", lambda: _FakeUW(chain))

    assert vix_options.pick_call("VXX") is None


def test_pick_call_none_when_chain_fetch_fails(monkeypatch):
    class _FailingUW:
        def option_chain(self, ticker, greeks=True):
            raise vix_options.UWError("boom")

    monkeypatch.setattr(vix_options, "get_client", lambda: _FailingUW())
    assert vix_options.pick_call("VXX") is None
