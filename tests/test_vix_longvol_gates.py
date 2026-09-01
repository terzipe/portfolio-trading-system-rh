"""
monitor/vix_longvol_gates.py — data-driven LONG_VOL_TACTICAL confirming
gates (cheap-vol floor, term-structure flattening, VXX momentum). Network
calls (FRED, UW) are monkeypatched throughout.
"""
from types import SimpleNamespace

import pytest

from monitor import vix_longvol_gates as gates
from monitor import vix_percentile
from data.fred import FredError


# ── Gate A: cheap vol floor ──────────────────────────────────────────

def test_gate_a_confirms_when_vix_below_threshold(monkeypatch):
    monkeypatch.setattr(vix_percentile, "get_gate_a_threshold", lambda: 20.0)
    assert gates.cheap_vol_gate(15.0) is True


def test_gate_a_does_not_confirm_when_vix_above_threshold(monkeypatch):
    monkeypatch.setattr(vix_percentile, "get_gate_a_threshold", lambda: 20.0)
    assert gates.cheap_vol_gate(25.0) is False


def test_gate_a_fails_closed_with_no_vix(monkeypatch):
    monkeypatch.setattr(vix_percentile, "get_gate_a_threshold", lambda: 20.0)
    assert gates.cheap_vol_gate(None) is False


def test_gate_a_fails_closed_with_no_cached_threshold(monkeypatch):
    monkeypatch.setattr(vix_percentile, "get_gate_a_threshold", lambda: None)
    assert gates.cheap_vol_gate(15.0) is False


# ── Gate B: term-structure flattening ───────────────────────────────

def test_gate_b_confirms_when_ratio_has_decreased(monkeypatch):
    monkeypatch.setattr(gates, "_ratio_n_sessions_ago", lambda sessions, as_of=None: 1.10)
    # ratio_now = 15/19 ≈ 0.789, less than 1.10 -> flattening confirmed
    assert gates.term_structure_gate(15.0, 19.0, lookback_sessions=10) is True


def test_gate_b_does_not_confirm_when_ratio_has_increased(monkeypatch):
    monkeypatch.setattr(gates, "_ratio_n_sessions_ago", lambda sessions, as_of=None: 0.5)
    assert gates.term_structure_gate(15.0, 19.0, lookback_sessions=10) is False


def test_gate_b_fails_closed_with_missing_live_data(monkeypatch):
    monkeypatch.setattr(gates, "_ratio_n_sessions_ago", lambda sessions, as_of=None: 1.10)
    assert gates.term_structure_gate(None, 19.0, lookback_sessions=10) is False
    assert gates.term_structure_gate(15.0, None, lookback_sessions=10) is False
    assert gates.term_structure_gate(15.0, 0.0, lookback_sessions=10) is False


def test_gate_b_magnitude_floor_rejects_a_small_decrease(monkeypatch):
    # ratio_then=1.0, ratio_now=0.97 -- a 3% decrease, under a 10% floor.
    monkeypatch.setattr(gates, "_ratio_n_sessions_ago", lambda sessions, as_of=None: 1.0)
    assert gates.term_structure_gate(0.97 * 19.0, 19.0, lookback_sessions=10, min_pct=0.10) is False


def test_gate_b_magnitude_floor_accepts_a_large_enough_decrease(monkeypatch):
    # ratio_then=1.0, ratio_now=0.85 -- a 15% decrease, clears a 10% floor.
    monkeypatch.setattr(gates, "_ratio_n_sessions_ago", lambda sessions, as_of=None: 1.0)
    assert gates.term_structure_gate(0.85 * 19.0, 19.0, lookback_sessions=10, min_pct=0.10) is True


def test_gate_b_fails_closed_when_historical_ratio_unavailable(monkeypatch):
    monkeypatch.setattr(gates, "_ratio_n_sessions_ago", lambda sessions, as_of=None: None)
    assert gates.term_structure_gate(15.0, 19.0, lookback_sessions=10) is False


