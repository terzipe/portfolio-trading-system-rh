"""
One-time research phase.
Run this to have Claude screen tickers, pick contracts, and write positions.json.
Then review the output and edit positions.json before going live.
"""
import json
from research.screener import screen_tickers
from research.contract_picker import pick_all_contracts
from config import POSITIONS_FILE


def main():
    print("Step 1 — Screening tickers with Claude...")
    candidates = screen_tickers(
        risk_profile="moderate-aggressive",
        holding_period_days=30,
        extra_context="Focus on names with upcoming catalysts (earnings, product launches, policy events).",
    )
    print(f"  Found {len(candidates)} candidates")
    for c in candidates:
        print(f"    {c['ticker']:>6} [{c['asset_type']:>6}]  score={c['score']}  {c['catalyst_timing']} catalyst  {c['sector']}")

    print("\nStep 2 — Picking option contracts with Claude...")
    candidates = pick_all_contracts(candidates)

    print("\nCandidate summary:")
    print(json.dumps(candidates, indent=2))

    # Convert research output to positions.json format
    positions = []
    for c in candidates:
        if c["asset_type"] == "option" and c.get("contract"):
            ct = c["contract"]
            positions.append({
                "type": "option",
                "ticker": c["ticker"],
                "strike": ct["strike"],
                "expiry": ct["expiry"],
                "option_type": ct.get("option_type", "call"),
                "contracts": ct.get("suggested_contracts", 1),
                "cost_basis": ct.get("estimated_cost_per_contract", 0),
                "sector": c.get("sector", "unknown"),
            })
        elif c["asset_type"] == "share":
            positions.append({
                "type": "share",
                "ticker": c["ticker"],
                "quantity": 0,       # fill in manually after reviewing
                "cost_basis": 0.0,   # fill in after purchase
                "sector": c.get("sector", "unknown"),
            })

    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

    print(f"\nPositions written to {POSITIONS_FILE}")
    print("IMPORTANT: Review and edit positions.json before running run_daily.py.")
    print("Fill in quantity and cost_basis for share positions.")


if __name__ == "__main__":
    main()
