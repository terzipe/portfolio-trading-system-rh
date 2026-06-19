"""
Layer 3 — Market Context & News.
Part A (deterministic): macro gate imported from regime_trader (6-signal, cached).
Part B (Claude API): per-ticker news summary, sentiment, position impact — cached 24h.
"""
import json
import sqlite3
import time
import datetime
import sys
from pathlib import Path

import yfinance as yf
import anthropic
from config import ANTHROPIC_API_KEY, BASE_DIR

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Macro gate (regime_trader) ─────────────────────────────────────────
REGIME_TRADER_PATH = Path(__file__).parent.parent.parent / "regime_trader"
if str(REGIME_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(REGIME_TRADER_PATH))


def _macro_score() -> dict:
    try:
        from macro_gate import run_gate
        gate = run_gate()
        return {
            "composite_macro_score": gate.score,
            "posture": gate.posture,
            "sizing_pct": gate.sizing_pct,
            "regime": gate.posture.lower().replace("full", "risk-on")
                                          .replace("reduced", "neutral")
                                          .replace("defensive", "risk-off")
                                          .replace("cash", "risk-off"),
            "signals": {
                name: {"score": round(sig["score"], 1), "detail": sig["detail"]}
                for name, sig in gate.signals.items()
            },
        }
    except Exception as e:
        print(f"[layer3] macro_gate import failed ({e}), using fallback")
        return _macro_score_fallback()


def _macro_score_fallback() -> dict:
    vix = yf.Ticker("^VIX").fast_info.last_price
    spy_hist = yf.Ticker("SPY").history(period="5d")
    breadth_5d = float(spy_hist["Close"].pct_change().iloc[-1] * 100) if len(spy_hist) >= 2 else 0
    hyg = yf.Ticker("HYG").fast_info.last_price
    lqd = yf.Ticker("LQD").fast_info.last_price
    vix_score = max(0, min(100, int((50 - vix) * 2)))
    breadth_score = max(0, min(100, int(50 + breadth_5d * 10)))
    credit_score = max(0, min(100, int(hyg / lqd * 100)))
    composite = round(vix_score * 0.4 + breadth_score * 0.4 + credit_score * 0.2, 1)
    regime = "risk-on" if composite >= 60 else ("neutral" if composite >= 40 else "risk-off")
    return {
        "composite_macro_score": composite,
        "posture": "FULL" if composite >= 75 else ("REDUCED" if composite >= 50 else "DEFENSIVE"),
        "sizing_pct": 1.0 if composite >= 75 else (0.6 if composite >= 50 else 0.3),
        "regime": regime,
        "signals": {
            "vix_level": {"score": vix_score, "detail": f"VIX={vix:.1f}"},
            "breadth":   {"score": breadth_score, "detail": f"5d SPY {breadth_5d:+.2f}%"},
            "credit":    {"score": credit_score, "detail": f"HYG/LQD={hyg/lqd:.3f}"},
        },
    }


# ── News cache (SQLite, 24h TTL) ──────────────────────────────────────
_NEWS_DB = BASE_DIR / "data" / "news_cache.db"
_NEWS_TTL = 86400  # 24 hours


def _news_conn() -> sqlite3.Connection:
    _NEWS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_NEWS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            cache_key  TEXT PRIMARY KEY,
            result_json TEXT,
            fetched_at  INTEGER
        )
    """)
    conn.commit()
    return conn


def _news_cache_get(key: str) -> list | None:
    conn = _news_conn()
    row = conn.execute(
        "SELECT result_json, fetched_at FROM news_cache WHERE cache_key=?", (key,)
    ).fetchone()
    conn.close()
    if row and (time.time() - row[1]) < _NEWS_TTL:
        return json.loads(row[0])
    return None


def _news_cache_put(key: str, result: list) -> None:
    conn = _news_conn()
    conn.execute(
        "INSERT OR REPLACE INTO news_cache VALUES (?,?,?)",
        (key, json.dumps(result), int(time.time())),
    )
    conn.commit()
    conn.close()


def _claude_news_analysis(positions: list[dict]) -> list[dict]:
    tickers = sorted({p["ticker"] for p in positions})
    cache_key = ",".join(tickers) + "_" + datetime.date.today().isoformat()

    cached = _news_cache_get(cache_key)
    if cached:
        print(f"  [layer3] news cache hit for {tickers}")
        return cached

    context = []
    for t in tickers:
        yf_t = yf.Ticker(t)
        hist = yf_t.history(period="5d")
        pct_5d = float((hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100) if len(hist) >= 2 else 0
        news = yf_t.news[:5] if yf_t.news else []
        titles = [n.get("content", {}).get("title", "") for n in news if n.get("content")]
        context.append({"ticker": t, "price_5d_pct": round(pct_5d, 2), "recent_news": titles})

    prompt = f"""
Today: {datetime.date.today()}
Portfolio tickers: {tickers}

Market data and recent headlines per ticker:
{json.dumps(context, indent=2)}

For each ticker return a JSON object with:
- ticker
- sentiment: "bullish" | "neutral" | "bearish"
- summary: 1-2 sentences on what is happening
- position_impact: how does this affect a long call or long share holder?
- action_flag: "none" | "monitor" | "consider_exit" | "consider_add"

Return a JSON array, one object per ticker. No prose outside the JSON.
"""
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                result = json.loads(part)
                _news_cache_put(cache_key, result)
                return result
            except json.JSONDecodeError:
                continue
    result = json.loads(raw)
    _news_cache_put(cache_key, result)
    return result


def run(valued_positions: list[dict]) -> dict:
    return {
        "macro": _macro_score(),
        "news": _claude_news_analysis(valued_positions),
    }
