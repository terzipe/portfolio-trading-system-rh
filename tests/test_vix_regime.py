"""
Posture-priority test matrix against monitor.vix_regime.compute_posture()
— pure function, no network needed. SVIX_ON/FLATTEN_SVIX were retired
2026-08-20 (SVIX is now driven independently by monitor/vix_ladder.py, see
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md) — this function is options-only
now (FADE_SPIKE_PUTS/LONG_VOL_TACTICAL/CASH).
"""
from monitor import vix_regime
from monitor.vix_longvol_gates import LongVolGateResult


def _gates(confirmed: bool, score: int = 2) -> LongVolGateResult:
    return LongVolGateResult(
        gate_a_cheap=True, gate_b_term_structure=True, gate_c_momentum=False,
        score=score, confirmed=confirmed,
        reasons=["gate A (cheap vol): confirmed", "gate B (term structure): confirmed", "gate C (momentum): no"],
    )


def test_fade_spike_ok_triggers_fade_spike_puts():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=22.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
        fade_spike_ok=True,
    )
    assert posture == vix_regime.FADE_SPIKE_PUTS
    assert any("fade-spike" in r for r in reasons)


def test_confirmed_longvol_gates_trigger_long_vol_tactical():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        longvol_gates=_gates(confirmed=True),
    )
    assert posture == vix_regime.LONG_VOL_TACTICAL
    assert any("long-vol gates" in r for r in reasons)


def test_longvol_gates_fire_outside_aug_oct_window_too():
    # Calendar is fully dropped from this decision (2026-08-25) -- a
    # February month (the old "neutral" bias) still fires
    # LONG_VOL_TACTICAL if the data-driven gates confirm.
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=2, data_age_sec=0,
        longvol_gates=_gates(confirmed=True),
    )
    assert posture == vix_regime.LONG_VOL_TACTICAL


def test_calendar_alone_no_longer_triggers_long_vol_tactical():
    # August (the old hard-coded bias month) with no/unconfirmed gates must
    # NOT trigger LONG_VOL_TACTICAL anymore -- calendar is fully replaced
    # by monitor/vix_longvol_gates.py, not just supplemented by it.
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        longvol_gates=None,
    )
    assert posture == vix_regime.CASH


def test_unconfirmed_longvol_gates_reasons_still_surfaced():
    # Even when the score doesn't clear the bar, the per-gate breakdown
    # should be visible in reasons -- observability for tuning thresholds,
    # not just a silent CASH.
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        longvol_gates=_gates(confirmed=False, score=1),
    )
    assert posture == vix_regime.CASH
    assert any("gate A" in r for r in reasons)


def test_neutral_month_no_fade_spike_is_cash():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=2, data_age_sec=0,  # Feb = neutral bias
    )
    assert posture == vix_regime.CASH
    assert bias == "neutral"


def test_stale_data_blocks_new_buys():
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6,
        data_age_sec=99999,  # way past VIX_STALE_SECONDS
    )
    assert posture == vix_regime.CASH


def test_kill_switch_forces_cash(monkeypatch):
    monkeypatch.setattr(vix_regime, "VIX_KILL_SWITCH", True)
    posture, bias, reasons = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.CASH
    assert any("KILL_SWITCH" in r for r in reasons)


def test_kill_switch_takes_priority_over_fade_spike(monkeypatch):
    # Kill switch is priority 1, checked before fade-spike criteria — even
    # a real spike shouldn't open a new options position while it's on.
    monkeypatch.setattr(vix_regime, "VIX_KILL_SWITCH", True)
    posture, bias, reasons = vix_regime.compute_posture(
        vix=22.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
        fade_spike_ok=True,
    )
    assert posture == vix_regime.CASH
