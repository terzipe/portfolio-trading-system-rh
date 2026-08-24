"""
monitor/vix_percentile.py — percentile-rung threshold table for the SVIX
ladder. compute_thresholds() is pure (no I/O); refresh()/needs_refresh()/
is_stale() are exercised against a tmp_path-isolated state file with
data.fred.fetch_vix_closes monkeypatched, so none of this touches the
network. See VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md v2.0 §2.
"""
from datetime import datetime, timedelta, timezone

import pytest

from monitor import vix_percentile
from data.fred import FredError


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_file = tmp_path / "vix_percentile_state.json"
    monkeypatch.setattr(vix_percentile, "VIX_PERCENTILE_STATE_FILE", state_file)
    return state_file


# ── compute_thresholds() — pure function ────────────────────────────

def test_compute_thresholds_covers_all_configured_rungs():
    closes = [float(v) for v in range(1, 1001)]  # 1..1000, evenly distributed
    thresholds = vix_percentile.compute_thresholds(closes)
    assert set(thresholds) == set(vix_percentile.VIX_PERCENTILE_RUNGS)


def test_compute_thresholds_90th_percentile_of_uniform_distribution():
    closes = [float(v) for v in range(1, 1001)]  # 1..1000
    thresholds = vix_percentile.compute_thresholds(closes)
    # 90th percentile of a uniform 1..1000 series lands ~900 (linear interp).
    assert thresholds[90] == pytest.approx(900.1, abs=1.0)


def test_compute_thresholds_monotonic_in_percentile():
    closes = [10, 15, 12, 40, 18, 90, 22, 14, 30, 55, 60, 11, 13, 70, 45]
    thresholds = vix_percentile.compute_thresholds(closes)
    ordered = [thresholds[p] for p in sorted(thresholds)]
    assert ordered == sorted(ordered)  # ascending percentile -> ascending VIX level


def test_compute_thresholds_single_value():
    assert vix_percentile.compute_thresholds([42.0]) == {p: 42.0 for p in vix_percentile.VIX_PERCENTILE_RUNGS}


def test_compute_thresholds_empty_raises():
    with pytest.raises(ValueError):
        vix_percentile.compute_thresholds([])


# ── needs_refresh() / is_stale() timing ─────────────────────────────

def _state_aged(days_ago: float | None) -> dict:
    ts = None if days_ago is None else (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"refreshed_at": ts, "thresholds": {"90": 30.0}, "lookback_years": 10, "sample_size": 100}


def test_never_refreshed_needs_refresh_and_is_stale():
    state = _state_aged(None)
    assert vix_percentile.needs_refresh(state) is True
    assert vix_percentile.is_stale(state) is True


def test_fresh_refresh_does_not_need_refresh_or_stale():
    state = _state_aged(1)
    assert vix_percentile.needs_refresh(state) is False
    assert vix_percentile.is_stale(state) is False


def test_eight_days_old_needs_refresh_but_not_stale():
    state = _state_aged(8)  # past the 7-day refresh interval, short of the 14-day stale gate
    assert vix_percentile.needs_refresh(state) is True
    assert vix_percentile.is_stale(state) is False


def test_fifteen_days_old_is_stale():
    state = _state_aged(15)
    assert vix_percentile.needs_refresh(state) is True
    assert vix_percentile.is_stale(state) is True


# ── refresh() ────────────────────────────────────────────────────────

def test_refresh_fetches_and_persists_when_due(isolated_state, monkeypatch):
    monkeypatch.setattr(vix_percentile, "fetch_vix_closes", lambda years: [10.0, 20.0, 30.0, 40.0, 50.0])
    state = vix_percentile.refresh()
    assert state["refreshed_at"] is not None
    assert state["sample_size"] == 5
    thresholds = vix_percentile.get_thresholds()
    assert thresholds is not None
    assert set(thresholds) == set(vix_percentile.VIX_PERCENTILE_RUNGS)


def test_refresh_noop_when_not_due(isolated_state, monkeypatch):
    calls = []
    monkeypatch.setattr(vix_percentile, "fetch_vix_closes", lambda years: calls.append(1) or [10.0, 20.0])
    vix_percentile.refresh()  # first call: due (never refreshed), fetches once
    vix_percentile.refresh()  # second call: not due yet, must not fetch again
    assert len(calls) == 1


def test_refresh_force_fetches_even_when_not_due(isolated_state, monkeypatch):
    calls = []
    monkeypatch.setattr(vix_percentile, "fetch_vix_closes", lambda years: calls.append(1) or [10.0, 20.0])
    vix_percentile.refresh()
    vix_percentile.refresh(force=True)
    assert len(calls) == 2


def test_refresh_keeps_cached_thresholds_on_fetch_failure(isolated_state, monkeypatch):
    monkeypatch.setattr(vix_percentile, "fetch_vix_closes", lambda years: [10.0, 20.0, 30.0])
    vix_percentile.refresh()
    good_thresholds = vix_percentile.get_thresholds()

    def _boom(years):
        raise FredError("simulated FRED outage")

    monkeypatch.setattr(vix_percentile, "fetch_vix_closes", _boom)
    vix_percentile.refresh(force=True)  # must not raise
    assert vix_percentile.get_thresholds() == good_thresholds  # untouched


def test_get_thresholds_none_before_first_refresh(isolated_state):
    assert vix_percentile.get_thresholds() is None
