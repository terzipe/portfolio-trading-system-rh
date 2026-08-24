"""
FRED (Federal Reserve Economic Data) — free, no-API-key CSV endpoint for the
VIX index's own daily close (VIXCLS). Used by monitor/vix_percentile.py to
build the trailing-N-year percentile-rung threshold table (see
VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md §2).

Deliberately NOT sourced from Unusual Whales: UW's `iv_rank` field is the IV
rank of VIX's own *options* (how rich/cheap VIX options are vs. their 1yr
range), a different instrument from the VIX index level this strategy needs
to percentile-rank. UW also only carries ~2 years of history, too short a
lookback for a stable 90th+ percentile tail threshold. FRED's VIXCLS goes
back to 1990 and is exactly the index-close series this calc needs.
"""
from __future__ import annotations

from datetime import date

import requests

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class FredError(Exception):
    pass


def _lookback_start(end: date, lookback_years: int) -> date:
    try:
        return end.replace(year=end.year - lookback_years)
    except ValueError:  # end is Feb 29, target year has no leap day
        return end.replace(month=2, day=28, year=end.year - lookback_years)


def fetch_vix_closes(lookback_years: int, end: date | None = None) -> list[float]:
    """Trailing `lookback_years` of VIX daily closes (oldest first). Rows
    FRED marks missing ("." on holidays/pre-series-start) are dropped
    rather than raising."""
    end = end or date.today()
    start = _lookback_start(end, lookback_years)
    try:
        resp = requests.get(
            FRED_CSV_URL,
            params={"id": "VIXCLS", "cosd": start.isoformat(), "coed": end.isoformat()},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FredError(f"VIXCLS fetch failed: {exc}") from exc

    closes: list[float] = []
    for line in resp.text.strip().splitlines()[1:]:  # skip header row
        parts = line.split(",")
        if len(parts) != 2:
            continue
        try:
            closes.append(float(parts[1]))
        except ValueError:
            continue  # "." placeholder or malformed row
    if not closes:
        raise FredError("VIXCLS fetch returned no usable closes")
    return closes
