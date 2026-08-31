"""
monitor/vix_leading_signals.py — leading-indicator exit stack for the SVIX
manual campaign. Network calls (FRED, yfinance) and the imported
vix_longvol_gates.term_structure_gate are monkeypatched throughout, same
convention as test_vix_longvol_gates.py.
"""
from datetime import date

import pytest

from monitor import vix_leading_signals


def _oscillating(n, amplitude=5, base=100):
    return [base + (amplitude if i % 2 == 0 else -amplitude) for i in range(n)]


def _flat(n, value=100):
    return [value] * n


# ── Tier 1: VVIX/VIX divergence ─────────────────────────────────────────

def test_divergence_gate_confirms_vvix_up_vix_flat_or_down():
    vix = [20.0] * 4 + [19.0]
    vvix = [90.0] * 4 + [100.0]  # up ~11.1pp vs VIX's -5pp -> ~16.1pp spread
    assert vix_leading_signals.divergence_gate(vix, vvix, sessions=4, min_pp=0.10) is True


def test_divergence_gate_fails_when_vix_already_moving_up():
    vix = [20.0] * 4 + [22.0]  # up 10% -- coincident, not leading
    vvix = [90.0] * 4 + [105.0]
    assert vix_leading_signals.divergence_gate(vix, vvix, sessions=4, min_pp=0.10) is False


def test_divergence_gate_fails_when_spread_below_threshold():
    vix = [20.0] * 5
    vvix = [90.0] * 4 + [95.0]  # up ~5.6pp, under a 10pp floor
    assert vix_leading_signals.divergence_gate(vix, vvix, sessions=4, min_pp=0.10) is False


def test_divergence_gate_fails_closed_on_insufficient_history():
    assert vix_leading_signals.divergence_gate([20.0], [90.0], sessions=5, min_pp=0.10) is False


# ── Tier 2: VIX + VVIX compression ──────────────────────────────────────

def test_compression_gate_confirms_when_both_series_recently_flat():
    vix = _oscillating(60) + _flat(10)
    vvix = _oscillating(60, amplitude=8, base=90) + _flat(10, value=90)
    assert vix_leading_signals.compression_gate(vix, vvix, window=5, percentile=50) is True


def test_compression_gate_does_not_confirm_when_still_oscillating():
    vix = _oscillating(70)
    vvix = _oscillating(70, amplitude=8, base=90)
    assert vix_leading_signals.compression_gate(vix, vvix, window=5, percentile=10) is False


def test_compression_gate_requires_both_series_compressed():
    vix = _oscillating(60) + _flat(10)              # VIX compressed
    vvix = _oscillating(70, amplitude=8, base=90)    # VVIX still oscillating -- "combined" required
    assert vix_leading_signals.compression_gate(vix, vvix, window=5, percentile=50) is False


def test_compression_gate_fails_closed_on_insufficient_history():
    assert vix_leading_signals.compression_gate([1.0] * 3, [1.0] * 3, window=5, percentile=50) is False


# ── Tier 4: SKEW confirmer ───────────────────────────────────────────────

def test_skew_confirmer_gate_confirms_on_rise():
    skew = [140.0] * 4 + [148.0]  # +5.7%
    assert vix_leading_signals.skew_confirmer_gate(skew, sessions=4, min_pct=0.03) is True


def test_skew_confirmer_gate_fails_below_threshold():
    skew = [140.0] * 4 + [141.0]  # +0.7%
    assert vix_leading_signals.skew_confirmer_gate(skew, sessions=4, min_pct=0.03) is False


def test_skew_confirmer_gate_fails_closed_on_insufficient_history():
    assert vix_leading_signals.skew_confirmer_gate([140.0], sessions=4, min_pct=0.03) is False


# ── _update_tier3_streak() — day-aware consecutive-confirmation counter ──

_D1, _D2, _D3, _D4 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)


def test_streak_first_ever_call_confirmed_reads_one(isolated_leading_state):
    assert vix_leading_signals._update_tier3_streak(True, today=_D1) == 1


def test_streak_first_ever_call_unconfirmed_reads_zero(isolated_leading_state):
    assert vix_leading_signals._update_tier3_streak(False, today=_D1) == 0


def test_streak_extends_on_a_second_consecutive_confirmed_day(isolated_leading_state):
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    assert vix_leading_signals._update_tier3_streak(True, today=_D2) == 2


def test_streak_keeps_extending_across_more_consecutive_days(isolated_leading_state):
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    vix_leading_signals._update_tier3_streak(True, today=_D2)
    assert vix_leading_signals._update_tier3_streak(True, today=_D3) == 3


