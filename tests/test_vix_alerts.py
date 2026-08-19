import json
import time

from monitor import vix_alerts


def test_buy_skip_due_to_flag_off_is_suppressed():
    suppress, reason = vix_alerts.should_suppress("BUY_SVIX_SHARES", False, "ENABLE_VIX_AUTO_BUY=false")
    assert suppress is True
    assert "Phase T" in reason


def test_sell_skip_due_to_flag_off_is_not_suppressed():
    # ENABLE_VIX_AUTO_SELL going false is worth surfacing, unlike auto-buy
    # staying off during the phased rollout.
    suppress, reason = vix_alerts.should_suppress("SELL_SVIX_ALL", False, "ENABLE_VIX_AUTO_SELL=false")
    assert suppress is False


def test_buy_skip_for_a_different_reason_is_not_suppressed():
    # e.g. sleeve cap or session-state rejections are real news, not flag noise.
    suppress, reason = vix_alerts.should_suppress("BUY_SVIX_SHARES", False, "sleeve % cap would be exceeded")
    assert suppress is False


def test_executed_action_is_never_suppressed():
    suppress, reason = vix_alerts.should_suppress("BUY_SVIX_SHARES", True, "")
    assert suppress is False


# ── Duplicate roll-alert suppression (Impl Plan §9: "Suppress duplicate
# rolls") ────────────────────────────────────────────────────────────────

_POSITION = {"ticker": "UVXY", "expiry": "2099-01-15", "strike": 54.0, "option_type": "put"}


def test_first_roll_alert_is_never_suppressed(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_alerts, "VIX_ROLL_ALERT_STATE_FILE", tmp_path / "roll_alert_state.json")

    suppress, reason = vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.26)

    assert suppress is False


def test_second_alert_within_4h_same_pnl_is_suppressed(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_alerts, "VIX_ROLL_ALERT_STATE_FILE", tmp_path / "roll_alert_state.json")

    vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.26)
    suppress, reason = vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.27)  # only 1pp moved

    assert suppress is True
    assert "< 4h" in reason


def test_second_alert_pnl_moved_10pp_is_not_suppressed(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_alerts, "VIX_ROLL_ALERT_STATE_FILE", tmp_path / "roll_alert_state.json")

    vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.26)
    suppress, reason = vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.37)  # 11pp moved

    assert suppress is False


def test_second_alert_after_cooldown_expires_is_not_suppressed(monkeypatch, tmp_path):
    state_file = tmp_path / "roll_alert_state.json"
    monkeypatch.setattr(vix_alerts, "VIX_ROLL_ALERT_STATE_FILE", state_file)

    key = vix_alerts._roll_position_key(_POSITION)
    stale_timestamp = time.time() - (5 * 3600)  # 5 hours ago, past the 4h cooldown
    state_file.write_text(json.dumps({key: {"alerted_at": stale_timestamp, "pnl_pct": 0.26}}))

    suppress, reason = vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.26)  # same P&L, but cooldown expired

    assert suppress is False


def test_different_position_is_not_suppressed_by_another_positions_alert(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_alerts, "VIX_ROLL_ALERT_STATE_FILE", tmp_path / "roll_alert_state.json")

    vix_alerts.should_suppress_roll(_POSITION, pnl_pct=0.26)
    other_position = {"ticker": "VXX", "expiry": "2099-02-19", "strike": 20.0, "option_type": "call"}
    suppress, reason = vix_alerts.should_suppress_roll(other_position, pnl_pct=0.26)

    assert suppress is False


def test_none_pnl_falls_back_to_plain_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_alerts, "VIX_ROLL_ALERT_STATE_FILE", tmp_path / "roll_alert_state.json")

    vix_alerts.should_suppress_roll(_POSITION, pnl_pct=None)
    suppress, reason = vix_alerts.should_suppress_roll(_POSITION, pnl_pct=None)

    assert suppress is True  # can't tell if P&L moved -> cooldown alone governs
