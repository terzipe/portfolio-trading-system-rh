"""
VIX Trader BOT — paper ledger (SRS v1.4 §8, §8.1, §15.3, Impl Plan §5).

Records every signal at every cycle, even when the live book skipped the
trade (session DEAD, sleeve at cap, flag off, etc.) — this is the dataset
for the Phase A (~3 month) review. Append-only JSONL; never mutated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import VIX_DATA_DIR, VIX_PAPER_LEDGER_FILE, VIX_SLEEVE_MAX_PCT

_PAPER_NAV_ASSUMED = 1.0  # fraction of "virtual" NAV represented by the paper sleeve; scaled by caller


@dataclass
class PaperRow:
    timestamp: str
    posture: str
    action: str
    ticker: Optional[str]
    price: Optional[float]
    session_state: str
    live_executed: bool
    skip_reason: str = ""
    family_tags: dict = field(default_factory=dict)  # SRS §15.3 — reference only, no live orders

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp, "posture": self.posture, "action": self.action,
            "ticker": self.ticker, "price": self.price, "session_state": self.session_state,
            "live_executed": self.live_executed, "skip_reason": self.skip_reason,
            "family_tags": self.family_tags,
        }


def record(
    posture: str,
    action: str,
    ticker: str | None,
    price: float | None,
    session_state: str,
    live_executed: bool,
    skip_reason: str = "",
    family_tags: dict | None = None,
) -> None:
    """Append one row. Called every intraday cycle for every signal computed,
    matching live at UW mid +/- half-spread (approximated by `price` as
    passed by the caller, which already has the mid)."""
    row = PaperRow(
        timestamp=datetime.now().isoformat(),
        posture=posture, action=action, ticker=ticker, price=price,
        session_state=session_state, live_executed=live_executed,
        skip_reason=skip_reason, family_tags=family_tags or {},
    )
    with open(VIX_PAPER_LEDGER_FILE, "a") as f:
        f.write(json.dumps(row.as_dict()) + "\n")


def read_ledger() -> list[dict]:
    if not VIX_PAPER_LEDGER_FILE.exists():
        return []
    rows = []
    with open(VIX_PAPER_LEDGER_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_daily_summary(nav: float) -> dict:
    """
    Daily batch writes data/vix/paper_daily_YYYY-MM-DD.json — win rate,
    max DD, SVIX vs fade vs tactical attribution (Impl Plan §5).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    rows = [r for r in read_ledger() if r["timestamp"].startswith(today)]

    by_posture: dict[str, int] = {}
    for r in rows:
        by_posture[r["posture"]] = by_posture.get(r["posture"], 0) + 1

    executed = [r for r in rows if r["live_executed"]]
    skipped = [r for r in rows if not r["live_executed"] and r["action"] not in ("HOLD", "NOOP")]

    summary = {
        "date": today,
        "nav": nav,
        "sleeve_max_pct": VIX_SLEEVE_MAX_PCT,
        "signal_count": len(rows),
        "by_posture": by_posture,
        "live_executed_count": len(executed),
        "live_skipped_count": len(skipped),
        "skipped_reasons": [r["skip_reason"] for r in skipped if r["skip_reason"]],
    }

    out_file = VIX_DATA_DIR / f"paper_daily_{today}.json"
    out_file.write_text(json.dumps(summary, indent=2))
    return summary
