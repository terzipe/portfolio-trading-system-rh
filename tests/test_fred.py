"""
data/fred.py — free, no-API-key FRED VIXCLS CSV fetch. requests.get is
monkeypatched throughout; no real network calls.
"""
from datetime import date

import pytest

from data import fred


class _FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise fred.requests.HTTPError(f"status {self._status}")


def test_fetch_vix_closes_parses_csv(monkeypatch):
    csv_text = "DATE,VIXCLS\n2026-08-18,17.24\n2026-08-19,18.01\n2026-08-20,16.90\n"
    monkeypatch.setattr(fred.requests, "get", lambda *a, **k: _FakeResponse(csv_text))
    closes = fred.fetch_vix_closes(10)
    assert closes == [17.24, 18.01, 16.90]


def test_fetch_vix_closes_skips_missing_value_rows(monkeypatch):
    csv_text = "DATE,VIXCLS\n2026-08-18,17.24\n2026-08-19,.\n2026-08-20,16.90\n"
    monkeypatch.setattr(fred.requests, "get", lambda *a, **k: _FakeResponse(csv_text))
    closes = fred.fetch_vix_closes(10)
    assert closes == [17.24, 16.90]


def test_fetch_vix_closes_raises_on_empty_series(monkeypatch):
    monkeypatch.setattr(fred.requests, "get", lambda *a, **k: _FakeResponse("DATE,VIXCLS\n"))
    with pytest.raises(fred.FredError):
        fred.fetch_vix_closes(10)


def test_fetch_vix_closes_wraps_request_exception(monkeypatch):
    def _boom(*a, **k):
        raise fred.requests.RequestException("network down")

    monkeypatch.setattr(fred.requests, "get", _boom)
    with pytest.raises(fred.FredError):
        fred.fetch_vix_closes(10)


def test_lookback_start_handles_leap_day():
    # end date is a leap day; target year has none -> falls back to Feb 28.
    start = fred._lookback_start(date(2024, 2, 29), 3)
    assert start == date(2021, 2, 28)


def test_lookback_start_normal_case():
    start = fred._lookback_start(date(2026, 8, 24), 10)
    assert start == date(2016, 8, 24)
