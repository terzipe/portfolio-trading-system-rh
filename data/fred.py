"""
FRED (Federal Reserve Economic Data) — free, no-API-key CSV endpoint for VIX-
family daily series: VIXCLS (VIX index close) and VXVCLS (VIX3M index
close). Used by monitor/vix_percentile.py to build the trailing-N-year
percentile-rung threshold table (see VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md
§2), and by monitor/vix_longvol_gates.py's term-structure gate, which needs
VIX3M history to compare today's VIX/VIX3M ratio against N sessions ago.

Deliberately NOT sourced from Unusual Whales: UW's `iv_rank` field is the IV
rank of VIX's own *options* (how rich/cheap VIX options are vs. their 1yr
range), a different instrument from the VIX index level this strategy needs
to percentile-rank. UW also only carries ~2 years of history, too short a
lookback for a stable 90th+ percentile tail threshold. FRED's VIXCLS/VXVCLS
go back years further and are exactly the index-close series these calcs
need. UW_HAS_CME_FUTURES is false on this account tier anyway (no VX1/VX2),
so VIX/VIX3M is also the only term-structure signal actually available live.
"""
from __future__ import annotations

from datetime import date

import requests

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

VIXCLS = "VIXCLS"   # VIX index close
VXVCLS = "VXVCLS"   # VIX3M index close


class FredError(Exception):
    pass


def _lookback_start(end: date, lookback_years: int) -> date:
    try:
        return end.replace(year=end.year - lookback_years)
    except ValueError:  # end is Feb 29, target year has no leap day
        return end.replace(month=2, day=28, year=end.year - lookback_years)


def fetch_dated_series(series_id: str, lookback_years: int, end: date | None = None) -> list[tuple[date, float]]:
    """Trailing `lookback_years` of a FRED daily series (oldest first), as
    (date, value) pairs. Rows FRED marks missing ("." on holidays/
    pre-series-start) are dropped rather than raising."""
    end = end or date.today()
    start = _lookback_start(end, lookback_years)
    try:
        resp = requests.get(
            FRED_CSV_URL,
            params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FredError(f"{series_id} fetch failed: {exc}") from exc

    rows: list[tuple[date, float]] = []
    for line in resp.text.strip().splitlines()[1:]:  # skip header row
        parts = line.split(",")
        if len(parts) != 2:
            continue
        try:
            rows.append((date.fromisoformat(parts[0]), float(parts[1])))
        except ValueError:
            continue  # "." placeholder or malformed row
    if not rows:
        raise FredError(f"{series_id} fetch returned no usable observations")
    return rows


def fetch_vix_closes(lookback_years: int, end: date | None = None) -> list[float]:
    """Trailing `lookback_years` of VIX daily closes (oldest first), values
    only. Thin wrapper over fetch_dated_series() for callers that don't
    need dates (e.g. monitor/vix_percentile.py's percentile calc)."""
    return [v for _, v in fetch_dated_series(VIXCLS, lookback_years, end)]
