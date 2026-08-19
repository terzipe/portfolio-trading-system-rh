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
