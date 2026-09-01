"""
Posture-priority test matrix against monitor.vix_regime.compute_posture()
— pure function, no network needed. SVIX_ON/FLATTEN_SVIX were retired
2026-08-20 (SVIX is now driven independently by monitor/vix_ladder.py, see
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md) — this function is options-only
now (FADE_SPIKE_PUTS/LONG_VOL_TACTICAL/CASH).

VXX/UVXY rotation (2026-09-01): compute_posture() now takes vxx_gates AND
uvxy_gates (previously a single longvol_gates) and returns a 4-tuple
(posture, bias, reasons, longvol_ticker) -- see _pick_longvol_ticker()'s
own dedicated test section below for the rotation/tie-break rule itself.
"""
from monitor import vix_regime
from monitor.vix_longvol_gates import LongVolGateResult


def _gates(
    confirmed: bool, score: int = 2, momentum_pct: float | None = None,
    gate_c_momentum: bool = False,
) -> LongVolGateResult:
    return LongVolGateResult(
        gate_a_cheap=True, gate_b_term_structure=True, gate_c_momentum=gate_c_momentum,
        score=score, confirmed=confirmed, momentum_pct=momentum_pct,
        reasons=["gate A (cheap vol): confirmed", "gate B (term structure): confirmed",
                 f"gate C (momentum): {'yes' if gate_c_momentum else 'no'}"],
    )


def test_fade_spike_ok_triggers_fade_spike_puts():
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=22.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
        fade_spike_ok=True,
    )
    assert posture == vix_regime.FADE_SPIKE_PUTS
    assert ticker is None
    assert any("fade-spike" in r for r in reasons)


def test_confirmed_vxx_gates_trigger_long_vol_tactical():
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        vxx_gates=_gates(confirmed=True),
    )
    assert posture == vix_regime.LONG_VOL_TACTICAL
    assert ticker == "VXX"
    assert any("long-vol gates" in r for r in reasons)


def test_confirmed_uvxy_gates_alone_also_trigger_long_vol_tactical():
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        uvxy_gates=_gates(confirmed=True),
    )
    assert posture == vix_regime.LONG_VOL_TACTICAL
    assert ticker == "UVXY"


def test_longvol_gates_fire_outside_aug_oct_window_too():
    # Calendar is fully dropped from this decision (2026-08-25) -- a
    # February month (the old "neutral" bias) still fires
    # LONG_VOL_TACTICAL if the data-driven gates confirm.
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=2, data_age_sec=0,
        vxx_gates=_gates(confirmed=True),
    )
    assert posture == vix_regime.LONG_VOL_TACTICAL


def test_calendar_alone_no_longer_triggers_long_vol_tactical():
    # August (the old hard-coded bias month) with no/unconfirmed gates must
    # NOT trigger LONG_VOL_TACTICAL anymore -- calendar is fully replaced
    # by monitor/vix_longvol_gates.py, not just supplemented by it.
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        vxx_gates=None, uvxy_gates=None,
    )
    assert posture == vix_regime.CASH
    assert ticker is None


def test_unconfirmed_longvol_gates_reasons_still_surfaced():
    # Even when the score doesn't clear the bar, the per-gate breakdown
    # should be visible in reasons -- observability for tuning thresholds,
    # not just a silent CASH.
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        vxx_gates=_gates(confirmed=False, score=1),
    )
    assert posture == vix_regime.CASH
    assert any("gate A" in r for r in reasons)


def test_neutral_month_no_fade_spike_is_cash():
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=2, data_age_sec=0,  # Feb = neutral bias
    )
    assert posture == vix_regime.CASH
    assert bias == "neutral"


def test_stale_data_blocks_new_buys():
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6,
        data_age_sec=99999,  # way past VIX_STALE_SECONDS
    )
    assert posture == vix_regime.CASH


def test_kill_switch_forces_cash(monkeypatch):
    monkeypatch.setattr(vix_regime, "VIX_KILL_SWITCH", True)
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
    )
    assert posture == vix_regime.CASH
    assert any("KILL_SWITCH" in r for r in reasons)


def test_kill_switch_takes_priority_over_fade_spike(monkeypatch):
    # Kill switch is priority 1, checked before fade-spike criteria — even
    # a real spike shouldn't open a new options position while it's on.
    monkeypatch.setattr(vix_regime, "VIX_KILL_SWITCH", True)
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=22.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=6, data_age_sec=0,
        fade_spike_ok=True,
    )
    assert posture == vix_regime.CASH


# ── VXX/UVXY rotation (_pick_longvol_ticker) ────────────────────────────
# Confirmed design, 2026-09-01, refined 2026-09-02: trade whichever ticker's
# OWN gates confirm. When both confirm (common, since Gate A/B are
# index-level and identical for both -- Gate B alone reaching the score
# means BOTH automatically confirm together WITHOUT Gate C itself
# confirming for either), momentum magnitude is only compared when BOTH
# tickers' Gate C independently confirmed -- a genuine momentum reading on
# both sides. Otherwise (confirmed via A+B alone for one or both) the
# comparison is skipped and it defaults straight to VXX. Ties within
# VIX_LONGVOL_MOMENTUM_TIE_PCT (default 2%) also default to VXX (changed
# from the original UVXY default, per the same 2026-09-02 refinement).

