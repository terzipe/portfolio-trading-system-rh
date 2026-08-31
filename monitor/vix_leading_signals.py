"""
VIX Trader BOT — leading-indicator exit stack for the SVIX manual campaign
(monitor/svix_manual_campaign.py). Kept separate from monitor/vix_longvol_
gates.py deliberately — that module's gates are backtested/calibrated
specifically for confirming LONG_VOL_TACTICAL entries; this module scores
toward an EXIT instead, and folding the two together would conflate two
different calibrations against two different targets.

VIX print itself is coincident, not leading (his framework, 2026-08-28):
what actually leads a spike is (1) how the vol-of-vol market is pricing the
next VIX move, and (2) compression in VIX's own range. Four-tier stack,
priority order:

  Tier 1 — VVIX/VIX divergence (best single leading signal): VVIX rising
    while VIX is flat/down. Informed flow hits VIX options before the
    headline VIX moves.
  Tier 2 — VIX + VVIX range compression ("coiled spring"): unusually low
    20-day dispersion in EITHER series precedes spikes more reliably than a
    simply "low" VIX level does (Thrasher). "Combined compression of both
    is the highest-alert state" — required here on BOTH series, not either.
  Tier 3 — front-of-curve term structure: VIX/VIX3M ratio rising off a deep
    contango reading — reuses vix_longvol_gates.term_structure_gate()'s
    machinery but with a sharper/shorter lookback tuned for the earliest
    exit warning, not entry confirmation (Gate B's 15-session default is
    calibrated for the opposite use case).
  Tier 4 — confirmers (SKEW): never used alone, only adds conviction.

VVIX/SKEW come from yfinance — FRED does not carry either series (checked
live, 2026-08-28: fredgraph.csv 404s for VVIXCLS/VIX9D). VIX/VIX3M continue
to come from data/fred.py, matching the rest of this codebase.

exit_level mapping (svix_manual_campaign.py's exit-response logic):
  0 — nothing confirmed
  1 — tier 1 (divergence) confirmed — arm a WIDE resting stop
      (config.SVIX_MANUAL_TIER1_STOP_PCT). Wired live 2026-08-29 — tier 1
      is the earliest/least-confirmed signal in his priority ordering, so
      it gets the loosest response, not a flatten. The stop only ever
      ratchets TIGHTER (closer to price) as a stronger tier confirms or
      price falls further — a level-1-armed stop is never widened back out
      by a later level-1-only reading, see run_exit_cycle()'s docstring.
  2 — tier 2 (compression) confirmed — arm/tighten the resting stop to
      config.SVIX_MANUAL_STOP_PCT (tighter than tier 1's)
  3 — tier 3 (term structure) confirmed on VIX_LEADING_TIER3_CONFIRM_DAYS
      CONSECUTIVE trading days — full flatten immediately, regardless of
      any armed stop

Tier 3 requiring multiple consecutive days (not just today) was wired live
2026-08-29 after backtest_svix_manual.py --tier3-confirm-days found the
un-gated (1-day) version was flattening 133/134 of all sells and same-day
round-tripping 37% of entries (bought and immediately flattened before ever
holding overnight) — tier 3 alone was functioning as a near-single-tier
system despite the four-tier design. See config.py's
VIX_LEADING_TIER3_CONFIRM_DAYS comment for the full before/after numbers.
This is the one piece of state this otherwise-stateless module carries
(VIX_LEADING_STATE_FILE) — see _update_tier3_streak()'s docstring for how
it distinguishes "still today" re-polls (fast exit-loop cadence, seconds to
minutes) from a genuine day-over-day rollover, since a naive per-call
streak would satisfy "2 consecutive" within two polls of the same day
rather than two actual trading days.

All thresholds below are backtest-swept starting defaults (see
backtest_svix_manual.py), not fixed conclusions. All four tiers fail closed
(return False) on missing/unavailable data, consistent with
vix_longvol_gates.py's convention — no gate ever confirms on an educated
guess.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date

import yfinance as yf

from config import (
    VIX_LEADING_DIVERGENCE_SESSIONS,
    VIX_LEADING_DIVERGENCE_MIN_PP,
    VIX_LEADING_COMPRESSION_WINDOW,
    VIX_LEADING_COMPRESSION_PERCENTILE,
    VIX_LEADING_COMPRESSION_LOOKBACK_YEARS,
    VIX_LEADING_TERM_STRUCTURE_SESSIONS,
    VIX_LEADING_TERM_STRUCTURE_MIN_PCT,
    VIX_LEADING_SKEW_SESSIONS,
    VIX_LEADING_SKEW_MIN_PCT,
    VIX_LEADING_TIER3_CONFIRM_DAYS,
    VIX_LEADING_STATE_FILE,
)
from data.fred import fetch_dated_series, VIXCLS
from monitor.vix_longvol_gates import term_structure_gate


@dataclass
class LeadingSignalResult:
    tier1_divergence: bool
    tier2_compression: bool
    tier3_term_structure: bool  # TODAY's raw reading -- may be True even when exit_level < 3 (streak not yet met)
    tier4_skew_confirmer: bool
    tier3_confirmed_days: int  # consecutive trading days tier3 has read confirmed, including today
    score: int  # count of tier1-3 confirmed (tier3 counts on today's raw reading); tier4 never counted (confirmer only)
    exit_level: int  # 0 = none, 2 = arm stop, 3 = full flatten (tier3 sustained VIX_LEADING_TIER3_CONFIRM_DAYS)
    reasons: list[str] = field(default_factory=list)


def _load_leading_state() -> dict:
    if not VIX_LEADING_STATE_FILE.exists():
        return {"tier3_last_known_date": None, "tier3_last_known_confirmed": False, "tier3_finalized_streak": 0}
    try:
        return json.loads(VIX_LEADING_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"tier3_last_known_date": None, "tier3_last_known_confirmed": False, "tier3_finalized_streak": 0}


def _save_leading_state(state: dict) -> None:
    VIX_LEADING_STATE_FILE.write_text(json.dumps(state, indent=2))


def _update_tier3_streak(tier3_now: bool, today: date | None = None, dry_run: bool = False) -> int:
    """Consecutive-trading-day confirmation count for tier 3, INCLUDING
    today's current reading. Deliberately day-aware, not call-aware: the
    fast exit-poll loop calls evaluate() every ~15s-5min while a position
    is open, and tier3_now can genuinely change intraday (ratio_now uses
    live VIX/VIX3M, not just FRED's daily close) -- a naive "confirmed on
    the last N calls" counter would satisfy "2 consecutive days" within two
    poll cycles of the SAME day, which is not what was backtested or
    intended (see module docstring).

    On each NEW calendar date (first call of the day), the PREVIOUS day's
    last-known reading is finalized into tier3_finalized_streak (extending
    it if that day ended confirmed, resetting to 0 if not) before today's
    reading is recorded. The returned streak is finalized-prior-days plus
    (1 if today is confirmed right now, else 0) -- so a day that starts
    unconfirmed and later flips confirmed intraday is reflected immediately
    on the next call, without waiting for that day's close.

    `today` is injectable for testing; defaults to the real current date.
    `dry_run=True` computes and returns the same preview a real call would,
    but never persists it -- matching this codebase's "dry run leaves no
    trace" convention elsewhere (see vix_ladder.evaluate()'s dry_run) so a
    `--dry-run` drill can't corrupt the real streak a subsequent live cycle
    depends on."""
    today = today or date.today()
    today_iso = today.isoformat()
    state = _load_leading_state()

    if state["tier3_last_known_date"] is None or state["tier3_last_known_date"] == today_iso:
        finalized = state["tier3_finalized_streak"]
    else:
        finalized = state["tier3_finalized_streak"] + 1 if state["tier3_last_known_confirmed"] else 0

    if not dry_run:
        state["tier3_finalized_streak"] = finalized
        state["tier3_last_known_date"] = today_iso
        state["tier3_last_known_confirmed"] = tier3_now
        _save_leading_state(state)

    return finalized + (1 if tier3_now else 0)


def _yf_closes(ticker: str, lookback_years: float) -> list[float] | None:
    """Trailing ~lookback_years of daily closes for a yfinance ticker (VVIX/
    SKEW — not on FRED), oldest first, NaN-dropped. None on any fetch
    failure or empty result (fail closed)."""
    try:
        period = f"{max(1, round(lookback_years))}y"
        hist = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=False)
    except Exception:
        return None
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes = hist["Close"]
    if hasattr(closes, "columns"):  # flatten yfinance's MultiIndex columns if present
        closes = closes.iloc[:, 0]
    out = [float(v) for v in closes if v == v]  # v == v drops NaN
    return out or None


def _pct_change_n_sessions(closes: list[float], n: int) -> float | None:
    if len(closes) <= n:
        return None
    then, now = closes[-1 - n], closes[-1]
    if not then:
        return None
    return (now / then) - 1


def _rolling_sd_percentile(closes: list[float], window: int) -> float | None:
    """Today's trailing `window`-day SD's percentile rank against the full
    history of rolling `window`-day SDs available in `closes` — the same
    compression math validated live 2026-08-28 (VIX's 20d SD sat at the
    0.8th percentile of 2y of rolling 20d SDs). None if there's not enough
    history for even one rolling window."""
    if len(closes) < window + 1:
        return None
    today_sd = statistics.pstdev(closes[-window:])
    roll_sds = [statistics.pstdev(closes[i - window:i]) for i in range(window, len(closes))]
    if not roll_sds:
        return None
    return sum(1 for s in roll_sds if s <= today_sd) / len(roll_sds) * 100


def divergence_gate(
    vix_closes: list[float], vvix_closes: list[float],
    sessions: int | None = None, min_pp: float | None = None,
) -> bool:
    """Tier 1: VVIX outpacing VIX by at least `min_pp` (as a fraction, e.g.
    0.10 = 10 percentage points) over `sessions`, while VIX hasn't already
    moved up itself over that same window (proxied as VIX's own change
    being <= 0 — "flat or down", matching his framework's setup description
    literally: divergence, not confirmation of a move already underway)."""
    sessions = sessions if sessions is not None else VIX_LEADING_DIVERGENCE_SESSIONS
    min_pp = min_pp if min_pp is not None else VIX_LEADING_DIVERGENCE_MIN_PP
    vix_chg = _pct_change_n_sessions(vix_closes, sessions)
    vvix_chg = _pct_change_n_sessions(vvix_closes, sessions)
    if vix_chg is None or vvix_chg is None:
        return False
    if vix_chg > 0:
        return False  # VIX already moving -- coincident, not a leading divergence
    return (vvix_chg - vix_chg) >= min_pp


def compression_gate(
    vix_closes: list[float], vvix_closes: list[float],
    window: int | None = None, percentile: float | None = None,
) -> bool:
    """Tier 2: BOTH VIX's and VVIX's own trailing-`window`-day SD are below
    `percentile` of their own rolling-SD history — "combined compression of
    both is the highest-alert state" (his framework), so both, not either."""
    window = window if window is not None else VIX_LEADING_COMPRESSION_WINDOW
    percentile = percentile if percentile is not None else VIX_LEADING_COMPRESSION_PERCENTILE
    vix_pct = _rolling_sd_percentile(vix_closes, window)
    vvix_pct = _rolling_sd_percentile(vvix_closes, window)
    if vix_pct is None or vvix_pct is None:
        return False
    return vix_pct <= percentile and vvix_pct <= percentile


def skew_confirmer_gate(
    skew_closes: list[float], sessions: int | None = None, min_pct: float | None = None,
) -> bool:
    """Tier 4: CBOE SKEW (crash-put demand) up at least `min_pct` over
    `sessions` — confirmer only, see evaluate()'s scoring (never confirms
    the exit alone)."""
    sessions = sessions if sessions is not None else VIX_LEADING_SKEW_SESSIONS
    min_pct = min_pct if min_pct is not None else VIX_LEADING_SKEW_MIN_PCT
    chg = _pct_change_n_sessions(skew_closes, sessions)
    if chg is None:
        return False
    return chg >= min_pct


def evaluate(
    vix_now: float | None, vix3m_now: float | None,
    today: date | None = None, dry_run: bool = False,
) -> LeadingSignalResult:
    """Runs all four tiers and maps the result to an exit_level (see module
    docstring for the full 0-3 mapping). Tier 1 (divergence) got a wired
    response 2026-08-29 — a wide/loose arm-stop, one rung below tier 2's
    tighter stop, matching its position as the earliest/least-confirmed
    signal in his priority ordering.

    `today` is injectable for testing (threaded into _update_tier3_streak());
    live callers should never pass it. `dry_run=True` previews the same
    exit_level a real call would compute but never persists the tier-3
    streak update -- pass this from any --dry-run drill (see
    loop_svix_exit_monitor.py) so a preview can't corrupt the real streak.
    """
    reasons: list[str] = []

    try:
        vix_closes = [v for _, v in fetch_dated_series(VIXCLS, int(VIX_LEADING_COMPRESSION_LOOKBACK_YEARS) + 1)]
    except Exception as exc:
        reasons.append(f"VIX fetch failed: {exc}")
        vix_closes = []

    vvix_closes = _yf_closes("^VVIX", VIX_LEADING_COMPRESSION_LOOKBACK_YEARS + 1) or []
    skew_closes = _yf_closes("^SKEW", 1.0) or []

    tier1 = divergence_gate(vix_closes, vvix_closes) if vix_closes and vvix_closes else False
    tier2 = compression_gate(vix_closes, vvix_closes) if vix_closes and vvix_closes else False
    tier3 = term_structure_gate(
        vix_now, vix3m_now,
        lookback_sessions=VIX_LEADING_TERM_STRUCTURE_SESSIONS,
        min_pct=VIX_LEADING_TERM_STRUCTURE_MIN_PCT,
    )
    tier3_confirmed_days = _update_tier3_streak(tier3, today=today, dry_run=dry_run)
    tier4 = skew_confirmer_gate(skew_closes) if skew_closes else False

    reasons.append(f"tier 1 (VVIX/VIX divergence, {VIX_LEADING_DIVERGENCE_SESSIONS}d): {'confirmed' if tier1 else 'no'}")
    reasons.append(f"tier 2 (VIX+VVIX compression, {VIX_LEADING_COMPRESSION_WINDOW}d SD <= {VIX_LEADING_COMPRESSION_PERCENTILE:g}th pct): {'confirmed' if tier2 else 'no'}")
    reasons.append(
        f"tier 3 (term structure, {VIX_LEADING_TERM_STRUCTURE_SESSIONS}d): {'confirmed today' if tier3 else 'no'} "
        f"— {tier3_confirmed_days}/{VIX_LEADING_TIER3_CONFIRM_DAYS} consecutive day(s)"
    )
    reasons.append(f"tier 4 (SKEW confirmer, {VIX_LEADING_SKEW_SESSIONS}d): {'confirmed' if tier4 else 'no'}")

    score = sum((tier1, tier2, tier3))  # tier4 never counted -- confirmer only
    tier3_sustained = tier3_confirmed_days >= VIX_LEADING_TIER3_CONFIRM_DAYS
    exit_level = 3 if tier3_sustained else (2 if tier2 else (1 if tier1 else 0))

    return LeadingSignalResult(
        tier1_divergence=tier1, tier2_compression=tier2,
        tier3_term_structure=tier3, tier4_skew_confirmer=tier4,
        tier3_confirmed_days=tier3_confirmed_days,
        score=score, exit_level=exit_level, reasons=reasons,
    )
