"""
VIX Trader BOT — normalize held SVIX/VXX/UVXY shares + options from an
authenticated RH session into the position-dict shape vix_signals.py and
vix_paper.py expect: {"ticker", "type", "quantity"/"contracts",
"cost_basis", "pnl_pct", "mid_price", "expiry", "strike", "option_type"}.

Kept separate from vix_session.py's shadow book (that's a minimal
ticker/quantity snapshot for BOOK_MISMATCH detection only, saved on every
HEALTHY pull). This module does the fuller valuation pass, called only
when session is HEALTHY/DEGRADED and we're about to decide actions.
"""
from __future__ import annotations

from config import VIX_ACCOUNT
from data.unusual_whales import get_client
from monitor.layer0_universe import _AGENTIC_ACCOUNT, _MARGIN_ACCOUNT

_ACCOUNT_NUMBERS = {"AGENTIC": _AGENTIC_ACCOUNT, "MARGIN": _MARGIN_ACCOUNT}
_VOL_TICKERS = {"SVIX", "VXX", "UVXY"}


def fetch_positions(rh) -> list[dict]:
    account_number = _ACCOUNT_NUMBERS.get(VIX_ACCOUNT, _AGENTIC_ACCOUNT)
    uw = get_client()
    out: list[dict] = []

    try:
        shares = rh.get_open_stock_positions(account_number=account_number) or []
    except Exception as exc:  # noqa: BLE001
        print(f"[vix_positions] equity positions fetch failed: {exc}")
        shares = []

    for p in shares:
        try:
            instr = rh.get_instrument_by_url(p["instrument"])
            ticker = instr.get("symbol", "")
        except Exception:  # noqa: BLE001
            continue
        if ticker not in _VOL_TICKERS:
            continue
        qty = float(p.get("quantity", 0))
        if qty <= 0:
            continue
        avg_cost = float(p.get("average_buy_price", 0))
        price = uw.last_price(ticker) or avg_cost
        pnl_pct = (price / avg_cost - 1) if avg_cost else 0.0
        out.append({
            "ticker": ticker, "type": "share", "quantity": qty,
            "cost_basis": avg_cost, "mid_price": price, "pnl_pct": pnl_pct,
        })

    try:
        options = rh.get_open_option_positions(account_number=account_number) or []
    except Exception as exc:  # noqa: BLE001
        print(f"[vix_positions] option positions fetch failed: {exc}")
        options = []

    for p in options:
        try:
            chain_symbol = p.get("chain_symbol", "")
        except AttributeError:
            continue
        if chain_symbol not in _VOL_TICKERS:
            continue
        contracts = float(p.get("quantity", 0))
        if contracts <= 0:
            continue
        cost_basis = float(p.get("average_open_price", 0)) * 100  # $/contract, RH Tracker convention
        out.append({
            "ticker": chain_symbol, "type": "option",
            "contracts": contracts, "cost_basis": cost_basis,
            "expiry": p.get("expiration_date"), "strike": float(p.get("strike_price", 0)),
            "option_type": p.get("type"),
            # pnl_pct left unset here — needs a live option mark from UW
            # option_chain(), wired by the caller once it has the chain
            # for roll/stop evaluation (vix_signals.decide_option_management).
            "pnl_pct": None,
        })

    return out


def unrealized_pnl(positions: list[dict]) -> dict:
    """
    Unrealized P&L on current holdings, scoped to shares (SVIX) only —
    option positions' pnl_pct is always None above (no live option mark
    wired yet), so option unrealized P&L isn't reliably computable until
    that gap is closed. Shared by both loop_daily_vix.py and
    loop_intraday_vix.py so the calculation lives in exactly one place.
    """
    total_dollars = 0.0
    total_cost = 0.0
    by_ticker: dict[str, dict] = {}
    for p in positions:
        if p.get("type") != "share":
            continue
        qty = p.get("quantity", 0)
        cost = p.get("cost_basis", 0)
        mid = p.get("mid_price")
        if mid is None or qty <= 0:
            continue
        dollars = (mid - cost) * qty
        total_dollars += dollars
        total_cost += cost * qty
        by_ticker[p["ticker"]] = {
            "dollars": dollars, "pct": (mid / cost - 1) if cost else None,
            "quantity": qty, "cost_basis": cost, "mid_price": mid,
        }
    return {
        "total_dollars": total_dollars,
        "total_pct": (total_dollars / total_cost) if total_cost else None,
        "by_ticker": by_ticker,
    }
