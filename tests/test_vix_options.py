"""
get_contract_mark() regression tests — this closes the gap that used to
leave option positions' pnl_pct permanently None (vix_positions.py), which
blocked ROLL_OPTION/CLOSE_OPTION from ever firing and kept unrealized P&L
scoped to shares only.
"""
import json
from pathlib import Path

import pytest

from monitor import vix_options

_FIXTURE = Path(__file__).parent / "fixtures" / "uw" / "uvxy_chain.json"


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
