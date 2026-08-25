"""
VIX Trader BOT — normalize held SVIX/VXX/UVXY shares + options from an
authenticated Alpaca paper-trading client into the position-dict shape
vix_signals.py and vix_paper.py expect: {"ticker", "type",
"quantity"/"contracts", "cost_basis", "pnl_pct", "mid_price", "expiry",
"strike", "option_type"}.

Kept separate from vix_session.py's shadow book (that's a minimal
ticker/quantity snapshot for BOOK_MISMATCH detection only, saved on every
HEALTHY pull). This module does the fuller valuation pass, called only
when session is HEALTHY/DEGRADED and we're about to decide actions.
"""
from __future__ import annotations

from alpaca.trading.enums import AssetClass

from data.unusual_whales import get_client
from monitor import vix_options

_VOL_TICKERS = {"SVIX", "VXX", "UVXY"}


def _parse_occ_symbol(symbol: str) -> tuple[str, str, float, str] | None:
    """
    Parse an OCC-format option symbol (e.g. "UVXY260115P00054000") into
    (root_ticker, expiry "YYYY-MM-DD", strike, option_type "put"/"call").
    Alpaca's option position/order symbols are root + 15 trailing characters
    (6-digit YYMMDD + C/P + 8-digit strike*1000) — the root is NOT padded
    to a fixed width like the raw OCC spec, so parsing anchors from the
    right rather than assuming a fixed root length. Returns None on any
    malformed symbol rather than guessing.
    """
    if len(symbol) < 15:
        return None
    root = symbol[:-15]
    date_part = symbol[-15:-9]
    cp = symbol[-9]
    strike_part = symbol[-8:]
    if cp not in ("C", "P") or not date_part.isdigit() or not strike_part.isdigit() or not root:
        return None
    try:
        expiry = f"20{date_part[0:2]}-{date_part[2:4]}-{date_part[4:6]}"
        strike = int(strike_part) / 1000.0
    except ValueError:
        return None
    return root, expiry, strike, ("call" if cp == "C" else "put")


def fetch_positions(client) -> list[dict]:
    uw = get_client()
    out: list[dict] = []

    try:
        positions = client.get_all_positions() or []
    except Exception as exc:  # noqa: BLE001
        print(f"[vix_positions] positions fetch failed: {exc}")
        return out

    for p in positions:
        if p.asset_class == AssetClass.US_EQUITY:
            ticker = p.symbol
            if ticker not in _VOL_TICKERS:
                continue
            qty = float(p.qty)
            if qty <= 0:
                continue
            avg_cost = float(p.avg_entry_price)
            price = uw.last_price(ticker) or avg_cost
            pnl_pct = (price / avg_cost - 1) if avg_cost else 0.0
            out.append({
                "ticker": ticker, "type": "share", "quantity": qty,
                "cost_basis": avg_cost, "mid_price": price, "pnl_pct": pnl_pct,
            })

        elif p.asset_class == AssetClass.US_OPTION:
            parsed = _parse_occ_symbol(p.symbol)
            if parsed is None:
                continue
            ticker, expiry, strike, option_type = parsed
            if ticker not in _VOL_TICKERS:
                continue
            contracts = float(p.qty)
            if contracts <= 0:
                continue
            cost_basis = float(p.avg_entry_price) * 100  # $/contract, RH Tracker convention

            # Live mark, sourced from Unusual Whales (broker-independent —
            # unaffected by the Alpaca migration). Fails closed (mid_price/
            # pnl_pct stay None) if the chain fetch fails or no exact
            # contract match is found — never guesses a mark.
            mark_per_share = vix_options.get_contract_mark(ticker, expiry, strike, option_type)
            mid_price = None
            pnl_pct = None
            if mark_per_share is not None and cost_basis:
                mid_price = mark_per_share * 100  # $/contract, matching cost_basis's convention
                pnl_pct = (mid_price - cost_basis) / cost_basis

            out.append({
                "ticker": ticker, "type": "option",
                "contracts": contracts, "cost_basis": cost_basis,
                "expiry": expiry, "strike": strike, "option_type": option_type,
                "mid_price": mid_price, "pnl_pct": pnl_pct,
            })

    return out


