"""
Unusual Whales API Basic client — VIX Trader BOT's only market-data source.

SRS v1.4 §5-§6: replaces yfinance for the VIX sleeve. REST only on Basic
($150/mo, 40k req/day, no WebSocket, no CME futures tape). Advanced-tier
features (WS, CME VX1/VX2) are capability flags, not a fork — flipping
UW_HAS_WEBSOCKET / UW_HAS_CME_FUTURES after an account upgrade must not
require touching this module's callers.

VIX ≠ a tradable share. This client fetches VXX/UVXY/SVIX and VIX/VIX3M
quotes; nothing here "buys VIX."
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import requests

from config import UW_API_KEY, UW_TIER, UW_HAS_WEBSOCKET, UW_HAS_CME_FUTURES

_BASE_URL = "https://api.unusualwhales.com"
_QUOTE_CACHE_TTL = 30       # seconds — SRS §6.3
_CHAIN_CACHE_TTL = 120      # seconds — SRS §6.3
_TIMEOUT = 10
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.5

_USAGE_HEADERS = (
    "x-uw-token-req-limit",
    "x-uw-daily-req-count",
    "x-uw-req-per-minute-remaining",
)


class UWError(Exception):
    """Raised when UW is unreachable or returns a non-2xx after retries."""


@dataclass
class _CacheEntry:
    value: Any
    saved_at: float = field(default_factory=time.time)

    def age(self) -> float:
        return time.time() - self.saved_at


class UnusualWhalesClient:
    """
    Thin bearer-auth REST client. One instance per process is enough —
    the quote/chain caches are instance-scoped so a daily and intraday
    loop each get their own short-lived cache.
    """

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or UW_API_KEY
        self.tier = UW_TIER
        self.has_websocket = UW_HAS_WEBSOCKET
        self.has_cme_futures = UW_HAS_CME_FUTURES
        self._session = session or requests.Session()
        self._quote_cache: dict[str, _CacheEntry] = {}
        self._chain_cache: dict[str, _CacheEntry] = {}
        self.last_usage_headers: dict[str, str] = {}

    # ── transport ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.api_key:
            raise UWError("UW_API_KEY not set")

        url = f"{_BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.request(
                    method, url, headers=headers, timeout=_TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(_BACKOFF_BASE ** attempt)
                continue

            self._log_usage_headers(resp.headers)

            if resp.status_code == 429:
                # SRS §6.1: "If 429s appear, slow the loop to 30 min" — that's
                # an operator/config action, not something to retry-loop here.
                last_exc = UWError(f"429 on {path}")
                time.sleep(_BACKOFF_BASE ** attempt)
                continue
            if resp.status_code >= 500:
                last_exc = UWError(f"{resp.status_code} on {path}")
                time.sleep(_BACKOFF_BASE ** attempt)
                continue
            if resp.status_code >= 400:
                raise UWError(f"{resp.status_code} on {path}: {resp.text[:300]}")

            return resp.json()

        raise UWError(f"UW request failed after {_MAX_RETRIES} attempts: {last_exc}")

    def _log_usage_headers(self, headers) -> None:
        found = {h: headers[h] for h in _USAGE_HEADERS if h in headers}
        if found:
            self.last_usage_headers = found
            print(f"[unusual_whales] usage: {found}")

    # ── quotes / OHLC ────────────────────────────────────────────────────
    # Confirmed against a live pull 2026-08-18 (account on Basic, no
    # `volatility` add-on): GET /api/stock/{ticker}/quote, price at
    # data.last_trade.price (string). SVIX/VXX/UVXY all 200. VIX/VIX3M do
    # NOT work here — see vix_term() below.
    def quote(self, ticker: str) -> dict:
        cached = self._quote_cache.get(ticker)
        if cached and cached.age() < _QUOTE_CACHE_TTL:
            return cached.value
        data = self._request("GET", f"/api/stock/{ticker}/quote")
        self._quote_cache[ticker] = _CacheEntry(data)
        return data

    def last_price(self, ticker: str) -> float | None:
        """Convenience wrapper — every caller wants a float, not the raw
        nested quote payload. Use this instead of hand-parsing quote()."""
        try:
            return _extract_last(self.quote(ticker))
        except UWError:
            return None

    def ohlc(self, ticker: str, candle_size: str = "1d", **params) -> dict:
        """candle_size must be one of: 1m,5m,10m,15m,30m,1h,4h,1d,1w (confirmed via OpenAPI spec)."""
        return self._request("GET", f"/api/stock/{ticker}/ohlc/{candle_size}", params=params)

    def vix_term(self) -> dict:
        """
        Spot VIX + VIX3M. VX1/VX2 only if UW_HAS_CME_FUTURES is on after an
        Advanced upgrade — SRS §7.1/§7.6, Impl Plan §2 method list.

        CONFIRMED against a live pull 2026-08-18, on this account's current
        plan (Basic, no `volatility` add-on):
          - GET /api/stock/VIX/quote  -> 404 (VIX is an index, not a stock —
            UW exposes no spot quote for it under /api/stock/).
          - GET /api/stock/VIX3M/quote -> 422 (not a recognized ticker).
          - GET /api/volatility/vix-term-structure -> 403
            "volatility_scope_required" — the endpoint the SRS assumed would
            carry VIX/VIX3M requires a separate paid add-on this account
            does not have.
          - GET /api/stock/VIX/option-chains -> 200, works, and has good
            expiry density (weekly-ish, e.g. 2026-09-16 at 29 DTE and
            2026-11-18 at 92 DTE on the day this was captured) — used below.

        Tries the paid endpoint first (zero code changes needed if the
        add-on is purchased later), then falls back to a **synthetic**
        estimate via put-call parity on the VIX option chain: for the
        expiry closest to a target DTE, the strike where call/put mids are
        closest is approximately the market's forward level for that
        expiry (C - P ≈ K - F, ignoring discounting, negligible at these
        DTEs/strikes), so `level ≈ strike + (call_mid - put_mid)`. One
        near-30-day expiry proxies "VIX", one near-93-day expiry proxies
        "VIX3M" — consistent with how VIX3M is itself defined (3-month
        constant maturity). This is a genuine approximation, not the
        official Cboe SOQ print: it inherits whatever bid-ask noise exists
        on that day's VIX option book, and is undefined if no expiry near
        the target exists or every candidate strike has a one-sided quote.
        `source` is always set to say which path produced the numbers, and
        `warning` (not `error`) is set on the synthetic path so callers can
        distinguish "no data" from "approximated data" without treating the
        latter as a hard failure.
        """
        cached = self._quote_cache.get("__vix_term__")
        if cached and cached.age() < _QUOTE_CACHE_TTL:
            return cached.value

        result = {
            "vix": None, "vix3m": None, "vx1": None, "vx2": None,
            "source": None, "error": None, "warning": None, "fetched_at": time.time(),
        }

        try:
            payload = self._request("GET", "/api/volatility/vix-term-structure")
            latest = payload.get("data", payload).get("latest", {})
            result["vix"] = latest.get("vix") or latest.get("spot")
            result["vix3m"] = latest.get("vix3m")
            result["source"] = "vix_term_structure_addon"
        except UWError as exc:
            try:
                chain = self.option_chain("VIX", greeks=True)
                vix_level, vix_dte = _synthetic_index_level(chain, _VIX_TARGET_DTE)
                vix3m_level, vix3m_dte = _synthetic_index_level(chain, _VIX3M_TARGET_DTE)
                if vix_level is not None and vix3m_level is not None:
                    result["vix"] = round(vix_level, 2)
                    result["vix3m"] = round(vix3m_level, 2)
                    result["source"] = "synthetic_put_call_parity"
                    result["warning"] = (
                        f"vix-term-structure add-on unavailable ({exc}); using put-call-parity "
                        f"estimate from VIX option chain (vix@{vix_dte}DTE, vix3m@{vix3m_dte}DTE) "
                        f"instead of the official Cboe print."
                    )
                else:
                    result["error"] = (
                        f"vix-term-structure unavailable ({exc}) AND synthetic put-call-parity "
                        f"fallback found no usable near-30-day/93-day VIX option expiry pair "
                        f"with two-sided quotes."
                    )
            except UWError as exc2:
                result["error"] = f"vix-term-structure unavailable ({exc}); synthetic fallback also failed: {exc2}"

        if self.has_cme_futures:
            # Advanced upgrade path — VX1/VX2 becomes primary, VIX3M stays
            # the fallback for >=1 week of agreement-checking (SRS §6.1.1).
            # Endpoint not yet confirmed against a live pull — same
            # fail-closed contract as above if it errors.
            try:
                futures = self._request("GET", "/api/futures/VX")
                result["vx1"] = futures.get("vx1")
                result["vx2"] = futures.get("vx2")
                result["source"] = "vx1_vx2"
            except UWError:
                pass  # fall back to whatever vix/vix3m populated above

        self._quote_cache["__vix_term__"] = _CacheEntry(result)
        return result

    # ── options ──────────────────────────────────────────────────────────
    def option_chain(self, ticker: str, greeks: bool = True) -> dict:
        key = f"chain:{ticker}:{greeks}"
        cached = self._chain_cache.get(key)
        if cached and cached.age() < _CHAIN_CACHE_TTL:
            return cached.value
        data = self._request(
            "GET", f"/api/stock/{ticker}/option-chains", params={"greeks": str(greeks).lower()}
        )
        self._chain_cache[key] = _CacheEntry(data)
        return data

    def option_contracts(self, ticker: str, expiry: str | None = None, option_type: str | None = None) -> dict:
        params = {}
        if expiry:
            params["expiry"] = expiry
        if option_type:
            params["option_type"] = option_type
        return self._request("GET", f"/api/stock/{ticker}/option-contracts", params=params)

    # ── flow (never cached — SRS §6.3) ──────────────────────────────────
    def flow_alerts(self, ticker: str, **params) -> dict:
        """Per-ticker, confirmed via OpenAPI spec: GET /api/stock/{ticker}/flow-alerts
        (there is no bulk multi-ticker flow-alerts endpoint on this API)."""
        return self._request("GET", f"/api/stock/{ticker}/flow-alerts", params=params)

    # ── WebSocket (stub — must stay disabled on Basic) ─────────────────
    def ws_connect(self):
        """
        Optional accelerator, SRS §6.1.1/§6.2. Must not connect unless
        UW_HAS_WEBSOCKET=true — Basic tier has no WS entitlement and this
        would just fail, or worse, silently no-op in a way that looks like
        a working fast path.
        """
        if not self.has_websocket:
            raise UWError("ws_connect() called but UW_HAS_WEBSOCKET=false")
        raise NotImplementedError(
            "WS listener not implemented — build after an Advanced upgrade "
            "(SRS §6.1.1, Impl Plan §14 step 8)."
        )


_VIX_TARGET_DTE = 30    # "spot VIX" proxy expiry
_VIX3M_TARGET_DTE = 93  # 3-month constant-maturity proxy expiry
_MAX_PARITY_GAP = 2.0   # reject the estimate if the best call/put mid gap exceeds this (bad/stale quotes)


def _mid(bid, ask) -> float | None:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 and a <= 0:
        return None
    return (b + a) / 2


def _synthetic_index_level(chain: dict, target_dte: int) -> tuple[float | None, int | None]:
    """
    Approximate an index's forward level via put-call parity: for the
    expiry closest to target_dte, find the strike where call and put mids
    are closest (true-ATM strike ≈ forward level), then
    level ≈ strike + (call_mid - put_mid). See vix_term()'s docstring for
    the full rationale and caveats. Returns (level, actual_dte) or
    (None, None) if no expiry near target_dte has a usable two-sided quote.
    """
    rows = chain.get("data", [])
    if not rows or not isinstance(rows[0], dict):
        return None, None  # some option-chains calls return bare symbol strings, not contracts

    by_expiry: dict[str, dict[str, dict]] = defaultdict(dict)
    for c in rows:
        expiry, strike, opt_type = c.get("expires"), c.get("strike"), c.get("option_type")
        if not expiry or strike is None or opt_type not in ("call", "put"):
            continue
        by_expiry[expiry].setdefault(strike, {})[opt_type] = c

    today = date.today()

    def _dte_of(expiry: str) -> int:
        return (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days

    future_expiries = [(abs(_dte_of(e) - target_dte), e) for e in by_expiry if _dte_of(e) > 0]
    if not future_expiries:
        return None, None
    future_expiries.sort()
    best_expiry = future_expiries[0][1]
    actual_dte = _dte_of(best_expiry)

    best_gap, best_level = None, None
    for strike, pair in by_expiry[best_expiry].items():
        call, put = pair.get("call"), pair.get("put")
        if not call or not put:
            continue
        call_mid, put_mid = _mid(call.get("nbbo_bid"), call.get("nbbo_ask")), _mid(put.get("nbbo_bid"), put.get("nbbo_ask"))
        if call_mid is None or put_mid is None:
            continue
        gap = abs(call_mid - put_mid)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best_level = float(strike) + (call_mid - put_mid)

    if best_gap is None or best_gap > _MAX_PARITY_GAP:
        return None, None
    return best_level, actual_dte


def _extract_last(quote_payload: dict) -> float | None:
    """
    Extract last trade price. Primary path confirmed against a live pull
    2026-08-18 (SVIX/VXX/UVXY all matched): {"data": {"last_trade":
    {"price": "26.1", ...}, ...}}. Remaining fallback keys are untested
    guesses kept only as a defensive net for payload shapes not yet seen
    (e.g. if a future endpoint nests differently).
    """
    data = quote_payload.get("data", quote_payload)
    if not isinstance(data, dict):
        return None

    last_trade = data.get("last_trade")
    if isinstance(last_trade, dict) and "price" in last_trade:
        try:
            return float(last_trade["price"])
        except (TypeError, ValueError):
            pass

    for key in ("last", "last_price", "close", "price"):
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return None


def get_client() -> UnusualWhalesClient:
    """Module-level singleton — one cache/session per process."""
    global _client
    try:
        return _client
    except NameError:
        _client = UnusualWhalesClient()
        return _client


if __name__ == "__main__":
    # S0 acceptance (Impl Plan §2): print VIX, SVIX last, UVXY ATM put mid
    # without yfinance. As of 2026-08-18 on this account's Basic plan (no
    # `volatility` add-on), VIX/VIX3M will print as None with an `error`
    # explaining why — that is the correct, honest fail-closed result, not
    # a bug. See vix_term()'s docstring.
    c = get_client()
    term = c.vix_term()
    print(f"VIX={term['vix']} VIX3M={term['vix3m']} source={term['source']} error={term['error']}")
    print(f"SVIX last: {c.last_price('SVIX')}")
    chain = c.option_chain("UVXY")
    puts = [row for row in chain.get("data", []) if row.get("option_type") == "put"]
    print(f"UVXY puts available: {len(puts)} (first: {puts[0] if puts else None})")
