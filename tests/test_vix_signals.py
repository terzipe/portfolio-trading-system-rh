"""
fetch_uvxy_history() regression test — UW's /ohlc/1d endpoint returns one
row per session *segment* per day (market_time: pr/r/po), not one row per
day, confirmed live 2026-08-19. A naive "last N rows" slice would mix
pre/post-market segments in with regular-session closes and undercount
actual trading days for the fade-spike +30%-in-10-sessions window.
"""
from monitor import vix_signals


def _row(date: str, close: str, market_time: str) -> dict:
    return {"date": date, "close": close, "market_time": market_time}


class _FakeUW:
    def __init__(self, rows):
        self._rows = rows

    def ohlc(self, ticker, candle_size="1d", **params):
        return {"data": self._rows}


def test_fetch_uvxy_history_filters_to_regular_session_only():
    # 3 trading days x 3 segments each (pre/regular/post) = 9 rows, but
    # only 3 should survive the market_time=="r" filter.
    rows = []
    for date, closes in [("2026-08-17", ("20.0", "20.5", "20.4")),
                          ("2026-08-18", ("20.4", "20.6", "20.5")),
                          ("2026-08-19", ("20.5", "19.8", "19.9"))]:
        rows.append(_row(date, closes[0], "pr"))
        rows.append(_row(date, closes[1], "r"))
        rows.append(_row(date, closes[2], "po"))

    history = vix_signals.fetch_uvxy_history(_FakeUW(rows), sessions=10)

    assert history == [20.5, 20.6, 19.8]  # regular-session closes only, in order


def test_fetch_uvxy_history_returns_none_on_too_few_closes():
    rows = [_row("2026-08-19", "20.5", "r")]  # only one regular-session row
    assert vix_signals.fetch_uvxy_history(_FakeUW(rows), sessions=10) is None


def test_fetch_uvxy_history_returns_none_on_uw_error():
    from data.unusual_whales import UWError

    class _FailingUW:
        def ohlc(self, ticker, candle_size="1d", **params):
            raise UWError("boom")

    assert vix_signals.fetch_uvxy_history(_FailingUW()) is None


def test_fetch_uvxy_history_respects_sessions_window():
    rows = [_row(f"2026-08-{d:02d}", str(20 + d), "r") for d in range(1, 21)]
    history = vix_signals.fetch_uvxy_history(_FakeUW(rows), sessions=5)
    assert len(history) == 5
    assert history == [36.0, 37.0, 38.0, 39.0, 40.0]  # last 5, in order


# ── decide_actions() LONG_VOL_TACTICAL — VXX/UVXY rotation (2026-09-01) ──

def test_decide_actions_buys_vxx_when_vxx_confirmed():
    actions = vix_signals.decide_actions(vix_signals.LONG_VOL_TACTICAL, positions=[], longvol_ticker="VXX")
    assert len(actions) == 1
    assert actions[0].action == vix_signals.BUY_VXX_CALL
    assert actions[0].ticker == "VXX"


def test_decide_actions_buys_uvxy_when_uvxy_confirmed():
    actions = vix_signals.decide_actions(vix_signals.LONG_VOL_TACTICAL, positions=[], longvol_ticker="UVXY")
    assert len(actions) == 1
    assert actions[0].action == vix_signals.BUY_UVXY_CALL
    assert actions[0].ticker == "UVXY"


def test_decide_actions_noop_when_posture_fires_but_no_ticker_resolved():
    """Fails closed rather than defaulting to VXX -- a stale caller not
    passing longvol_ticker must not buy blind."""
    actions = vix_signals.decide_actions(vix_signals.LONG_VOL_TACTICAL, positions=[], longvol_ticker=None)
    assert len(actions) == 1
    assert actions[0].action == vix_signals.NOOP


def test_decide_actions_holds_when_vxx_calls_already_held_even_if_uvxy_confirmed():
    """One still blocks the other (his call, 2026-09-01): holding VXX
    calls means no fresh UVXY buy either, even if UVXY's own gates
    confirmed this cycle."""
    vxx_position = {"ticker": "VXX", "type": "option"}
    actions = vix_signals.decide_actions(vix_signals.LONG_VOL_TACTICAL, positions=[vxx_position], longvol_ticker="UVXY")
    assert len(actions) == 1
    assert actions[0].action == vix_signals.HOLD


def test_decide_actions_holds_when_uvxy_calls_already_held_even_if_vxx_confirmed():
    uvxy_position = {"ticker": "UVXY", "type": "option"}
    actions = vix_signals.decide_actions(vix_signals.LONG_VOL_TACTICAL, positions=[uvxy_position], longvol_ticker="VXX")
    assert len(actions) == 1
    assert actions[0].action == vix_signals.HOLD
