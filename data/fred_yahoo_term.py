"""
FRED/Yahoo-backed VIX/VIX3M term structure — UW_OFF_ALPACA_CUTOVER_PLAN.md
Phase P2.

vix_term() tries a fresh Yahoo intraday read first (^VIX/^VIX3M — the
REAL index level, not an estimate); on any failure (either ticker), falls
back to FRED's most recent daily close (VIXCLS/VXVCLS — always available,
the official Cboe settle, but only as fresh as the last FRED release).
Never guesses with only one side available: both tickers must succeed on
a given source, or that source is treated as failed and the next one is
tried.

Same return shape as UW's vix_term() (vix, vix3m, vx1, vx2, source,
warning, error, fetched_at) and the SAME never-raises contract (see
data.unusual_whales.UnusualWhalesClient.vix_term()'s docstring: always
returns a dict, sets `error` on total failure rather than raising) --
callers (monitor/vix_regime.py) already handle this shape unchanged.

fetched_at is the AGE OF THE UNDERLYING DATA POINT, not the API call
time: for Yahoo it's that 1-minute bar's own timestamp (seconds old
during RTH); for FRED it's that observation date's ~market-close time
(21:00 UTC / 4:15pm ET), which can be many hours old over a weekend or
holiday. This feeds vix_regime.py's EXISTING data_age_sec staleness gate
(VIX_STALE_SECONDS) unchanged -- no new staleness logic needed, just
honest timestamps (UW's own synthetic path always reports fetched_at=now,
since it computes live from the option chain on every call).

vx1/vx2 (CME futures) are always None here -- this account has no futures
data entitlement (UW_HAS_CME_FUTURES=false) and neither FRED nor this
Yahoo path source them; unchanged from UW's own behavior on this tier.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

_FRED_CLOSE_HOUR_UTC = 21  # ~4:15pm ET official Cboe settle, expressed in UTC
_YF_VIX_TICKER = "^VIX"
_YF_VIX3M_TICKER = "^VIX3M"


def _yahoo_intraday_level(ticker: str) -> tuple[float, float] | None:
    """(level, data_timestamp_epoch) from the latest available 1-minute
    bar, or None on any failure -- fails closed, never guesses."""
    try:
        import yfinance as yf
        hist = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=False)
    except Exception:
        return None
    if hist is None or hist.empty or "Close" not in hist:
        return None
    close = hist["Close"]
    if hasattr(close, "columns"):  # flatten yfinance's MultiIndex columns if present
        close = close.iloc[:, 0]
    close = close.dropna()
    if close.empty:
        return None
    try:
        ts_epoch = close.index[-1].to_pydatetime().timestamp()
        level = float(close.iloc[-1])
    except (AttributeError, TypeError, ValueError):
        return None
    return level, ts_epoch


def _fred_daily_close(series_id: str) -> tuple[float, float] | None:
    """(level, ~market_close_timestamp_epoch) from FRED's most recent
    observation, or None on failure."""
    from data.fred import fetch_dated_series, FredError
    try:
        series = fetch_dated_series(series_id, lookback_years=1)
    except FredError:
        return None
    if not series:
        return None
    obs_date, value = series[-1]
    close_dt = datetime(obs_date.year, obs_date.month, obs_date.day, _FRED_CLOSE_HOUR_UTC, tzinfo=timezone.utc)
    return float(value), close_dt.timestamp()


class FredYahooTermStructure:
    """vix_term() only -- deliberately not a full MarketDataClient (no
    last_price/ohlc/option_chain). Composed into data.market_data's dual
    backend alongside AlpacaQuotes, which covers those."""

    name = "fred_yahoo"

    def vix_term(self) -> dict:
        from data.fred import VIXCLS, VXVCLS

        vix_y = _yahoo_intraday_level(_YF_VIX_TICKER)
        vix3m_y = _yahoo_intraday_level(_YF_VIX3M_TICKER)
        if vix_y is not None and vix3m_y is not None:
            vix, vix_ts = vix_y
            vix3m, vix3m_ts = vix3m_y
            return {
                "vix": vix, "vix3m": vix3m, "vx1": None, "vx2": None,
                "source": "yahoo_intraday", "warning": None, "error": None,
                "fetched_at": min(vix_ts, vix3m_ts),
            }

        vix_f = _fred_daily_close(VIXCLS)
        vix3m_f = _fred_daily_close(VXVCLS)
        if vix_f is not None and vix3m_f is not None:
            vix, vix_ts = vix_f
            vix3m, vix3m_ts = vix3m_f
            return {
                "vix": vix, "vix3m": vix3m, "vx1": None, "vx2": None,
                "source": "fred_daily_close",
                "warning": (
                    "Yahoo intraday (^VIX/^VIX3M) unavailable; using FRED's most recent "
                    "daily close instead -- may be stale over a weekend/holiday."
                ),
                "error": None,
                "fetched_at": min(vix_ts, vix3m_ts),
            }

        return {
            "vix": None, "vix3m": None, "vx1": None, "vx2": None,
            "source": None, "warning": None,
            "error": "both Yahoo intraday (^VIX/^VIX3M) and FRED daily close (VIXCLS/VXVCLS) failed",
            "fetched_at": time.time(),
        }