def test_streak_resets_when_a_day_breaks_confirmation(isolated_leading_state):
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    vix_leading_signals._update_tier3_streak(False, today=_D2)
    assert vix_leading_signals._update_tier3_streak(True, today=_D3) == 1


def test_streak_repeated_same_day_calls_do_not_double_count(isolated_leading_state):
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    assert vix_leading_signals._update_tier3_streak(True, today=_D1) == 1


def test_streak_reflects_a_flip_to_confirmed_intraday_immediately(isolated_leading_state):
    """A day that starts unconfirmed and later flips confirmed (same date,
    later poll) should reflect the flip right away without waiting for the
    next day's rollover -- ratio_now uses live VIX/VIX3M, so tier3 can
    genuinely change intraday. Day 1 was confirmed and is already finalized
    by the time day 2 starts, so day 2 flipping confirmed (even later in
    the same session) immediately reads as 2 consecutive days -- day 2's
    EARLIER unconfirmed reading that same day doesn't erase day 1's
    already-finalized contribution."""
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    vix_leading_signals._update_tier3_streak(False, today=_D2)  # day 2 starts unconfirmed
    result = vix_leading_signals._update_tier3_streak(True, today=_D2)  # flips confirmed, still day 2
    assert result == 2


def test_streak_a_later_same_day_dip_does_not_retroactively_break_a_prior_streak(isolated_leading_state):
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    vix_leading_signals._update_tier3_streak(True, today=_D2)  # streak = 2
    result = vix_leading_signals._update_tier3_streak(False, today=_D2)  # same day, dips -- still day 2
    assert result == 1  # today's (day 2's) own reading is what's live, not a rewrite of yesterday


def test_streak_dry_run_previews_without_persisting(isolated_leading_state):
    vix_leading_signals._update_tier3_streak(True, today=_D1)  # real: streak now 1, finalized on rollover
    preview = vix_leading_signals._update_tier3_streak(True, today=_D2, dry_run=True)
    assert preview == 2  # same preview a real call would compute
    assert vix_leading_signals._update_tier3_streak(True, today=_D2) == 2  # real call still sees D1 as prior, unaffected by the dry run
    # A dry run on a day that would otherwise BREAK the streak must not
    # poison the real state for a later real call the same day.
    vix_leading_signals._update_tier3_streak(False, today=_D3, dry_run=True)
    assert vix_leading_signals._update_tier3_streak(True, today=_D3) == 3  # real call: D2 was confirmed, extends to 3


def test_streak_gap_with_no_calls_carries_forward_the_last_known_reading(isolated_leading_state):
    """Known limitation, documented rather than silently wrong: a silently-
    skipped day (e.g. the loop wasn't running) is indistinguishable from
    that day continuing the last-known reading -- the streak only ever
    finalizes ONE day per call regardless of how many calendar days
    actually elapsed, so a 2-day gap after a confirmed day still only adds
    +1 to the finalized streak (not +2), then today's own fresh reading
    adds one more on top."""
    vix_leading_signals._update_tier3_streak(True, today=_D1)
    # no call at all on _D2 (e.g. loop wasn't running) -- next call is _D3, two days later
    result = vix_leading_signals._update_tier3_streak(True, today=_D3)
    assert result == 2


# ── evaluate() — exit_level mapping + tier4-never-alone scoring ────────

@pytest.fixture
def isolated_leading_state(tmp_path, monkeypatch):
    state_file = tmp_path / "vix_leading_state.json"
    monkeypatch.setattr(vix_leading_signals, "VIX_LEADING_STATE_FILE", state_file)
    return state_file


def _patch_evaluate_deps(monkeypatch, tier1, tier2, tier3, tier4):
    monkeypatch.setattr(vix_leading_signals, "fetch_dated_series", lambda *a, **k: [(date.today(), 1.0), (date.today(), 2.0)])
    monkeypatch.setattr(vix_leading_signals, "_yf_closes", lambda *a, **k: [1.0, 2.0])
    monkeypatch.setattr(vix_leading_signals, "divergence_gate", lambda *a, **k: tier1)
    monkeypatch.setattr(vix_leading_signals, "compression_gate", lambda *a, **k: tier2)
    monkeypatch.setattr(vix_leading_signals, "term_structure_gate", lambda *a, **k: tier3)
    monkeypatch.setattr(vix_leading_signals, "skew_confirmer_gate", lambda *a, **k: tier4)


