"""
Layer 2 — Portfolio Analytics.
Aggregates Greeks, sector allocation, theta decay, and IV exposure across all positions.
"""
import datetime
from collections import defaultdict


def run(valued_positions: list[dict]) -> dict:
    total_mv = sum(p["market_value"] for p in valued_positions)
    total_pnl = sum(p["pnl"] for p in valued_positions)
    total_cost = total_mv - total_pnl

    agg_greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sector_alloc = defaultdict(float)
    option_rows = []
    share_rows = []

    for p in valued_positions:
        sector_alloc[p.get("sector", "unknown")] += p["market_value"]

        if p["type"] == "option":
            g = p.get("greeks", {})
            mult = p["contracts"] * 100
            for k in agg_greeks:
                agg_greeks[k] += g.get(k, 0) * mult
            option_rows.append({
                "ticker": p["ticker"],
                "strike": p["strike"],
                "expiry": p["expiry"],
                "dte": p["dte"],
                "iv_pct": round(p["iv"] * 100, 1),
                "mark": round(p["mark"], 2),
                "market_value": round(p["market_value"], 2),
                "pnl_pct": round(p["pnl_pct"], 1),
                "delta": round(g.get("delta", 0), 3),
                "theta_daily": round(g.get("theta", 0) * mult, 2),
            })
        else:
            share_rows.append({
                "ticker": p["ticker"],
                "quantity": p["quantity"],
                "spot": round(p["spot"], 2),
                "market_value": round(p["market_value"], 2),
                "pnl_pct": round(p["pnl_pct"], 1),
            })

    return {
        "as_of": datetime.date.today().isoformat(),
        "total_market_value": round(total_mv, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost else 0,
        "aggregate_greeks": {k: round(v, 4) for k, v in agg_greeks.items()},
        "daily_theta_bleed": round(agg_greeks["theta"], 2),
        "sector_allocation_pct": {k: round(v / total_mv * 100, 1) for k, v in sector_alloc.items()} if total_mv else {},
        "option_positions": option_rows,
        "share_positions": share_rows,
    }
