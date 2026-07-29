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


_PICKLE = Path.home() / ".tokens" / "robinhood.pickle"
_CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"


def _rh_login():
    """Inject/refresh the stored session token instead of always doing a full
    username/password login. A full login triggers Robinhood's device-approval
    push challenge, which nobody is around to approve on this unattended 9am
    cron — every day it burns retries polling get_prompts_status until Robinhood
    429s the endpoint. Reusing the pickled access/refresh token (same approach
    as regime_trader/dashboard/app.py) avoids the challenge entirely as long as
    the refresh token is still alive.
    """
    import pickle
    import time
    import base64
    import json
    import requests
    import robin_stocks.robinhood as rh
    from robin_stocks.robinhood.helper import update_session, set_login_state

    if _PICKLE.exists():
        try:
            with open(_PICKLE, "rb") as f:
                tok = pickle.load(f)
            access = tok.get("access_token", "")
            refresh = tok.get("refresh_token", "")
            device = tok.get("device_token", "")

            parts = access.split(".")
            exp = json.loads(base64.urlsafe_b64decode(parts[1] + "==")).get("exp", 0)

            if exp > time.time():
                set_login_state(True)
                update_session("Authorization", f"Bearer {access}")
                return rh

            resp = requests.post("https://api.robinhood.com/oauth2/token/", json={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": _CLIENT_ID,
                "device_token": device,
                "scope": "internal",
            }, timeout=10)
            new = resp.json()
            if "access_token" in new and "refresh_token" in new:
                tok["access_token"] = new["access_token"]
                tok["refresh_token"] = new["refresh_token"]
                with open(_PICKLE, "wb") as f:
                    pickle.dump(tok, f)
                set_login_state(True)
                update_session("Authorization", f"Bearer {new['access_token']}")
                return rh
        except Exception:
            pass  # fall through to full login

    result = rh.login(username=RH_USERNAME, password=RH_PASSWORD, store_session=True)
    if not result or "access_token" not in result:
        raise RuntimeError(
            "Robinhood session expired and refresh failed. Run rh_reauth.py "
            "on the machine (needs a device-approval tap on the phone):\n"
            "  /Users/pterzian/Desktop/TVClaude/rh_reauth.py"
        )
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
