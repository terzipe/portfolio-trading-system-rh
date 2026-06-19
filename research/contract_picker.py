"""
Research phase — Step 2.
For each option candidate, fetches the live chain via yfinance and asks
Claude to pick the best specific contract (strike + expiry).
"""
import json
import datetime
import yfinance as yf
import anthropic
from config import ANTHROPIC_API_KEY, MIN_DTE, ACCOUNT_BUDGET, MAX_POSITION_PCT

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are an options strategist.
You produce structured JSON only — no prose outside the JSON block.
Your role: given a filtered options chain and a trade thesis, select the single best
LEAP call contract (strike + expiry). Prioritize: delta 0.3-0.5, DTE > 180, reasonable IV."""


def _get_chain_summary(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    spot = t.fast_info.last_price
    expirations = [
        e for e in t.options
        if (datetime.datetime.strptime(e, "%Y-%m-%d").date() - datetime.date.today()).days >= MIN_DTE
    ]
    if not expirations:
        return {"ticker": ticker, "spot": spot, "chains": []}

    chains = []
    for exp in expirations[:4]:
        chain = t.option_chain(exp)
        calls = chain.calls[["strike", "bid", "ask", "impliedVolatility", "volume", "openInterest"]].head(15)
        chains.append({"expiry": exp, "calls": calls.to_dict(orient="records")})
    return {"ticker": ticker, "spot": spot, "chains": chains}


def pick_contract(ticker: str, thesis: str, budget_per_position: float | None = None) -> dict:
    """
    Returns:
    {
      "ticker": "NOW",
      "strike": 120.0,
      "expiry": "2027-01-15",
      "option_type": "call",
      "rationale": "...",
      "estimated_cost_per_contract": 850.0,
      "suggested_contracts": 2
    }
    """
    if budget_per_position is None:
        budget_per_position = ACCOUNT_BUDGET * MAX_POSITION_PCT

    chain_data = _get_chain_summary(ticker)
    prompt = f"""
Ticker: {ticker}
Spot price: ${chain_data['spot']:.2f}
Trade thesis: {thesis}
Budget for this position: ${budget_per_position:,.0f}

Options chain (calls only, DTE >= {MIN_DTE}):
{json.dumps(chain_data['chains'], indent=2)}

Pick the single best LEAP call contract. Return JSON with:
ticker, strike, expiry (YYYY-MM-DD), option_type, rationale, estimated_cost_per_contract, suggested_contracts.
"""
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # extract JSON from markdown fences if present
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue
    # try to find a {...} block directly
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        return json.loads(raw[start:end])
    return json.loads(raw)


def pick_all_contracts(candidates: list[dict]) -> list[dict]:
    """Augments each option candidate with a picked contract. Passes shares through unchanged."""
    for c in candidates:
        c["contract"] = pick_contract(c["ticker"], c["thesis"]) if c["asset_type"] == "option" else None
    return candidates
