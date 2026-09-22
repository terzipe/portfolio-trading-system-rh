"""
MarketData adapter — UW_OFF_ALPACA_CUTOVER_PLAN.md, Phase P0.

One place every caller gets a market-data client from, instead of each
importing data.unusual_whales directly. Backend selected via the
DATA_BACKEND env var:
  "uw"    (default) — byte-for-byte today's behavior, pure UW passthrough.
  "dual"  (P1/P2/P3, added 2026-09-19, completed 2026-09-21) — mixed
          authority, decided PER METHOD (see _DualBackend's own docstring
          for the exact split and why). ALL FOUR methods are now non-UW-
          authoritative -- UW is called on every one purely for continued
          comparison logging, and can never affect what's returned. This
          is now functionally the full cutover: nothing in a live
          decision path reads from UW anymore. UW stays wired in only
          because the comparison logging is free confidence -- dropping
          it (and the UW_API_KEY dependency entirely) is P4/P5, a
          deliberate later step, not something this phase does on its
          own. Every comparison is logged to
          data/vix/market_data_shadow_log.jsonl.

UWError itself is untouched (still raised directly from
data/unusual_whales.py, still caught by the existing `except UWError`
clauses in vix_regime.py/vix_options.py/vix_longvol_gates.py) — those get
widened to also catch MarketDataError once "alpaca_fred" is real and can
raise on its own, not before.

Real call surface, audited 2026-09-19 across monitor/vix_regime.py,
monitor/vix_signals.py (fetch_ticker_history()/fetch_uvxy_history() — these
call .ohlc() on whatever client object they're handed, duck-typed, so they
need NO changes as backends are added), monitor/vix_options.py,
monitor/vix_positions.py, and the 3 root loop scripts (loop_daily_vix.py,
loop_intraday_vix.py, loop_svix_exit_monitor.py):

  - last_price(ticker) -> float | None
  - ohlc(ticker, candle_size="1d", **params) -> dict
      Raw payload shaped {"data": [{"close": ..., "market_time":
      "r"|"pr"|"po"}, ...]} — the UW shape. A future non-UW backend must
      reshape its own bars into this so fetch_ticker_history()'s existing
      regular-session filtering keeps working with zero changes to it.
  - vix_term() -> dict
      Same shape UW's vix_term() already returns: vix, vix3m, source,
      warning|error.
  - option_chain(ticker, greeks=True) -> dict
      Raw payload shaped {"data": [{"option_type", "expires", "strike",
      "nbbo_bid", "nbbo_ask", "delta", "open_interest"}, ...]} — the UW
      shape. monitor/vix_options.py's own DTE/liquidity filtering reads
      exactly these field names, so a non-UW backend must reshape into
      this, not raise (see data/alpaca_options.py, P3).

option_contracts() / flow_alerts() / ws_connect() are NOT part of this
interface — audited 2026-09-19, nothing in the live VIX Trader BOT calls
them (dead surface on UnusualWhalesClient).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone


class MarketDataError(Exception):
    """Backend-agnostic market-data failure. UW's own UWError is left as
    its own exception type in P0 (see module docstring) — this class exists
    so P1+ backends have somewhere to raise into without inventing a new
    error type per backend."""


class _UWBackend:
    """Delegates every call to the existing UnusualWhalesClient, unchanged.
    This is the entirety of P0's behavior — a pure passthrough, no new
    logic, no new failure modes."""

    name = "uw"

    def __init__(self) -> None:
        from data.unusual_whales import get_client as _uw_get_client
        self._uw = _uw_get_client()

    def last_price(self, ticker: str) -> float | None:
        return self._uw.last_price(ticker)

    def ohlc(self, ticker: str, candle_size: str = "1d", **params) -> dict:
        return self._uw.ohlc(ticker, candle_size=candle_size, **params)

    def vix_term(self) -> dict:
        return self._uw.vix_term()

    def option_chain(self, ticker: str, greeks: bool = True) -> dict:
        return self._uw.option_chain(ticker, greeks=greeks)


def _shadow_log_path():
    from config import VIX_DATA_DIR
    return VIX_DATA_DIR / "market_data_shadow_log.jsonl"


def _log_shadow_diff(
    method: str, ticker: str | None,
    returned_value, returned_source: str,
    compared_value, compared_source: str, compared_error: str | None,
) -> None:
    """Append one line to the shadow-comparison log. Never raises -- a
    logging failure must not affect the value actually being returned to
    the caller. Field names are vendor-neutral (returned_* / compared_*)
    rather than uw_*/alpaca_* because WHICH vendor is "returned" vs.
    "compared" differs per method -- see _DualBackend's docstring. Diff is
    only meaningful for scalar-ish returns; for others we just record
    whether the comparison call succeeded."""
    try:
        diff_pct = None
        if isinstance(returned_value, (int, float)) and isinstance(compared_value, (int, float)) and returned_value:
            diff_pct = (compared_value - returned_value) / returned_value
        row = {
            "at": datetime.now(timezone.utc).isoformat(),
            "method": method, "ticker": ticker,
            "returned_value": returned_value, "returned_source": returned_source,
            "compared_value": compared_value, "compared_source": compared_source,
            "diff_pct": diff_pct, "compared_error": compared_error,
        }
        with open(_shadow_log_path(), "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[market_data:dual] shadow-log write failed (non-fatal): {exc}", file=sys.stderr)


class _DualBackend:
    """Mixed-authority backend -- which side is authoritative is decided
    PER METHOD, not globally, per what's actually been verified so far
    (UW_OFF_ALPACA_CUTOVER_PLAN.md P1):

      last_price() -- Alpaca is now AUTHORITATIVE (returned value); UW is
        called alongside only to log a diff. Flipped 2026-09-21 after a
        full live RTH session of shadow data: 2,671 comparisons, mean
        diff 0.04%, max 1.6% -- UW and Alpaca agree tightly, nothing like
        the ohlc()/vix_term() discrepancies. On an Alpaca failure,
        last_price() fails closed (returns None) rather than falling back
        to UW -- every caller of last_price() already treats a None
        price as "no quote this cycle, skip" (see e.g. vix_positions.py's
        `uw.last_price(ticker) or avg_cost` and the loop scripts' `_quote()`
        helpers), so this is a pre-existing convention, not a new one.

      ohlc() -- Alpaca is now AUTHORITATIVE (returned value); UW is called
        alongside only to log a diff, kept purely for continued
        observation. Flipped 2026-09-19 after the shadow log caught UW's
        own .ohlc() returning data wildly inconsistent with BOTH Alpaca
        and yfinance for UVXY/VXX (off by ~3x / ~1.9x, no split-like
        discontinuity anywhere in 252 days of history -- consistent with a
        long-stale feed, not a transient blip). This was silently feeding
        Gate C momentum, the FADE_SPIKE_PUTS UVXY-history check, and SVIX
        manual-campaign ride-mode's momentum check. On an Alpaca failure,
        ohlc() fails CLOSED ({"data": []}) rather than falling back to
        UW's now-proven-unreliable series -- fetch_ticker_history()
        already treats too-short/empty history as "no momentum decision",
        same fail-closed convention as everywhere else in this codebase.

      vix_term() -- FRED/Yahoo (data.fred_yahoo_term.FredYahooTermStructure)
        is now AUTHORITATIVE (returned value); UW is called alongside only
        to log a diff, kept purely for continued observation. Flipped
        2026-09-19, same day as ohlc(), after confirming live (and getting
        explicit sign-off given how central this is -- it feeds every
        posture/gate decision) that UW's synthetic estimate runs ~3pt /
        ~20% above the real VIX, with FRED and yfinance agreeing with each
        other exactly across the last 10 sessions -- e.g. Gate A's cheap-
        vol threshold (14.97) was actually crossed on 2026-09-18 (real
        close 14.81) but read "no" under UW's inflated ~18.0. The
        VIX/VIX3M ratio itself was also distorted (0.945 UW vs 0.810 real
        -- materially different contango depth), not just the level. On a
        FRED+Yahoo failure, vix_term() fails CLOSED (vix=vix3m=None,
        error set) rather than falling back to UW's now-proven-unreliable
        estimate -- vix_regime.py already treats a None vix as "no data,
        fail closed to CASH", the same convention as everywhere else.

      option_chain() -- Alpaca (data.alpaca_options.AlpacaOptions) is now
        AUTHORITATIVE (returned value); UW is called alongside only to
        log a diff. Flipped 2026-09-21, live-verified: 100% real bid/ask
        coverage on UVXY/VXX in every DTE window this codebase actually
        filters to (10-21d and 21-45d). Two deliberate gaps vs. UW's
        payload, both audited as harmless to every live caller (see
        data/alpaca_options.py's module docstring for the detail):
        open_interest is always None (Alpaca's option snapshot has no OI
        field; _is_liquid()'s OI check already tolerates None as
        "unknown", not "illiquid"), and only ~40-45% of contracts carry a
        live delta (pick_call() already has a full-pool fallback for
        when nothing in range has one). On an Alpaca failure,
        option_chain() fails CLOSED ({"data": []}) rather than falling
        back to UW -- every picker (pick_put/pick_call/get_contract_quote)
        already treats an empty/no-match chain as "no contract found",
        the same fail-closed convention as everywhere else.

    Whichever side is NOT authoritative for a given method can never raise
    into the caller -- its failure is logged and swallowed."""

    name = "dual"

    def __init__(self) -> None:
        self._primary = _UWBackend()
        try:
            from data.alpaca_quotes import AlpacaQuotes
            self._secondary = AlpacaQuotes()
        except Exception as exc:  # noqa: BLE001
            print(f"[market_data:dual] Alpaca secondary unavailable, shadow logging disabled: {exc}", file=sys.stderr)
            self._secondary = None
        try:
            from data.alpaca_options import AlpacaOptions
            self._options_source = AlpacaOptions()
        except Exception as exc:  # noqa: BLE001
            print(f"[market_data:dual] Alpaca options unavailable, option_chain() will fail closed: {exc}", file=sys.stderr)
            self._options_source = None
        from data.fred_yahoo_term import FredYahooTermStructure
        self._term_source = FredYahooTermStructure()  # authoritative for vix_term() -- see class docstring

    def _shadow(self, method: str, ticker: str | None, returned_value, returned_source: str,
               compared_source: str, compare_fn) -> None:
        """Fetch the comparison side and log a diff. Never raises, never
        affects `returned_value` -- the comparison side's failure is
        recorded, not propagated."""
        try:
            compared_value = compare_fn()
            _log_shadow_diff(method, ticker, returned_value, returned_source, compared_value, compared_source, None)
        except Exception as exc:  # noqa: BLE001
            _log_shadow_diff(method, ticker, returned_value, returned_source, None, compared_source, str(exc))

    def last_price(self, ticker: str) -> float | None:
        # Alpaca is authoritative here -- see class docstring. No fallback
        # to UW on failure: fail closed (None) instead, matching every
        # caller's existing "no quote this cycle" handling of a None price.
        if self._secondary is None:
            _log_shadow_diff("last_price", ticker, None, "alpaca", None, "uw",
                             "Alpaca unavailable -- failing closed")
            return None
        try:
            val = self._secondary.last_price(ticker)
        except Exception as exc:  # noqa: BLE001
            _log_shadow_diff("last_price", ticker, None, "alpaca", None, "uw", f"Alpaca (now authoritative) failed: {exc}")
            return None

        self._shadow("last_price", ticker, val, "alpaca", "uw", lambda: self._primary.last_price(ticker))
        return val

    @staticmethod
    def _last_regular_close(payload: dict) -> float | None:
        try:
            regular = [r for r in payload.get("data", []) if r.get("market_time") == "r"]
            return float(regular[-1]["close"]) if regular else None
        except Exception:  # noqa: BLE001
            return None

    def ohlc(self, ticker: str, candle_size: str = "1d", **params) -> dict:
        # Alpaca is authoritative here -- see class docstring. No fallback
        # to UW on failure: fail closed instead, since UW's own ohlc() has
        # been proven unreliable for these tickers.
        if self._secondary is None:
            _log_shadow_diff("ohlc_last_close", ticker, None, "alpaca", None, "uw",
                             "Alpaca unavailable -- failing closed, not falling back to UW's known-stale data")
            return {"data": []}
        try:
            val = self._secondary.ohlc(ticker, candle_size=candle_size, **params)
        except Exception as exc:  # noqa: BLE001
            _log_shadow_diff("ohlc_last_close", ticker, None, "alpaca", None, "uw", f"Alpaca (now authoritative) failed: {exc}")
            return {"data": []}

        alpaca_close = self._last_regular_close(val)
        self._shadow(
            "ohlc_last_close", ticker, alpaca_close, "alpaca", "uw",
            lambda: self._last_regular_close(self._primary.ohlc(ticker, candle_size=candle_size, **params)),
        )
        return val

    def vix_term(self) -> dict:
        # FRED/Yahoo is authoritative here -- see class docstring. No
        # fallback to UW on failure: fail closed instead, since UW's own
        # synthetic estimate has been proven to run ~20% high for a
        # sustained period, not a one-off blip worth falling back to.
        try:
            val = self._term_source.vix_term()
        except Exception as exc:  # noqa: BLE001
            val = {
                "vix": None, "vix3m": None, "vx1": None, "vx2": None,
                "source": None, "warning": None,
                "error": f"FRED/Yahoo term structure (now authoritative) failed: {exc}",
                "fetched_at": time.time(),
            }

        try:
            uw_term = self._primary.vix_term()
            uw_vix, uw_vix3m, uw_err = uw_term.get("vix"), uw_term.get("vix3m"), uw_term.get("error")
        except Exception as exc:  # noqa: BLE001
            uw_vix = uw_vix3m = None
            uw_err = str(exc)
        _log_shadow_diff("vix_term_vix", "VIX", val.get("vix"), "fred_yahoo", uw_vix, "uw", uw_err)
        _log_shadow_diff("vix_term_vix3m", "VIX3M", val.get("vix3m"), "fred_yahoo", uw_vix3m, "uw", uw_err)
        return val

    def option_chain(self, ticker: str, greeks: bool = True) -> dict:
        # Alpaca is authoritative here -- see class docstring. No fallback
        # to UW on failure: fail closed instead, same convention as
        # ohlc()/vix_term().
        if self._options_source is None:
            _log_shadow_diff("option_chain_n", ticker, None, "alpaca", None, "uw",
                             "Alpaca options unavailable -- failing closed")
            return {"data": []}
        try:
            val = self._options_source.option_chain(ticker, greeks=greeks)
        except Exception as exc:  # noqa: BLE001
            _log_shadow_diff("option_chain_n", ticker, None, "alpaca", None, "uw", f"Alpaca (now authoritative) failed: {exc}")
            return {"data": []}

        # A full chain isn't a scalar -- log contract COUNT as the diff
        # proxy (a rough liquidity/coverage sanity check), not a price diff.
        alpaca_n = len(val.get("data", []))

        def _uw_n():
            uw_val = self._primary.option_chain(ticker, greeks=greeks)
            return len(uw_val.get("data", []))

        self._shadow("option_chain_n", ticker, alpaca_n, "alpaca", "uw", _uw_n)
        return val


_BACKENDS = {"uw": _UWBackend, "dual": _DualBackend}
_client = None
_client_backend_name: str | None = None


def get_client(backend: str | None = None):
    """Module-level singleton, one per (process, backend) — mirrors
    data.unusual_whales.get_client()'s one-per-process pattern, but
    re-inits if the requested backend differs from the cached one (so a
    runtime DATA_BACKEND flip, or a test passing an explicit backend,
    swaps cleanly instead of silently keeping a stale client)."""
    global _client, _client_backend_name
    backend = backend or os.getenv("DATA_BACKEND", "uw")
    if _client is not None and _client_backend_name == backend:
        return _client
    try:
        cls = _BACKENDS[backend]
    except KeyError:
        raise MarketDataError(
            f"unknown or not-yet-implemented DATA_BACKEND={backend!r} — implemented: "
            f"{sorted(_BACKENDS)} ('alpaca_fred' lands in P2+, see UW_OFF_ALPACA_CUTOVER_PLAN.md)"
        )
    _client = cls()
    _client_backend_name = backend
    return _client
