"""
Layer 1 — Data & Valuation.
Pulls spot prices and full options chain for every held position.
Computes Greeks locally via Black-Scholes.
Saves a dated snapshot to data/snapshots/.
"""
import json
import math
import datetime
import yfinance as yf
from scipy.stats import norm
from config import POSITIONS_FILE, SNAPSHOTS_DIR


def _bs_greeks(S, K, T, r, sigma, opt_type="call") -> dict:
    if T <= 0 or sigma <= 0:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "bs_price": 0}
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "call":
        delta = norm.cdf(d1)
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        delta = -norm.cdf(-d1)
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(d1)
    gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * norm.pdf(d1) * math.sqrt(T) / 100
    theta = (
        -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
        - r * K * math.exp(-r * T) * (norm.cdf(d2) if opt_type == "call" else norm.cdf(-d2))
    ) / 365
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "bs_price": price}


def value_positions(positions: list[dict]) -> list[dict]:
    """
    positions.json schema:
      share:  {type, ticker, quantity, cost_basis, sector}
      option: {type, ticker, strike, expiry, option_type, contracts, cost_basis, sector}
    cost_basis for options = price paid per contract (not per share).
    """
    RISK_FREE = 0.05
    today = datetime.date.today()
    valued = []

    for pos in positions:
        ticker = pos["ticker"]
        spot = yf.Ticker(ticker).fast_info.last_price

        if pos["type"] == "share":
            mv = spot * pos["quantity"]
            cost = pos["cost_basis"] * pos["quantity"]
            valued.append({**pos, "spot": spot, "market_value": mv, "pnl": mv - cost, "pnl_pct": (mv - cost) / cost * 100})

        elif pos["type"] == "option":
            expiry = datetime.datetime.strptime(pos["expiry"], "%Y-%m-%d").date()
            dte = (expiry - today).days
            T = dte / 365

            chain = yf.Ticker(ticker).option_chain(pos["expiry"])
            side = chain.calls if pos["option_type"] == "call" else chain.puts
            row = side[abs(side["strike"] - pos["strike"]) < 0.01]

            if row.empty:
                mark, iv = None, 0.3
            else:
                bid, ask = float(row["bid"].iloc[0]), float(row["ask"].iloc[0])
                mark = (bid + ask) / 2
                iv = float(row["impliedVolatility"].iloc[0])

            greeks = _bs_greeks(spot, pos["strike"], T, RISK_FREE, iv, pos["option_type"])
            effective_mark = mark if mark is not None else greeks["bs_price"]
            mv = effective_mark * 100 * pos["contracts"]
            cost = pos["cost_basis"] * pos["contracts"]
            valued.append({
                **pos,
                "spot": spot,
                "mark": effective_mark,
                "market_value": mv,
                "pnl": mv - cost,
                "pnl_pct": (mv - cost) / cost * 100,
                "dte": dte,
                "iv": iv,
                "greeks": greeks,
            })

    return valued


def run() -> list[dict]:
    with open(POSITIONS_FILE) as f:
        positions = json.load(f)
    valued = value_positions(positions)
    snapshot_path = SNAPSHOTS_DIR / f"{datetime.date.today().isoformat()}.json"
    with open(snapshot_path, "w") as f:
        json.dump(valued, f, indent=2, default=str)
    return valued
