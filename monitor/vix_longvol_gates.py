"""
VIX Trader BOT — data-driven confirming gates for the LONG_VOL_TACTICAL
posture (replaces the old pure Aug-Oct calendar trigger in
monitor/vix_regime.py — confirmed design, 2026-08-25). Calendar bias is
dropped from the decision entirely.

Scoring is NOT an equal-weighted "any 2-of-3" — Gate A is a REQUIRED
prerequisite, with Gate B/C as confirming votes on top of it (Gate A AND
score >= VIX_LONGVOL_MIN_GATES, see evaluate()). This replaced an initial
equal-weighted design after backtesting showed almost all the edge came
from signals where Gate A had confirmed, and signals where it hadn't were
net-negative — see evaluate()'s docstring for the numbers.

Gate A — cheap floor (REQUIRED): VIX below the VIX_LONGVOL_CHEAP_PERCENTILE
  (default 20th) of the trailing VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS
  (default 7y) — its OWN lookback window, not the SVIX ladder's 10y (see
  monitor/vix_percentile.py's get_gate_a_threshold() vs. get_thresholds()).
  Decoupled 2026-08-26: a 10y window spans multiple full vol cycles, so
  clearing its 15th percentile is a high bar that VIX hadn't touched even
  once in the trailing 2 years — a shorter window recalibrates "cheap"
  against the current regime instead of a decade-old one, trading some
  win rate (67%->61% in backtest) for meaningfully more opportunities
  (18->28 signals) and actually being able to fire in the current regime.

Gate B — term structure flattening: today's live VIX/VIX3M ratio compared
  to the ratio VIX_LONGVOL_LOOKBACK_SESSIONS sessions ago, confirming if it
  has DECREASED (curve compressing toward backwardation). Uses VIX/VIX3M,
  not VX1/VX2 futures, because UW_HAS_CME_FUTURES=false on this account
  tier — no futures data is actually available live (same fallback
  monitor/vix_regime.py already uses). The "N sessions ago" side comes from
  FRED's VIXCLS/VXVCLS series (data/fred.py) rather than a self-persisted
  rolling window, since FRED already carries full history for both. Ratio
  chosen over spread per 2026-08-25 discussion — scale-invariant.

Gate C — momentum: the traded ticker's own close today vs. VIX_LONGVOL_
  LOOKBACK_SESSIONS sessions ago, confirming if up at least VIX_LONGVOL_
  MOMENTUM_MIN_PCT (a magnitude floor so it doesn't fire on noise). VXX by
  default (the only ticker LONG_VOL_TACTICAL actually trades,
  vix_options.pick_call(ticker="VXX")) — momentum should reflect what the
  trade's own P&L is actually exposed to. evaluate()/momentum_gate() both
  accept ticker="UVXY" for the separate UVXY alert (2026-08-27, loop_daily_
  vix.py/loop_intraday_vix.py) — same Gate A/B (index-level, ticker-
  agnostic), UVXY-specific Gate C.

All three gates fail closed (return False) on missing/unavailable data,
consistent with evaluate_fade_spike()'s convention elsewhere in this
codebase — no gate ever confirms on an educated guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from config import (
    VIX_LONGVOL_CHEAP_PERCENTILE,
    VIX_LONGVOL_LOOKBACK_SESSIONS,
    VIX_LONGVOL_MOMENTUM_MIN_PCT,
    VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT,
    VIX_LONGVOL_MIN_GATES,
)
from data.fred import fetch_dated_series, FredError, VIXCLS, VXVCLS
from data.unusual_whales import UWError
from monitor import vix_percentile
# fetch_ticker_history is imported lazily inside momentum_gate() -- vix_signals
# imports posture constants from vix_regime, which imports this module, so a
# module-level import here would be circular.


@dataclass
class LongVolGateResult:
    gate_a_cheap: bool
    gate_b_term_structure: bool
    gate_c_momentum: bool
    score: int
    confirmed: bool
    # Raw momentum reading (e.g. 0.12 = +12%), not just the gate_c boolean --
    # added 2026-09-01 for the VXX/UVXY rotation tie-break (see
    # vix_regime.py's compute_posture()): when both tickers' gates confirm
    # in the same cycle (common whenever Gate B alone supplies the score,
    # since gate_c only needs to clear the 10% floor either way), this is
    # what decides which one actually gets traded. None if momentum_gate()
    # failed closed (missing/insufficient history).
    momentum_pct: float | None = None
    reasons: list[str] = field(default_factory=list)


def cheap_vol_gate(vix_now: float | None) -> bool:
    """Gate A: VIX below the cheap-floor percentile of trailing
    VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS history (its own window, not
    the SVIX ladder's)."""
    if vix_now is None:
        return False
    threshold = vix_percentile.get_gate_a_threshold()
    if threshold is None:
        return False
    return vix_now < threshold


def _ratio_n_sessions_ago(lookback_sessions: int, as_of: date | None = None) -> float | None:
    """VIX/VIX3M ratio from FRED's dated series, `lookback_sessions`
    trading-session rows back from the most recent shared observation date
    between VIXCLS and VXVCLS. ~3 months of history is fetched for safety
    margin around holidays/gaps; None on any fetch failure or insufficient
    overlapping history (fail closed)."""
    try:
        vix_series = dict(fetch_dated_series(VIXCLS, lookback_years=1, end=as_of))
        vix3m_series = dict(fetch_dated_series(VXVCLS, lookback_years=1, end=as_of))
    except FredError:
        return None
    shared_dates = sorted(set(vix_series) & set(vix3m_series))
    if len(shared_dates) <= lookback_sessions:
        return None
    d = shared_dates[-1 - lookback_sessions]
    vix3m_then = vix3m_series[d]
    if not vix3m_then:
        return None
    return vix_series[d] / vix3m_then


def term_structure_gate(
    vix_now: float | None, vix3m_now: float | None,
    lookback_sessions: int | None = None, min_pct: float | None = None,
) -> bool:
    """Gate B: VIX/VIX3M ratio has fallen by at least `min_pct` of its
    value `lookback_sessions` ago (curve flattening/compressing toward
    backwardation — an early-warning precursor rather than requiring
    outright backwardation already, which would mean the move already
    happened). min_pct=0.0 (the original definition) confirms on any
    decrease at all -- backtesting 2026-08-25 found that too loose, firing
    on essentially any wiggle in the ratio."""
    if not vix_now or not vix3m_now:
        return False
    lookback_sessions = lookback_sessions if lookback_sessions is not None else VIX_LONGVOL_LOOKBACK_SESSIONS
    min_pct = min_pct if min_pct is not None else VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT
    ratio_then = _ratio_n_sessions_ago(lookback_sessions)
    if ratio_then is None:
        return False
    ratio_now = vix_now / vix3m_now
    return ratio_now <= ratio_then * (1 - min_pct)


def _momentum_pct(uw_client, ticker: str, lookback_sessions: int) -> float | None:
    """Raw momentum reading behind Gate C -- `ticker`'s close today vs.
    `lookback_sessions` sessions ago, as a fraction (0.12 = +12%). None on
    any fetch failure or insufficient history (fail closed, same convention
    as every other gate here)."""
    from monitor.vix_signals import fetch_ticker_history

    try:
        closes = fetch_ticker_history(uw_client, ticker, sessions=lookback_sessions + 1)
    except UWError:
        return None
    if not closes or len(closes) < 2 or not closes[0]:
        return None
    return closes[-1] / closes[0] - 1


def momentum_gate(
    uw_client, lookback_sessions: int | None = None, min_pct: float | None = None, ticker: str = "VXX",
) -> bool:
    """Gate C: `ticker`'s close today vs. `lookback_sessions` sessions ago,
    up at least `min_pct` (magnitude floor against noise). Defaults to VXX
    (the ticker LONG_VOL_TACTICAL actually trades); pass ticker="UVXY" for
    a UVXY-specific read (e.g. the standalone alert in
    loop_daily_vix.py/loop_intraday_vix.py — momentum should reflect what
    that specific trade's P&L is actually exposed to, not always VXX's)."""
    lookback_sessions = lookback_sessions if lookback_sessions is not None else VIX_LONGVOL_LOOKBACK_SESSIONS
    min_pct = min_pct if min_pct is not None else VIX_LONGVOL_MOMENTUM_MIN_PCT
    pct = _momentum_pct(uw_client, ticker, lookback_sessions)
    return pct is not None and pct >= min_pct


def evaluate(uw_client, vix_now: float | None, vix3m_now: float | None, ticker: str = "VXX") -> LongVolGateResult:
    """Runs all three gates and scores them.

    confirmed = Gate A (cheap vol) REQUIRED, plus score >=
    VIX_LONGVOL_MIN_GATES (default 2) counting all three gates — i.e., at
    the default min_gates=2, Gate A confirmed AND at least one of B/C.
    Replaced the original equal-weighted "any 2-of-3" scoring 2026-08-25
    after backtesting (backtest_longvol_gates.py --require-gate-a) showed
    nearly the entire edge came from signals where A had confirmed; when A
    was off (chasing a move after vol was no longer cheap), results were
    negative (n=5, avg -12.8%, 20% win rate at 21d) — pulling the win
    rate/avg down for the whole equal-weighted sample. Requiring A
    mechanically drops just those losing signals: same qualifying
    threshold values, win rate 57%->67%, avg 21d return +5.6%->+10.7%,
    worst single trade -43.4%->-8.2%.

    `ticker` only affects Gate C (momentum source) — Gate A/B are
    index-level (VIX/VIX3M), unaffected by which instrument you'd actually
    trade. Default VXX drives the real LONG_VOL_TACTICAL posture;
    ticker="UVXY" powers the separate UVXY long-vol alert
    (2026-08-27) — backtested comparably (61%->64% win rate, +7.7%->+10.7%
    avg at 21d vs. VXX) but with materially worse decay past 21d (avg
    -0.3%->-2.4% at 45d) — UVXY amplifies both the win and the overstay
    penalty, consistent with it being the more leveraged/faster-decaying
    of the two products. The alert should not be read as a signal to hold
    UVXY on a 45-day view.

    No calendar input anywhere in this function — fully data-driven per
    the 2026-08-25 design decision that also dropped the old Aug-Oct
    calendar trigger entirely.
    """
    gate_a = cheap_vol_gate(vix_now)
    gate_b = term_structure_gate(vix_now, vix3m_now)
    momentum_pct = _momentum_pct(uw_client, ticker, VIX_LONGVOL_LOOKBACK_SESSIONS)
    gate_c = momentum_pct is not None and momentum_pct >= VIX_LONGVOL_MOMENTUM_MIN_PCT
    score = sum((gate_a, gate_b, gate_c))
    reasons = [
        f"gate A (cheap vol, VIX<{VIX_LONGVOL_CHEAP_PERCENTILE:g}th pct, REQUIRED): {'confirmed' if gate_a else 'no'}",
        f"gate B (term structure flattening, {VIX_LONGVOL_LOOKBACK_SESSIONS}d): {'confirmed' if gate_b else 'no'}",
        f"gate C ({ticker} momentum >= {VIX_LONGVOL_MOMENTUM_MIN_PCT:.0%}, {VIX_LONGVOL_LOOKBACK_SESSIONS}d): "
        f"{'confirmed' if gate_c else 'no'} ({momentum_pct:+.1%})" if momentum_pct is not None else
        f"gate C ({ticker} momentum >= {VIX_LONGVOL_MOMENTUM_MIN_PCT:.0%}, {VIX_LONGVOL_LOOKBACK_SESSIONS}d): no (unavailable)",
    ]
    return LongVolGateResult(
        gate_a_cheap=gate_a, gate_b_term_structure=gate_b, gate_c_momentum=gate_c,
        score=score, confirmed=gate_a and score >= VIX_LONGVOL_MIN_GATES,
        momentum_pct=momentum_pct, reasons=reasons,
    )
