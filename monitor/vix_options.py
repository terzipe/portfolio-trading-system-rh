"""
VIX Trader BOT — UW option-chain contract picker (SRS v1.4 §7.5-§7.6,
Impl Plan §4). Deliberately ignores the equity MIN_DTE=45 from config.py —
VIX_MIN_DTE/VIX_CALL_MIN_DTE are separate constants (SRS §2 decision #12).
Never selects 0-DTE.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from config import VIX_MIN_DTE, VIX_MAX_DTE, VIX_CALL_MIN_DTE, VIX_CALL_MAX_DTE
from data.unusual_whales import get_client, UWError

_WIDE_SPREAD_THRESHOLD = 0.08  # bid-ask / mid > 8% -> too illiquid (SRS §7.5)
_MIN_OI_FLOOR = 50             # config floor; adjust once UW OI distribution is known


@dataclass
class ContractPick:
    ticker: str
    option_type: str  # "put" | "call"
    strike: float
    expiry: str
    dte: int
    bid: float
    ask: float
    mid: float
    delta: float | None
    open_interest: int | None
    fallback: bool  # True if this is the VXX fallback (put path only)


def _dte(expiry: str) -> int:
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    return (exp_date - date.today()).days


def _safe_float(value, default: float = 0.0) -> float:
    """UW returns nbbo_bid/nbbo_ask/strike as JSON null (not just absent or
    "0") for some illiquid strikes — confirmed live 2026-08-19 on a real
    UVXY put chain, which crashed float(None) before this existed.
    contract.get(key, default) only substitutes default when the key is
    *missing*, not when it's present with value None, so this can't be
    fixed by tweaking .get() defaults alone."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value) -> float | None:
    """Same null-tolerance as _safe_float, but for fields like delta where
    None is a meaningful "unknown", not a fallback to 0.0."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid


def _is_liquid(contract: dict) -> bool:
    # Field names confirmed against a live UVXY option-chains pull
    # 2026-08-18: nbbo_bid / nbbo_ask (not bid/ask).
    bid, ask = _safe_float(contract.get("nbbo_bid")), _safe_float(contract.get("nbbo_ask"))
    oi = contract.get("open_interest")
    if _spread_pct(bid, ask) > _WIDE_SPREAD_THRESHOLD:
        return False
    if oi is not None and oi < _MIN_OI_FLOOR:
        return False
    return True


def _candidates(chain: dict, option_type: str, min_dte: int, max_dte: int) -> list[dict]:
    """
    Confirmed against a live UVXY option-chains pull 2026-08-18: contracts
    are under chain["data"] (a flat list, no pagination wrapper seen), and
    the expiry field is named "expires" (not "expiry"/"expiration_date").
    strike/delta come back as strings, not numbers.
    """
    contracts = chain.get("data", [])
    out = []
    for c in contracts:
        if c.get("option_type") != option_type:
            continue
        expiry = c.get("expires")
        if not expiry:
            continue
        dte = _dte(expiry)
        if dte <= 0:
            continue  # never 0-DTE
        if min_dte <= dte <= max_dte:
            out.append({**c, "expiry": expiry, "dte": dte})
    return out


def get_contract_quote(ticker: str, expiry: str, strike: float, option_type: str) -> tuple[float, float] | None:
    """
    Live (bid, ask) — dollars per *share* — for a specific contract. Matches
    on expiry (exact string) + strike (float, small tolerance for
    representation drift) + option_type. Returns None — fail closed, never
    guess — only if the chain fetch fails or no exact match is found; a
    matched contract with no real market (bid=ask=0) still returns (0.0,
    0.0) so callers can decide what that means for their own use (e.g.
    get_contract_mark() below treats it as "no mark", but a caller pricing
    a close order might instead want to fall back to a different price).
    """
    uw = get_client()
    try:
        chain = uw.option_chain(ticker, greeks=True)
    except UWError:
        return None

    rows = chain.get("data", [])
    if not rows or not isinstance(rows[0], dict):
        return None

    for c in rows:
        if c.get("option_type") != option_type:
            continue
        if c.get("expires") != expiry:
            continue
        if abs(_safe_float(c.get("strike")) - strike) > 0.01:
            continue
        return _safe_float(c.get("nbbo_bid")), _safe_float(c.get("nbbo_ask"))

    return None


def get_contract_mark(ticker: str, expiry: str, strike: float, option_type: str) -> float | None:
    """
    Live mid-price mark (dollars per *share*, not per contract — caller
    multiplies by 100 to match the RH Tracker cost_basis convention) for a
    specific held contract. Used by vix_positions.py to compute pnl_pct
    for option positions — previously always None, which meant
    ROLL_OPTION/CLOSE_OPTION could never fire (SRS §9 needs a real pnl_pct)
    and unrealized P&L was scoped to shares only.

    Thin wrapper over get_contract_quote() — returns None if the quote
    fetch/match fails, or if the matched contract has no real market
    (bid=ask=0, same "no real market" condition _size_and_explain_option()
    guards on the entry path).
    """
    quote = get_contract_quote(ticker, expiry, strike, option_type)
    if quote is None:
        return None
    bid, ask = quote
    mid = (bid + ask) / 2
    return mid if mid > 0 else None


def pick_put(spot_price: float, primary_ticker: str = "UVXY", fallback_ticker: str = "VXX") -> ContractPick | None:
    """
    10-21 DTE, ~10% OTM to slightly ITM. UVXY primary; falls back to VXX
    if UVXY's best candidate is too wide/thin (SRS §7.5).
    """
    uw = get_client()
    target_strike = spot_price * 0.90  # ~10% OTM for a put = lower strike

    for ticker, is_fallback in ((primary_ticker, False), (fallback_ticker, True)):
        try:
            chain = uw.option_chain(ticker, greeks=True)
        except UWError:
            continue
        candidates = _candidates(chain, "put", VIX_MIN_DTE, VIX_MAX_DTE)
        if not candidates:
            continue
        candidates.sort(key=lambda c: abs(_safe_float(c.get("strike")) - target_strike))
        best = candidates[0]
        if not _is_liquid(best) and not is_fallback:
            continue  # try VXX fallback
        bid, ask = _safe_float(best.get("nbbo_bid")), _safe_float(best.get("nbbo_ask"))
        return ContractPick(
            ticker=ticker, option_type="put",
            strike=_safe_float(best.get("strike")),
            expiry=best["expiry"], dte=best["dte"],
            bid=bid, ask=ask, mid=(bid + ask) / 2,
            delta=_optional_float(best.get("delta")),
            open_interest=best.get("open_interest"),
            fallback=is_fallback,
        )
    return None


def pick_call(ticker: str = "VXX", delta_range: tuple[float, float] = (0.40, 0.60)) -> ContractPick | None:
    """21-45 DTE, delta 0.40-0.60 (SRS §7.6, Aug-Oct tactical long-vol).

    Liquidity-filtered via _is_liquid() the same way pick_put() is —
    previously picked purely on delta proximity with no liquidity check at
    all, an inconsistency with pick_put()'s spread/OI check (found during
    design review, fixed 2026-08-26; vix_executor.py's skip_reason for a
    None return already said "no liquid ... call found", so this was
    always the intended behavior, just never implemented). Unlike
    pick_put(), there's no second ticker to fall back to (VXX is the only
    ticker LONG_VOL_TACTICAL trades) — instead of falling back to a
    different underlying, this walks the delta-sorted pool and returns the
    first candidate that clears the liquidity bar, only refusing (None) if
    nothing in the pool does.
    """
    uw = get_client()
    try:
        chain = uw.option_chain(ticker, greeks=True)
    except UWError:
        return None

    candidates = _candidates(chain, "call", VIX_CALL_MIN_DTE, VIX_CALL_MAX_DTE)
    lo, hi = delta_range
    in_range = [c for c in candidates if _optional_float(c.get("delta")) is not None and lo <= _optional_float(c["delta"]) <= hi]
    pool = in_range or candidates
    if not pool:
        return None

    mid_target = (lo + hi) / 2
    pool.sort(key=lambda c: abs((_optional_float(c.get("delta")) or mid_target) - mid_target))
    best = next((c for c in pool if _is_liquid(c)), None)
    if best is None:
        return None
    bid, ask = _safe_float(best.get("nbbo_bid")), _safe_float(best.get("nbbo_ask"))
    return ContractPick(
        ticker=ticker, option_type="call",
        strike=_safe_float(best.get("strike")),
        expiry=best["expiry"], dte=best["dte"],
        bid=bid, ask=ask, mid=(bid + ask) / 2,
        delta=_optional_float(best.get("delta")),
        open_interest=best.get("open_interest"),
        fallback=False,
    )