def unrealized_pnl(positions: list[dict]) -> dict:
    """
    Unrealized P&L on current holdings — shares and options both.
    cost_basis and mid_price are both already in "dollars per unit" terms
    for both position types (per-share for shares, per-contract for
    options — the RH Tracker convention), so (mid - cost) * qty works
    uniformly; only the quantity key name differs (quantity vs contracts).
    Shared by both loop_daily_vix.py and loop_intraday_vix.py so the
    calculation lives in exactly one place.
    """
    total_dollars = 0.0
    total_cost = 0.0
    by_ticker: dict[str, dict] = {}
    for p in positions:
        qty = p.get("quantity") if p.get("type") == "share" else p.get("contracts")
        cost = p.get("cost_basis", 0)
        mid = p.get("mid_price")
        if mid is None or not qty or qty <= 0:
            continue
        dollars = (mid - cost) * qty
        total_dollars += dollars
        total_cost += cost * qty
        key = p["ticker"] if p.get("type") == "share" else f"{p['ticker']} {p.get('expiry')} {p.get('strike')}{(p.get('option_type') or '?')[0]}"
        by_ticker[key] = {
            "dollars": dollars, "pct": (mid / cost - 1) if cost else None,
            "quantity": qty, "cost_basis": cost, "mid_price": mid,
        }
    return {
        "total_dollars": total_dollars,
        "total_pct": (total_dollars / total_cost) if total_cost else None,
        "by_ticker": by_ticker,
    }


def net_vol_exposure(positions: list[dict]) -> dict:
    """
    Net directional vol exposure across the SVIX ladder (short vol) and the
    options sleeve — visibility only, not an enforced limit. The two
    sleeves size against independent budgets with no shared risk view; this
    is a dashboard/reporting calc confirmed as wanted 2026-08-26, not a new
    sizing constraint on either sleeve.

    Direction, and a correction worth being explicit about: UVXY/VXX PUTS
    (FADE_SPIKE_PUTS) are a SHORT-vol bet — they profit when UVXY/VXX fall,
    i.e. when vol falls, the same direction as the SVIX ladder, not a
    hedge against it. Only UVXY/VXX CALLS (LONG_VOL_TACTICAL) are the
    natural offset. "the options sleeve is long vol" is only true for the
    LONG_VOL_TACTICAL half of it — the fade-spike half stacks with SVIX,
    it doesn't net against it.

    Uses raw dollar market value (mid_price, falling back to cost_basis if
    no live mark yet) as the exposure unit, matching every other sizing
    calc in this codebase (e.g. vix_executor._sleeve_pct_ok's sleeve_mv) —
    NOT a delta/beta-weighted Greek exposure. $1 of SVIX and $1 of VXX call
    premium do not move 1:1 with the same VIX change (options carry
    leverage/convexity that SVIX's -1x linear exposure doesn't); read
    net_dollars as a notional proxy, not a hedged Greek net.
    """
    short_vol_dollars = 0.0
    long_vol_dollars = 0.0
    legs = {"svix_ladder": 0.0, "puts": 0.0, "calls": 0.0}

    for p in positions:
        mark = p.get("mid_price")
        if p.get("type") == "share" and p.get("ticker") == "SVIX":
            qty = p.get("quantity") or 0
            value = qty * (mark if mark is not None else p.get("cost_basis", 0))
            short_vol_dollars += value
            legs["svix_ladder"] += value
        elif p.get("type") == "option" and p.get("ticker") in ("UVXY", "VXX"):
            contracts = p.get("contracts") or 0
            value = contracts * (mark if mark is not None else p.get("cost_basis", 0))
            if p.get("option_type") == "put":
                short_vol_dollars += value
                legs["puts"] += value
            elif p.get("option_type") == "call":
                long_vol_dollars += value
                legs["calls"] += value

    return {
        "long_vol_dollars": long_vol_dollars,
        "short_vol_dollars": short_vol_dollars,
        "net_dollars": long_vol_dollars - short_vol_dollars,
        "gross_dollars": long_vol_dollars + short_vol_dollars,
        "legs": legs,
    }
