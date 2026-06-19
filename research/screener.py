"""
Research phase — Step 1.
Sends a structured prompt to Claude to screen tickers based on account
constraints. Returns a list of candidate tickers with theses.
"""
import json
import datetime
import anthropic
from config import ANTHROPIC_API_KEY, ACCOUNT_BUDGET, MAX_POSITION_PCT, MIN_DTE

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are a quantitative equity and options analyst.
You produce structured JSON output only — no prose outside the JSON block.
Your role: given a set of constraints, screen for equity and options trade candidates.
Each candidate must have a clear catalyst, reasonable IV environment, and fit the stated risk profile."""


def screen_tickers(
    risk_profile: str = "moderate-aggressive",
    holding_period_days: int = 30,
    sectors: list[str] | None = None,
    extra_context: str = "",
) -> list[dict]:
    """
    Returns a list of candidate dicts:
    [
      {
        "ticker": "NOW",
        "name": "ServiceNow",
        "asset_type": "option",          # "option" or "share"
        "thesis": "...",
        "catalyst": "...",
        "catalyst_timing": "near|medium|long",
        "sector": "enterprise software",
        "iv_environment": "low|moderate|high",
        "market_cap": "large|mid|small",
        "score": 8.5,
        "warnings": ["..."]
      }
    ]
    """
    sector_str = ", ".join(sectors) if sectors else "any sector"
    prompt = f"""
Screen for strong trade candidates given these constraints:

- Total account budget: ${ACCOUNT_BUDGET:,.0f}
- Max position size: {MAX_POSITION_PCT*100:.0f}% of budget (${ACCOUNT_BUDGET * MAX_POSITION_PCT:,.0f})
- Min DTE for options: {MIN_DTE} days
- Risk profile: {risk_profile}
- Target holding period: {holding_period_days} days
- Preferred sectors: {sector_str}
- Strategy: LEAPs (1yr+ call options) on large/mid-cap names with reasonable IV;
  direct shares on small-cap names where options are illiquid or IV is too expensive
- Today's date: {datetime.date.today()}

{extra_context}

Return ONLY a JSON array of candidate objects. Each must include:
ticker, name, asset_type (option|share), thesis, catalyst, catalyst_timing (near|medium|long),
sector, iv_environment (low|moderate|high), market_cap (large|mid|small), score (0-10), warnings (array).

Aim for 6-10 candidates spanning different sectors and catalyst windows.
"""
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