def test_pick_ticker_none_when_neither_confirms():
    assert vix_regime._pick_longvol_ticker(_gates(False), _gates(False)) is None


def test_pick_ticker_vxx_when_only_vxx_confirms():
    assert vix_regime._pick_longvol_ticker(_gates(True, momentum_pct=0.11), _gates(False)) == "VXX"


def test_pick_ticker_uvxy_when_only_uvxy_confirms():
    assert vix_regime._pick_longvol_ticker(_gates(False), _gates(True, momentum_pct=0.11)) == "UVXY"


def test_pick_ticker_stronger_momentum_wins_when_both_gate_c_confirm():
    vxx = _gates(True, momentum_pct=0.20, gate_c_momentum=True)
    uvxy = _gates(True, momentum_pct=0.12, gate_c_momentum=True)
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "VXX"


def test_pick_ticker_stronger_uvxy_momentum_wins_when_both_gate_c_confirm():
    vxx = _gates(True, momentum_pct=0.11, gate_c_momentum=True)
    uvxy = _gates(True, momentum_pct=0.25, gate_c_momentum=True)
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "UVXY"


def test_pick_ticker_defaults_to_vxx_on_a_tie():
    vxx = _gates(True, momentum_pct=0.12, gate_c_momentum=True)
    uvxy = _gates(True, momentum_pct=0.125, gate_c_momentum=True)  # within the default 2% tie band
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "VXX"


def test_pick_ticker_exact_tie_defaults_to_vxx():
    vxx = _gates(True, momentum_pct=0.15, gate_c_momentum=True)
    uvxy = _gates(True, momentum_pct=0.15, gate_c_momentum=True)
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "VXX"


def test_pick_ticker_missing_momentum_pct_treated_as_zero():
    """Fails closed rather than crashing if momentum_pct is None (the gate
    itself failed closed on missing data) -- both effectively "0% momentum"
    for tie-break purposes, which lands on the VXX-default tie."""
    vxx = _gates(True, momentum_pct=None, gate_c_momentum=True)
    uvxy = _gates(True, momentum_pct=None, gate_c_momentum=True)
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "VXX"


def test_pick_ticker_both_confirm_via_ab_alone_defaults_to_vxx():
    """When both tickers confirm but neither's Gate C independently
    confirmed (score cleared via Gate A+B alone -- the common real-world
    case, since Gate A/B are index-level and identical for both tickers),
    momentum magnitude isn't a genuine signal -- skip the comparison and
    default straight to VXX, regardless of the raw (often negative)
    momentum_pct values."""
    vxx = _gates(True, momentum_pct=-0.05, gate_c_momentum=False)
    uvxy = _gates(True, momentum_pct=-0.20, gate_c_momentum=False)  # bigger magnitude, but not a real signal
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "VXX"


def test_pick_ticker_one_gate_c_confirmed_still_defaults_to_vxx():
    """Only a genuine comparison (BOTH sides' Gate C independently
    confirmed) triggers the momentum comparison -- one side alone doesn't
    count, even if that side has a much stronger reading."""
    vxx = _gates(True, momentum_pct=0.05, gate_c_momentum=False)
    uvxy = _gates(True, momentum_pct=0.30, gate_c_momentum=True)
    assert vix_regime._pick_longvol_ticker(vxx, uvxy) == "VXX"


def test_compute_posture_both_confirmed_with_gate_c_notes_genuine_tiebreak_in_reasons():
    vxx = _gates(True, momentum_pct=0.20, gate_c_momentum=True)
    uvxy = _gates(True, momentum_pct=0.12, gate_c_momentum=True)
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        vxx_gates=vxx, uvxy_gates=uvxy,
    )
    assert ticker == "VXX"
    assert any("both VXX and UVXY confirmed with their own Gate C also confirmed" in r for r in reasons)


def test_compute_posture_both_confirmed_via_ab_alone_notes_default_in_reasons():
    vxx = _gates(True, momentum_pct=-0.05, gate_c_momentum=False)
    uvxy = _gates(True, momentum_pct=-0.20, gate_c_momentum=False)
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=15.7, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        vxx_gates=vxx, uvxy_gates=uvxy,
    )
    assert ticker == "VXX"
    assert any("not a genuine momentum comparison" in r for r in reasons)


def test_compute_posture_neither_confirmed_shows_both_tickers_reasons_without_duplicating_shared_gates():
    vxx = _gates(False, score=1)
    uvxy = _gates(False, score=1)
    posture, bias, reasons, ticker = vix_regime.compute_posture(
        vix=17.0, vix3m=19.0, vx1=None, vx2=None,
        gate_posture="FULL", gate_score=90, calendar_month=8, data_age_sec=0,
        vxx_gates=vxx, uvxy_gates=uvxy,
    )
    assert posture == vix_regime.CASH
    # Gate A/B lines appear once (from vxx_gates), not duplicated from uvxy_gates too.
    assert sum("gate A" in r for r in reasons) == 1
    assert sum("gate B" in r for r in reasons) == 1
