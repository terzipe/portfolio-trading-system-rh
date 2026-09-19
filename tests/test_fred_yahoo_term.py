"""
data/fred_yahoo_term.py — FRED/Yahoo term structure (UW_OFF_ALPACA_CUTOVER_PLAN.md P2).
No network: yfinance.download and data.fred.fetch_dated_series are
monkeypatched out.
"""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from data import fred_yahoo_term as fyt
from data.fred_yahoo_term import FredYahooTermStructure


def _yf_frame(closes: list[float], end: datetime):
    """Bars ending AT `end` (i.e. the last row's timestamp == end) --
    mirrors real data, where the last row is never in the future."""
    idx = pd.date_range(end=end, periods=len(closes), freq="1min")
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.fixture
def fresh_yahoo(monkeypatch):
    """Both ^VIX and ^VIX3M succeed with a fresh (near-now) bar."""
    now = datetime.now(timezone.utc)

    def _download(ticker, period, interval, progress, auto_adjust):
        if ticker == "^VIX":
            return _yf_frame([18.0, 18.1, 18.2], now)
        if ticker == "^VIX3M":
            return _yf_frame([19.0, 19.1, 19.2], now)
        raise AssertionError(f"unexpected ticker {ticker}")

    monkeypatch.setattr("yfinance.download", _download)


@pytest.fixture
def broken_yahoo(monkeypatch):
    def _download(*a, **k):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr("yfinance.download", _download)


@pytest.fixture
def fred_fallback(monkeypatch):
    def _fetch(series_id, lookback_years, end=None):
        if series_id == "VIXCLS":
            return [(date(2026, 9, 17), 15.44), (date(2026, 9, 18), 14.81)]
        if series_id == "VXVCLS":
            return [(date(2026, 9, 17), 18.55), (date(2026, 9, 18), 18.29)]
        raise AssertionError(f"unexpected series {series_id}")
    monkeypatch.setattr("data.fred.fetch_dated_series", _fetch)


def test_yahoo_intraday_used_when_both_tickers_succeed(fresh_yahoo):
    term = FredYahooTermStructure().vix_term()
    assert term["source"] == "yahoo_intraday"
    assert term["vix"] == pytest.approx(18.2)
    assert term["vix3m"] == pytest.approx(19.2)
    assert term["error"] is None
    assert term["warning"] is None


def test_yahoo_data_age_reflects_the_bars_own_timestamp(fresh_yahoo):
    import time
    term = FredYahooTermStructure().vix_term()
    age = time.time() - term["fetched_at"]
    assert 0 <= age < 30  # fresh_yahoo's bars are timestamped "now"


def test_falls_back_to_fred_when_yahoo_fails(broken_yahoo, fred_fallback):
    term = FredYahooTermStructure().vix_term()
    assert term["source"] == "fred_daily_close"
    assert term["vix"] == pytest.approx(14.81)
    assert term["vix3m"] == pytest.approx(18.29)
    assert term["error"] is None
    assert "Yahoo intraday" in term["warning"]


def test_fred_fallback_data_age_reflects_the_observation_date_not_now(broken_yahoo, fred_fallback):
    import time
    term = FredYahooTermStructure().vix_term()
    age_days = (time.time() - term["fetched_at"]) / 86400
    # 2026-09-18 close vs "now" (this test suite runs well after that date)
    assert age_days > 0.5


def test_both_sources_failing_sets_error_not_raise(broken_yahoo, monkeypatch):
    def _fetch(*a, **k):
        from data.fred import FredError
        raise FredError("fred down too")
    monkeypatch.setattr("data.fred.fetch_dated_series", _fetch)
    term = FredYahooTermStructure().vix_term()
    assert term["vix"] is None and term["vix3m"] is None
    assert term["error"] is not None
    assert term["source"] is None


def test_never_uses_only_one_ticker_from_yahoo(monkeypatch, fred_fallback):
    """If ^VIX succeeds but ^VIX3M fails, Yahoo as a whole must be treated
    as failed (fall through to FRED) -- never a mixed vix=yahoo/vix3m=None
    result."""
    now = datetime.now(timezone.utc)

    def _download(ticker, period, interval, progress, auto_adjust):
        if ticker == "^VIX":
            return _yf_frame([18.0], now)
        return pd.DataFrame()  # ^VIX3M empty/failed
    monkeypatch.setattr("yfinance.download", _download)
    term = FredYahooTermStructure().vix_term()
    assert term["source"] == "fred_daily_close"
