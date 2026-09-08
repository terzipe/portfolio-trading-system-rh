"""monitor/corr_compression.py — realized-correlation compression signal.
No network: _fetch_basket_closes() is monkeypatched with synthetic price
frames."""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from monitor import corr_compression as cc


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "SVIX_CORR_STATE_FILE", tmp_path / "corr_compression_state.json")
    monkeypatch.setattr(cc, "SVIX_CORR_WINDOW", 21)
    monkeypatch.setattr(cc, "SVIX_CORR_PERCENTILE", 5.0)
    monkeypatch.setattr(cc, "SVIX_CORR_LOOKBACK_YEARS", 2.0)
    monkeypatch.setattr(cc, "SVIX_CORR_STALE_DAYS", 5)
    return tmp_path


def _synthetic_closes(n_days=600, n_names=15, corr_level=0.2, seed=0, tail_corr=None):
    """Build a price frame whose constituents have a target average pairwise
    return correlation. `tail_corr`, if set, applies to the last 30 days."""
    rng = np.random.default_rng(seed)

    def block(days, rho):
        common = rng.normal(0, 1, days)
        idio = rng.normal(0, 1, (days, n_names))
        w = np.sqrt(max(rho, 0.0))
        rets = w * common[:, None] + np.sqrt(max(1 - rho, 1e-6)) * idio
        return rets * 0.012

    if tail_corr is None:
        rets = block(n_days, corr_level)
    else:
        rets = np.vstack([block(n_days - 30, corr_level), block(30, tail_corr)])
    prices = 100 * np.exp(np.cumsum(rets, axis=0))
    idx = pd.bdate_range(end=date.today(), periods=n_days)
    return pd.DataFrame(prices, index=idx, columns=[f"T{i}" for i in range(n_names)])


def test_avg_pairwise_corr_matches_numpy():
    rng = np.random.default_rng(1)
    w = rng.normal(size=(50, 6))
    c = np.corrcoef(w.T)
    expected = (c.sum() - 6) / (6 * 5)
    assert cc._avg_pairwise_corr(w) == pytest.approx(expected)


def test_compute_flags_compressed_when_recent_corr_is_a_low_outlier(isolated, monkeypatch):
    # history sits around rho=0.35, the last 30 days collapse to rho=0.02
    monkeypatch.setattr(cc, "_fetch_basket_closes",
                        lambda: _synthetic_closes(corr_level=0.35, tail_corr=0.02, seed=7))
    out = cc.compute()
    assert out["rc"] is not None
    assert out["percentile"] <= 5.0
    assert out["compressed"] is True


def test_compute_not_compressed_in_a_steady_regime(isolated, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: _synthetic_closes(corr_level=0.3, seed=3))
    out = cc.compute()
    assert out["compressed"] is False
    assert 0 <= out["percentile"] <= 100


def test_compute_fails_closed_on_fetch_failure(isolated, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: None)
    out = cc.compute()
    assert out == {**out, "rc": None, "percentile": None, "compressed": False}


def test_get_status_caches_within_a_day(isolated, monkeypatch):
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1
        return _synthetic_closes(corr_level=0.3, seed=3)

    monkeypatch.setattr(cc, "_fetch_basket_closes", _fake)
    a = cc.get_status()
    b = cc.get_status()
    assert calls["n"] == 1  # second call served from cache
    assert a == b


def test_get_status_dry_run_does_not_persist(isolated, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: _synthetic_closes(corr_level=0.3, seed=3))
    cc.get_status(dry_run=True)
    assert not cc.SVIX_CORR_STATE_FILE.exists()


def test_get_status_falls_back_to_recent_cache_on_transient_failure(isolated, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: _synthetic_closes(corr_level=0.3, seed=3))
    good = cc.get_status()
    # make the cache look like yesterday, then break the fetch
    stale = {**good, "date": (date.today() - timedelta(days=1)).isoformat()}
    cc.SVIX_CORR_STATE_FILE.write_text(json.dumps(stale))
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: None)
    out = cc.get_status()
    assert out["rc"] == good["rc"]  # served the day-old cache, not the failure


def test_get_status_does_not_use_a_too_old_cache(isolated, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: _synthetic_closes(corr_level=0.3, seed=3))
    good = cc.get_status()
    ancient = {**good, "date": (date.today() - timedelta(days=30)).isoformat()}
    cc.SVIX_CORR_STATE_FILE.write_text(json.dumps(ancient))
    monkeypatch.setattr(cc, "_fetch_basket_closes", lambda: None)
    out = cc.get_status()
    assert out["compressed"] is False and out["rc"] is None  # failed closed, cache too stale
