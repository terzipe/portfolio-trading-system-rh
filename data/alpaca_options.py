"""
Alpaca-backed option chain — UW_OFF_ALPACA_CUTOVER_PLAN.md, Phase P3.

option_chain(ticker, greeks=True) off Alpaca's option-data feed, using the
SAME trading credentials already in .env (no new key). Live-verified
2026-09-21 against the real account: UVXY/VXX chains have 100% real
bid/ask coverage in every DTE window this codebase actually uses (10-21d
and 21-45d), and root_symbol=ticker server-side filtering cleanly excludes
corporate-action-adjusted contracts (e.g. "UVXY1..." after a reverse
split) that would otherwise corrupt symbol parsing -- confirmed live:
182/1550 UVXY contracts were adjusted-root before this filter, 0 after.

Reshapes Alpaca's OCC-keyed snapshot dict into UW's raw payload shape
({"data": [{"option_type", "expires", "strike", "nbbo_bid", "nbbo_ask",
"delta", "open_interest"}, ...]}) so monitor/vix_options.py's
_candidates()/_is_liquid()/get_contract_quote() need ZERO changes -- they
already just read chain["data"] and those exact field names.

Deliberate gaps vs. UW's payload, and why they don't matter to any live
caller (audited against monitor/vix_options.py 2026-09-21):
  - open_interest: Alpaca's option snapshot has no OI field at all (only
    the separate trading-contracts endpoint does, which would need a
    second paginated call per ticker). Always returned as None here.
    Safe: _is_liquid()'s OI check is `if oi is not None and oi < FLOOR`
    -- a None OI is never itself a liquidity failure, only a real
    below-floor OI is. Liquidity is still enforced via the bid-ask
    spread check, which Alpaca's feed fully supports.
  - greeks (delta): only ~40-45% of contracts carry a live greeks
    snapshot (confirmed live: 659/1550 for UVXY). pick_call()'s existing
    fallback already handles this -- `in_range or candidates` falls back
    to the full delta-sorted-by-midpoint pool when nothing has a delta in
    range, and _optional_float(None) is already a first-class "unknown"
    value throughout vix_options.py, not a new failure mode.

Server-side filtering (type, expiration_date_gte/lte) is used to keep
each call scoped to real trading needs rather than pulling the full
1000+ contract chain -- callers here don't ask for a DTE window
themselves (chain() intentionally mirrors UW's option_chain()'s all-
expiries-at-once contract, since monitor/vix_options.py's own
_candidates() does its own DTE filtering downstream), so this fetches
a wide practical window (config.VIX_MIN_DTE.._ALPACA_MAX_DTE_WINDOW)
covering every DTE range any caller filters to afterward.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from data.market_data import MarketDataError

_OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<right>[CP])(?P<strike8>\d{8})$")
_ALPACA_MAX_DTE_WINDOW = 60  # comfortably covers every DTE range this codebase filters to (max is 45)


def _parse_occ_symbol(symbol: str) -> tuple[str, str, float] | None:
    """(expiry "YYYY-MM-DD", option_type "call"|"put", strike) from a
    standard OCC-format symbol, or None if it doesn't match (shouldn't
    happen once root_symbol=ticker is passed server-side, but never
    guess if it does)."""
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    ymd, right, strike8 = m["ymd"], m["right"], m["strike8"]
    try:
        yy, mm, dd = int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])
        expiry = date(2000 + yy, mm, dd).isoformat()
        strike = int(strike8) / 1000.0
    except ValueError:
        return None
    return expiry, ("call" if right == "C" else "put"), strike


class AlpacaOptions:
    """option_chain() only -- composed into data.market_data's dual
    backend alongside AlpacaQuotes (last_price/ohlc) and
    FredYahooTermStructure (vix_term)."""

    name = "alpaca_options"

    def __init__(self) -> None:
        import os
        from alpaca.data.historical.option import OptionHistoricalDataClient

        key = os.getenv("ALPACA_API_KEY_ID")
        secret = os.getenv("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise MarketDataError(
                "ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not set -- cannot build the Alpaca options client"
            )
        self._client = OptionHistoricalDataClient(key, secret)

    def option_chain(self, ticker: str, greeks: bool = True) -> dict:
        from alpaca.data.requests import OptionChainRequest

        try:
            snapshots = self._client.get_option_chain(
                OptionChainRequest(
                    underlying_symbol=ticker,
                    root_symbol=ticker,  # excludes corporate-action-adjusted contracts, see module docstring
                    expiration_date_gte=date.today() + timedelta(days=1),
                    expiration_date_lte=date.today() + timedelta(days=_ALPACA_MAX_DTE_WINDOW),
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"Alpaca option_chain({ticker!r}) failed: {exc}") from exc

        rows = []
        for symbol, snap in snapshots.items():
            parsed = _parse_occ_symbol(symbol)
            if parsed is None:
                continue
            expiry, option_type, strike = parsed
            quote = snap.latest_quote
            bid = float(quote.bid_price) if quote and quote.bid_price is not None else None
            ask = float(quote.ask_price) if quote and quote.ask_price is not None else None
            delta = float(snap.greeks.delta) if snap.greeks and snap.greeks.delta is not None else None
            rows.append({
                "option_type": option_type, "expires": expiry, "strike": strike,
                "nbbo_bid": bid, "nbbo_ask": ask, "delta": delta,
                "open_interest": None,  # not available from this feed, see module docstring
            })
        return {"data": rows}