def test_ratio_n_sessions_ago_computes_from_shared_dates(monkeypatch):
    import datetime as dt
    vix_series = [(dt.date(2026, 8, d), 15.0 + d) for d in range(1, 21)]
    vix3m_series = [(dt.date(2026, 8, d), 19.0) for d in range(1, 21)]

    def _fake_fetch(series_id, lookback_years, end=None):
        return vix_series if series_id == "VIXCLS" else vix3m_series

    monkeypatch.setattr(gates, "fetch_dated_series", _fake_fetch)
    # 20 shared dates, 5 sessions back -> the (20-1-5)=14th index (0-based),
    # i.e. Aug 15 (day=15) -> vix=30.0, ratio = 30.0/19.0
    ratio = gates._ratio_n_sessions_ago(5)
    assert ratio == pytest.approx(30.0 / 19.0)


def test_ratio_n_sessions_ago_fails_closed_on_fred_error(monkeypatch):
    def _boom(series_id, lookback_years, end=None):
        raise FredError("simulated outage")

    monkeypatch.setattr(gates, "fetch_dated_series", _boom)
    assert gates._ratio_n_sessions_ago(10) is None


def test_ratio_n_sessions_ago_fails_closed_on_insufficient_history(monkeypatch):
    import datetime as dt
    short_series = [(dt.date(2026, 8, 1), 15.0), (dt.date(2026, 8, 2), 16.0)]

    monkeypatch.setattr(gates, "fetch_dated_series", lambda *a, **k: short_series)
    assert gates._ratio_n_sessions_ago(10) is None  # only 2 shared dates, need > 10


# ── Gate C: VXX momentum ────────────────────────────────────────────

def _uw_with_closes(closes, expected_ticker=None):
    def _ohlc(ticker, candle_size="1d"):
        if expected_ticker is not None:
            assert ticker == expected_ticker
        return {"data": [{"close": str(c), "market_time": "r"} for c in closes]}
    return SimpleNamespace(ohlc=_ohlc)


def test_gate_c_confirms_on_sufficient_positive_momentum():
    uw = _uw_with_closes([20.0] + [20.0] * 9 + [21.5])  # +7.5% over the window
    assert gates.momentum_gate(uw, lookback_sessions=10, min_pct=0.05) is True


def test_gate_c_does_not_confirm_below_magnitude_floor():
    uw = _uw_with_closes([20.0] + [20.0] * 9 + [20.5])  # +2.5%, under the 5% floor
    assert gates.momentum_gate(uw, lookback_sessions=10, min_pct=0.05) is False


def test_gate_c_does_not_confirm_on_negative_momentum():
    uw = _uw_with_closes([20.0] + [20.0] * 9 + [18.0])
    assert gates.momentum_gate(uw, lookback_sessions=10, min_pct=0.05) is False


def test_gate_c_fails_closed_on_insufficient_history():
    uw = _uw_with_closes([20.0])
    assert gates.momentum_gate(uw, lookback_sessions=10, min_pct=0.05) is False


def test_gate_c_defaults_to_vxx():
    uw = _uw_with_closes([20.0] * 10 + [21.5], expected_ticker="VXX")
    assert gates.momentum_gate(uw, lookback_sessions=10, min_pct=0.05) is True


def test_gate_c_uses_uvxy_when_requested():
    uw = _uw_with_closes([20.0] * 10 + [21.5], expected_ticker="UVXY")
    assert gates.momentum_gate(uw, lookback_sessions=10, min_pct=0.05, ticker="UVXY") is True


def test_momentum_pct_exposes_the_raw_reading():
    """Added 2026-09-01 for the VXX/UVXY rotation tie-break -- the raw
    magnitude, not just whether it cleared the floor."""
    uw = _uw_with_closes([20.0] * 10 + [21.5])  # +7.5%
    pct = gates._momentum_pct(uw, "VXX", lookback_sessions=10)
    assert pct == pytest.approx(0.075)