DAY1 = date(2026, 1, 5)
DAY2 = date(2026, 1, 6)
DAY3 = date(2026, 1, 7)


def test_evaluate_exit_level_0_when_nothing_confirmed(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, False, False, False)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.exit_level == 0
    assert result.score == 0


def test_evaluate_exit_level_1_on_tier1_only(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, True, False, False, False)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.exit_level == 1


def test_evaluate_exit_level_2_on_tier2_only(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, True, False, False)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.exit_level == 2


def test_evaluate_exit_level_2_wins_over_tier1_when_both_confirmed(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, True, True, False, False)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.exit_level == 2


def test_evaluate_single_day_of_tier3_does_not_reach_exit_level_3(isolated_leading_state, monkeypatch):
    """A single day's confirmation isn't enough on its own --
    VIX_LEADING_TIER3_CONFIRM_DAYS (2 by default) requires a SECOND
    consecutive day. See test_evaluate_exit_level_3_after_two_consecutive_
    days below for the case that does reach it."""
    _patch_evaluate_deps(monkeypatch, False, False, True, False)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.tier3_term_structure is True  # raw reading still confirmed today
    assert result.tier3_confirmed_days == 1
    assert result.exit_level == 0  # but not sustained long enough to flatten


def test_evaluate_exit_level_3_after_two_consecutive_days(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, False, True, False)
    vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY2)
    assert result.tier3_confirmed_days == 2
    assert result.exit_level == 3


def test_evaluate_streak_resets_if_a_day_in_between_is_not_confirmed(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, False, True, False)
    vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)  # day 1: confirmed
    monkeypatch.setattr(vix_leading_signals, "term_structure_gate", lambda *a, **k: False)
    vix_leading_signals.evaluate(15.0, 18.0, today=DAY2)  # day 2: NOT confirmed -- breaks the streak
    monkeypatch.setattr(vix_leading_signals, "term_structure_gate", lambda *a, **k: True)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY3)  # day 3: confirmed again -- streak restarts at 1
    assert result.tier3_confirmed_days == 1
    assert result.exit_level == 0


def test_evaluate_repeated_calls_same_day_do_not_inflate_the_streak(isolated_leading_state, monkeypatch):
    """The fast exit-poll loop calls evaluate() many times per day (every
    ~15s-5min) -- repeated same-day calls must not let 2 calls on ONE day
    satisfy a 2-CONSECUTIVE-DAY requirement."""
    _patch_evaluate_deps(monkeypatch, False, False, True, False)
    vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)  # same day, second poll
    result2 = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)  # same day, third poll
    assert result.tier3_confirmed_days == 1
    assert result2.tier3_confirmed_days == 1
    assert result2.exit_level == 0


def test_evaluate_tier3_wins_even_with_tier2_also_confirmed(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, True, True, False)
    vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY2)
    assert result.exit_level == 3


def test_evaluate_tier4_never_counted_or_actioned_alone(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, False, False, True)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.exit_level == 0
    assert result.score == 0
    assert result.tier4_skew_confirmer is True  # still surfaced, just not scored/actioned alone


def test_evaluate_score_counts_tier1_2_3_not_4(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, True, True, True, True)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.score == 3  # score reflects TODAY's raw tier3 reading, independent of the sustained streak


def test_evaluate_dry_run_does_not_persist_tier3_streak(isolated_leading_state, monkeypatch):
    _patch_evaluate_deps(monkeypatch, False, False, True, False)
    vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)  # real: day 1 confirmed
    preview = vix_leading_signals.evaluate(15.0, 18.0, today=DAY2, dry_run=True)
    assert preview.exit_level == 3  # preview correctly shows what WOULD happen
    real = vix_leading_signals.evaluate(15.0, 18.0, today=DAY2)  # real call, same day -- must reach the same result on its own
    assert real.exit_level == 3
    assert real.tier3_confirmed_days == 2


def test_evaluate_fails_closed_when_vix_fetch_raises(isolated_leading_state, monkeypatch):
    def _raise(*a, **k):
        raise Exception("network down")
    monkeypatch.setattr(vix_leading_signals, "fetch_dated_series", _raise)
    monkeypatch.setattr(vix_leading_signals, "_yf_closes", lambda *a, **k: [1.0, 2.0])
    monkeypatch.setattr(vix_leading_signals, "term_structure_gate", lambda *a, **k: False)
    result = vix_leading_signals.evaluate(15.0, 18.0, today=DAY1)
    assert result.tier1_divergence is False
    assert result.tier2_compression is False
