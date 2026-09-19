"""
Alpaca-backed market data — UW_OFF_ALPACA_CUTOVER_PLAN.md, Phase P1.

last_price() / ohlc() for SVIX/VXX/UVXY off Alpaca's free (IEX) market-data
feed, using the SAME trading credentials already in .env — no new API key.
Live-verified 2026-09-19 against the real account.

ohlc() reshapes Alpaca's daily bars into data.unusual_whales's raw payload
shape ({"data": [{"close": ..., "market_time": "r"}, ...]}) so
monitor/vix_signals.py's fetch_ticker_history()/fetch_uvxy_history() need
ZERO changes — they already just call .ohlc() on whatever client object
they're handed and filter market_time=="r" themselves. Alpaca daily bars
are already regular-session-only, so every row is tagged "r".

Gotcha found live 2026-09-19: alpaca-py's StockBarsRequest with no
explicit start/end silently returns ZERO bars for every symbol (including
AAPL), no error, no exception — always pass an explicit start/end. A
_DELAY_BUFFER_MIN buffer on `end` avoids asking for data inside the free
feed's realtime delay window.

Only last_price/ohlc are implemented here — vix_term() (P2, FRED/Yahoo)
and option_chain() (P3, deferred) are NOT covered; this class is meant to
be composed into data.market_data's dual/alpaca_fred backends alongside a
UW (or later FRED) delegate for those two.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from data.market_data import MarketDataError

_DELAY_BUFFER_MIN = 20   # stay clear of the free/IEX feed's realtime delay window
_HISTORY_DAYS = 90       # calendar days of bars to fetch -- comfortably covers every
                         # caller's tail-slice window (longest today is ~16 sessions)


class AlpacaQuotes:
    """last_price()/ohlc() only -- see module docstring for what's deliberately
    NOT implemented (vix_term, option_chain)."""

    name = "alpaca"

    def __init__(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        key = os.getenv("ALPACA_API_KEY_ID")
        secret = os.getenv("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise MarketDataError(
                "ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not set -- cannot build the Alpaca market-data client"
            )
        self._client = StockHistoricalDataClient(key, secret)

    def last_price(self, ticker: str) -> float | None:
        from alpaca.data.requests import StockLatestTradeRequest

        try:
            trades = self._client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=[ticker]))
            trade = trades.get(ticker) if hasattr(trades, "get") else trades[ticker]
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"Alpaca last_price({ticker!r}) failed: {exc}") from exc
        return float(trade.price) if trade is not None else None

    def ohlc(self, ticker: str, candle_size: str = "1d", **params) -> dict:
        if candle_size != "1d":
            raise MarketDataError(f"AlpacaQuotes.ohlc() only supports candle_size='1d' (got {candle_size!r})")
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.now(timezone.utc) - timedelta(minutes=_DELAY_BUFFER_MIN)
        start = end - timedelta(days=_HISTORY_DAYS)
        try:
            bars = self._client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=[ticker], timeframe=TimeFrame.Day, start=start, end=end)
            )
            rows = bars[ticker] if ticker in bars.data else []
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"Alpaca ohlc({ticker!r}) failed: {exc}") from exc
        # every Alpaca daily bar is regular-session -- tag "r" to match UW's shape
        return {"data": [{"close": str(b.close), "market_time": "r"} for b in rows]}
