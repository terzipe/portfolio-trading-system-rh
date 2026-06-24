"""
Layer 0 — Universe Builder.
Assembles the ticker watchlist from two sources:
  1. Live Robinhood positions (both margin and agentic accounts)
  2. Top LS equity fund LONG candidates (scored_universe_latest.csv)

Returns a list of dicts with at least {"ticker", "source"}.
Held positions also carry quantity/avg_cost/market_value/pnl fields
so downstream layers can use them for alerts.
"""
import csv
from pathlib import Path

from config import RH_USERNAME, RH_PASSWORD

_LS_CSV = Path(__file__).parent.parent.parent / "ls_equity_fund" / "output" / "scored_universe_latest.csv"
_MARGIN_ACCOUNT  = "579611880"
_AGENTIC_ACCOUNT = "725024723"
_LS_TOP_N = 10   # number of top LONG picks to include as watchlist


def _rh_login():
    import robin_stocks.robinhood as rh
    rh.login(username=RH_USERNAME, password=RH_PASSWORD, store_session=True)
    return rh


def _live_positions(rh) -> list[dict]:
    """Pull held equity positions from both accounts."""
    rows: list[dict] = []
    for acct in (_MARGIN_ACCOUNT, _AGENTIC_ACCOUNT):
        try:
            raw = rh.get_open_stock_positions(account_number=acct) or []
            for p in raw:
                qty = float(p.get("quantity", 0))
                if qty <= 0:
                    continue
                instr  = rh.get_instrument_by_url(p["instrument"])
                ticker = instr.get("symbol", "")
                if not ticker:
                    continue
                avg    = float(p.get("average_buy_price", 0))
                quote  = rh.get_latest_price(ticker)
                price  = float(quote[0]) if quote else avg
                mv     = qty * price
                pnl    = (price - avg) * qty
                rows.append({
                    "ticker":       ticker,
                    "source":       "held",
                    "account":      acct,
                    "quantity":     qty,
                    "avg_cost":     avg,
                    "price":        price,
                    "market_value": mv,
                    "pnl":          pnl,
                    "pnl_pct":      (price / avg - 1) * 100 if avg else 0,
                    "type":         "share",
                })
        except Exception as e:
            print(f"  [layer0] could not load positions for account {acct}: {e}")
    return rows


def _ls_top_longs(held_tickers: set[str]) -> list[dict]:
    """Return top N LONG-flagged tickers from LS CSV, excluding already-held tickers."""
    if not _LS_CSV.exists():
        return []
    candidates: list[tuple[float, str]] = []
    try:
        with open(_LS_CSV, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("long_short_flag") != "LONG":
                    continue
                try:
                    score = float(row["composite"])
                except (ValueError, KeyError):
                    continue
                ticker = row.get("ticker", "")
                if ticker and ticker not in held_tickers:
                    candidates.append((score, ticker))
    except Exception as e:
        print(f"  [layer0] could not read LS CSV: {e}")
        return []
    candidates.sort(reverse=True)
    return [
        {"ticker": t, "source": "ls_long", "composite_score": s,
         "quantity": 0, "market_value": 0, "pnl": 0, "pnl_pct": 0, "type": "watchlist"}
        for s, t in candidates[:_LS_TOP_N]
    ]


def run() -> list[dict]:
    """Return unified ticker universe: held positions + LS top longs."""
    print("  [layer0] logging into Robinhood...")
    try:
        rh = _rh_login()
        held = _live_positions(rh)
    except Exception as e:
        print(f"  [layer0] Robinhood login failed ({e}), using empty held list")
        held = []

    held_tickers = {p["ticker"] for p in held}
    print(f"  [layer0] held: {sorted(held_tickers)}")

    ls_picks = _ls_top_longs(held_tickers)
    ls_tickers = [p["ticker"] for p in ls_picks]
    print(f"  [layer0] LS top LONG picks: {ls_tickers}")

    universe = held + ls_picks
    print(f"  [layer0] total universe: {len(universe)} tickers")
    return universe
