"""
Posture-priority test matrix (Impl Plan §10) against
monitor.vix_regime.compute_posture() — pure function, no network needed.
"""
from monitor import vix_regime


def test_aug_low_vix_contango_is_not_forced_svix():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
    )
    # Aug bias is long_vol / SVIX(-); contango+VIX<25+gate-ok still wins per
    # the posture-priority list (calendar is a weight, not a veto) — but it
    # must NOT be forced off just because of the August bias, and must not
    # silently become a "buy VXX because VIX is cheap" signal either.
    assert posture != vix_regime.LONG_VOL_TACTICAL or posture == vix_regime.SVIX_ON
    assert posture in (vix_regime.SVIX_ON, vix_regime.LONG_VOL_TACTICAL)


def test_nov_low_vix_contango_is_svix_on():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=16.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=11, data_age_sec=0,
    )
    assert posture == vix_regime.SVIX_ON
    assert bias == "svix"


def test_feb_low_vix_contango_svix_on_allowed_not_locked_out():
    """SRS §2 decision #10 / §7.2: Feb-Mar is not a hard SVIX lock."""
    posture, bias, reasons = vix_regime.compute_posture(
        vix=16.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=2, data_age_sec=0,
    )
    assert posture == vix_regime.SVIX_ON
    assert bias == "neutral"


def test_vix_25_forces_flatten():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=25.2, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.FLATTEN_SVIX


def test_backwardation_forces_flatten_even_below_25():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=22.0, vix3m=19.0, vx1=None, vx2=None,  # VIX > VIX3M = backwardation
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.FLATTEN_SVIX


def test_stale_data_blocks_new_buys():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6,
        data_age_sec=99999,  # way past VIX_STALE_SECONDS
    )
    assert posture == vix_regime.CASH


def test_gate_cash_blocks_new_svix_even_in_contango():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="CASH", gate_score=10, calendar_month=6, data_age_sec=0,
    )
    assert posture != vix_regime.SVIX_ON


def test_kill_switch_forces_flatten_only(monkeypatch):
    monkeypatch.setattr(vix_regime, "VIX_KILL_SWITCH", True)
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.FLATTEN_SVIX
    assert any("KILL_SWITCH" in r for r in reasons)


def test_auto_kill_switch_tripped_forces_flatten(monkeypatch):
    monkeypatch.setattr(vix_regime.vix_kill_switch, "is_tripped", lambda: True)
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.FLATTEN_SVIX
    assert any("auto kill switch" in r for r in reasons)


def test_vx1_vx2_takes_priority_over_vix_vix3m_when_both_present():
    # VX1 > VX2 = backwardation on the futures curve, even if VIX < VIX3M
    # would otherwise read contango — CME curve wins once populated.
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=21.0, vx2=20.0,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.FLATTEN_SVIX