def test_momentum_pct_fails_closed_on_insufficient_history():
    uw = _uw_with_closes([20.0])
    assert gates._momentum_pct(uw, "VXX", lookback_sessions=10) is None


def test_evaluate_passes_ticker_through_to_momentum_pct(monkeypatch):
    monkeypatch.setattr(gates, "cheap_vol_gate", lambda vix_now: True)
    monkeypatch.setattr(gates, "term_structure_gate", lambda vix_now, vix3m_now, lookback_sessions=None: False)
    seen = {}

    def _fake_momentum_pct(uw, ticker, lookback_sessions):
        seen["ticker"] = ticker
        return 0.15  # clears the default 10% floor -- gate_c confirmed

    monkeypatch.setattr(gates, "_momentum_pct", _fake_momentum_pct)
    result = gates.evaluate(uw_client=object(), vix_now=15.0, vix3m_now=19.0, ticker="UVXY")
    assert seen["ticker"] == "UVXY"
    assert result.momentum_pct == 0.15
    assert any("UVXY momentum" in r for r in result.reasons)


# ── Combined scoring ─────────────────────────────────────────────────

def test_evaluate_confirms_at_two_of_three(monkeypatch):
    monkeypatch.setattr(gates, "cheap_vol_gate", lambda vix_now: True)
    monkeypatch.setattr(gates, "term_structure_gate", lambda vix_now, vix3m_now, lookback_sessions=None: True)
    monkeypatch.setattr(gates, "_momentum_pct", lambda uw, ticker, lookback_sessions: 0.02)  # below the 10% floor

    result = gates.evaluate(uw_client=object(), vix_now=15.0, vix3m_now=19.0)
    assert result.score == 2
    assert result.confirmed is True
    assert len(result.reasons) == 3


def test_evaluate_does_not_confirm_at_one_of_three(monkeypatch):
    monkeypatch.setattr(gates, "cheap_vol_gate", lambda vix_now: True)
    monkeypatch.setattr(gates, "term_structure_gate", lambda vix_now, vix3m_now, lookback_sessions=None: False)
    monkeypatch.setattr(gates, "_momentum_pct", lambda uw, ticker, lookback_sessions: 0.02)

    result = gates.evaluate(uw_client=object(), vix_now=15.0, vix3m_now=19.0)
    assert result.score == 1
    assert result.confirmed is False


def test_evaluate_confirms_at_three_of_three(monkeypatch):
    monkeypatch.setattr(gates, "cheap_vol_gate", lambda vix_now: True)
    monkeypatch.setattr(gates, "term_structure_gate", lambda vix_now, vix3m_now, lookback_sessions=None: True)
    monkeypatch.setattr(gates, "_momentum_pct", lambda uw, ticker, lookback_sessions: 0.15)

    result = gates.evaluate(uw_client=object(), vix_now=15.0, vix3m_now=19.0)
    assert result.score == 3
    assert result.confirmed is True


def test_evaluate_does_not_confirm_without_gate_a_even_at_score_two(monkeypatch):
    # Gate A is a REQUIRED prerequisite, not an equal-weighted vote (changed
    # 2026-08-25 after backtesting showed signals without A confirmed were
    # net-negative). B+C alone, however "confirmed" a score they'd add up
    # to, must not fire the posture without vol actually being cheap.
    monkeypatch.setattr(gates, "cheap_vol_gate", lambda vix_now: False)
    monkeypatch.setattr(gates, "term_structure_gate", lambda vix_now, vix3m_now, lookback_sessions=None: True)
    monkeypatch.setattr(gates, "_momentum_pct", lambda uw, ticker, lookback_sessions: 0.15)

    result = gates.evaluate(uw_client=object(), vix_now=15.0, vix3m_now=19.0)
    assert result.score == 2  # B and C both confirmed...
    assert result.confirmed is False  # ...but A didn't, so no fire
