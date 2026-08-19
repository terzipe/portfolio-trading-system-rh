"""
VIX Trader BOT — Alpaca "gold copy" balance/P&L for the dashboard. Unlike
vix_positions.py (which marks positions against Unusual Whales for the
bot's own trading *decisions*, e.g. ROLL_OPTION/CLOSE_OPTION thresholds),
everything here is sourced directly from Alpaca's own account state, so it
ties exactly to what Alpaca itself reports as the balance — and, via
/account/activities, includes every fill that touches the account
regardless of origin (the bot, the dashboard's manual flatten button, or a
trade placed directly in Alpaca's own UI). Display-only; nothing here feeds
the bot's execution logic.

/account/activities has no typed method on alpaca-py's TradingClient (only
the request/response models exist, in alpaca.trading.models.TradeActivity)
— reached here via the client's untyped client.get(path, data) escape
hatch, confirmed live against the real paper account 2026-08-19.

Every function fails closed (empty/None on any API error, never raises) —
same convention as vix_options.get_contract_mark().
"""
from __future__ import annotations

_MAX_PAGES = 50  # defensive cap; a single paper account's fill history is tiny


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_fill_activities(client) -> list[dict]:
    """
    All FILL/partial_fill activities on the account, oldest first (reversed
    from Alpaca's default newest-first order, so fifo_realized_pnl() can
    consume them directly). Normalized to
    {"symbol", "side", "qty", "price", "transaction_time", "order_id"}.
    """
    rows: list[dict] = []
    page_token = None
    for _ in range(_MAX_PAGES):
        params = {"activity_types": "FILL", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            page = client.get("/account/activities", data=params)
        except Exception as exc:  # noqa: BLE001
            print(f"[vix_ledger] activities fetch failed: {exc}")
            break
        if not page:
            break
        rows.extend(page)
        if len(page) < 100:
            break
        page_token = page[-1].get("id")
        if not page_token:
            break

    normalized = [
        {
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "qty": _safe_float(r.get("qty")),
            "price": _safe_float(r.get("price")),
            "transaction_time": r.get("transaction_time"),
            "order_id": r.get("order_id"),
        }
        for r in rows
        if r.get("symbol") and r.get("qty") is not None and r.get("price") is not None
    ]
    normalized.sort(key=lambda r: r.get("transaction_time") or "")
    return normalized


def fifo_realized_pnl(activities: list[dict]) -> dict:
    """
    FIFO-match buy/sell fills per symbol into realized P&L. Same algorithm
    as the dashboard's local _fifo_realized_pnl(), but sourced from
    Alpaca's real fill history instead of the bot's own paper ledger — so
    this sees manual trades too, not just bot-placed ones.
    """
    from collections import defaultdict, deque

    lots: dict[str, deque] = defaultdict(deque)
    by_ticker: dict[str, float] = defaultdict(float)
    total = 0.0
    matched = 0

    for row in activities:
        symbol = row["symbol"]
        qty = row["qty"]
        price = row["price"]
        side = (row.get("side") or "").lower()
        if side == "buy":
            lots[symbol].append([qty, price])
        elif side == "sell":
            remaining = qty
            while remaining > 1e-9 and lots.get(symbol):
                lot_qty, lot_price = lots[symbol][0]
                take = min(remaining, lot_qty)
                realized = take * (price - lot_price)
                total += realized
                by_ticker[symbol] += realized
                matched += 1
                lot_qty -= take
                remaining -= take
                if lot_qty <= 1e-9:
                    lots[symbol].popleft()
                else:
                    lots[symbol][0][0] = lot_qty

    return {"total_dollars": total, "by_ticker": dict(by_ticker), "matched_trades": matched}


def account_snapshot(client) -> dict | None:
    """Real-time equity/cash/portfolio_value/buying_power — literally what
    Alpaca states the balance to be."""
    try:
        account = client.get_account()
    except Exception as exc:  # noqa: BLE001
        print(f"[vix_ledger] account snapshot failed: {exc}")
        return None
    return {
        "equity": _safe_float(account.equity),
        "cash": _safe_float(account.cash),
        "portfolio_value": _safe_float(account.portfolio_value),
        "buying_power": _safe_float(account.buying_power),
    }


def positions_snapshot(client) -> dict | None:
    """Per-position unrealized P&L straight from Alpaca's own Position
    fields — no UW dependency, no drift risk from a second price source."""
    try:
        positions = client.get_all_positions() or []
    except Exception as exc:  # noqa: BLE001
        print(f"[vix_ledger] positions snapshot failed: {exc}")
        return None

    by_ticker = {}
    total_unrealized = 0.0
    for p in positions:
        unrealized_pl = _safe_float(p.unrealized_pl)
        by_ticker[p.symbol] = {
            "qty": _safe_float(p.qty),
            "market_value": _safe_float(p.market_value),
            "unrealized_pl": unrealized_pl,
            "unrealized_plpc": _safe_float(p.unrealized_plpc),
            "cost_basis": _safe_float(p.cost_basis),
        }
        total_unrealized += unrealized_pl or 0.0

    return {"by_ticker": by_ticker, "total_unrealized_dollars": total_unrealized}
