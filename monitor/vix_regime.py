"""
VIX Trader BOT — regime + posture engine (SRS v1.4 §7, Impl Plan §3).

Regime numbers (VIX, VIX3M, contango/backwardation) come from Unusual
Whales, never yfinance. The 6-signal macro gate is a required *input*
(its posture: FULL/REDUCED/DEFENSIVE/CASH) but this module does not
recompute VIX from the gate — "do not invent a second VIX calculator"
(SRS §3). Calendar month is a bias that can scale sizing, never a veto:
Feb-Mar is explicitly not a hard SVIX lock (SRS §2 decision #10, §7.2).
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import VIX_DATA_DIR, VIX_STALE_SECONDS, VIX_KILL_SWITCH, VIX_LONGVOL_MOMENTUM_TIE_PCT
from data.unusual_whales import get_client, UWError
from monitor.vix_longvol_gates import LongVolGateResult, evaluate as evaluate_longvol_gates

REGIME_TRADER_PATH = Path(__file__).parent.parent.parent / "regime_trader"
if str(REGIME_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(REGIME_TRADER_PATH))

LONG_VOL_TACTICAL = "LONG_VOL_TACTICAL"
FADE_SPIKE_PUTS = "FADE_SPIKE_PUTS"
CASH = "CASH"

# SRS §7.2 — bias only, never a lock.
_CALENDAR_BIAS = {
    1: ("svix", "+"), 11: ("svix", "+"), 12: ("svix", "+"),
    4: ("svix", "+"), 5: ("svix", "+"), 6: ("svix", "+"), 7: ("svix", "+"),
    8: ("long_vol", "-"), 9: ("long_vol", "-"), 10: ("long_vol", "-"),
    2: ("neutral", "0"), 3: ("neutral", "0"),
}


@dataclass
class RegimeResult:
    vix: float | None
    vix3m: float | None
    vx1: float | None
    vx2: float | None
    regime: str  # "CONTANGO" | "BACKWARDATION" | "UNKNOWN"
    days_in_regime: int
    gate_posture: str
    gate_score: float | None
    calendar_month: int
    calendar_bias: str
    posture: str
    # Which ticker LONG_VOL_TACTICAL actually trades this cycle -- "VXX",
    # "UVXY", or None when posture isn't LONG_VOL_TACTICAL. Added 2026-09-01
    # (VXX/UVXY rotation) -- see compute_posture()'s docstring.
    longvol_ticker: str | None = None
    # Full per-ticker gate detail (dashboard visibility) -- replaces the old
    # standalone "UVXY long-vol alert" that separately re-evaluated the same
    # gates in loop_daily_vix.py/loop_intraday_vix.py; now redundant since
    # run() already evaluates both tickers internally. dict, not
    # LongVolGateResult, to keep as_dict() JSON-serializable directly.
    vxx_gate_detail: dict | None = None
    uvxy_gate_detail: dict | None = None
    reasons: list[str] = field(default_factory=list)
    data_age_sec: float = 0.0
    fetched_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "vix": self.vix, "vix3m": self.vix3m, "vx1": self.vx1, "vx2": self.vx2,
            "regime": self.regime, "days_in_regime": self.days_in_regime,
            "gate": self.gate_posture, "gate_score": self.gate_score,
            "calendar_month": self.calendar_month, "calendar_bias": self.calendar_bias,
            "posture": self.posture, "longvol_ticker": self.longvol_ticker, "reasons": self.reasons,
            "vxx_gate_detail": self.vxx_gate_detail, "uvxy_gate_detail": self.uvxy_gate_detail,
            "data_age_sec": self.data_age_sec, "fetched_at": self.fetched_at,
        }


def _read_gate() -> tuple[str, float | None]:
    """Read regime_trader's cached gate posture. Falls back to REDUCED/50
    if the gate is unavailable, matching regime_trader's own fallback
    (run_gate() already does this internally) — never invented locally."""
    try:
        from macro_gate import run_gate
        gate = run_gate()
        return gate.posture, gate.score
    except Exception as exc:  # noqa: BLE001
        print(f"[vix_regime] gate import/read failed, treating as REDUCED: {exc}")
        return "REDUCED", 50.0


def _days_in_regime(regime: str) -> int:
    """Persisted via the most recent regime_*.json snapshot on disk."""
    files = sorted(VIX_DATA_DIR.glob("regime_*.json"))
    days = 1
    for f in reversed(files):
        try:
            prev = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if prev.get("regime") != regime:
            break
        days += 1
    return days


def _calendar_month() -> int:
    return datetime.now(timezone.utc).astimezone().month


def _pick_longvol_ticker(
    vxx_gates: "LongVolGateResult | None", uvxy_gates: "LongVolGateResult | None",
) -> str | None:
    """VXX/UVXY rotation rule (confirmed 2026-09-01, refined 2026-09-02
    after backtest_longvol_gates.py --rotation found the first cut never
    actually rotated). Trade whichever ticker's OWN gates confirm. Gate A/B
    are index-level -- identical for both -- so whenever Gate B alone
    supplies the score, BOTH tickers confirm together WITHOUT Gate C itself
    confirming for either. Backtesting the original "always compare raw
    momentum" version found every historical signal fired via A+B alone,
    with momentum negative on both sides -- and since UVXY's leverage makes
    its declines larger in magnitude than VXX's, that comparison mechanically
    picked VXX 100% of the time for a reason that had nothing to do with
    conviction, not because VXX was ever genuinely stronger.

    Fixed: momentum magnitude is only compared when BOTH tickers' Gate C
    independently confirmed (i.e. both cleared VIX_LONGVOL_MOMENTUM_MIN_PCT
    on their own, a genuine "which one shows more real momentum" case).
    When confirmation instead came from A+B alone for one or both (Gate C
    not actually differentiating), skip straight to the default -- VXX
    (his call, 2026-09-02, changed from the original UVXY default). Ties
    within VIX_LONGVOL_MOMENTUM_TIE_PCT of each other also default to VXX.
    Returns None if neither confirms."""
    vxx_confirmed = vxx_gates is not None and vxx_gates.confirmed
    uvxy_confirmed = uvxy_gates is not None and uvxy_gates.confirmed
    if vxx_confirmed and not uvxy_confirmed:
        return "VXX"
    if uvxy_confirmed and not vxx_confirmed:
        return "UVXY"
    if vxx_confirmed and uvxy_confirmed:
        both_gate_c_confirmed = vxx_gates.gate_c_momentum and uvxy_gates.gate_c_momentum
        if not both_gate_c_confirmed:
            return "VXX"  # confirmed via A+B alone for at least one -- not a genuine momentum comparison
        vxx_pct = vxx_gates.momentum_pct if vxx_gates.momentum_pct is not None else 0.0
        uvxy_pct = uvxy_gates.momentum_pct if uvxy_gates.momentum_pct is not None else 0.0
        if abs(vxx_pct - uvxy_pct) < VIX_LONGVOL_MOMENTUM_TIE_PCT:
            return "VXX"
        return "VXX" if vxx_pct > uvxy_pct else "UVXY"
    return None


def compute_posture(
    vix: float | None,
    vix3m: float | None,
    vx1: float | None,
    vx2: float | None,
    gate_posture: str,
    gate_score: float | None,
    calendar_month: int,
    data_age_sec: float,
    fade_spike_ok: bool = False,
    vxx_gates: "LongVolGateResult | None" = None,
    uvxy_gates: "LongVolGateResult | None" = None,
) -> tuple[str, str, list[str], str | None]:
    """
    Posture priority, highest first (Impl Plan §3, SVIX branches retired —
    see VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md; SVIX is now driven
    entirely by monitor/vix_ladder.py, independent of posture):
    1. kill switch -> CASH (suppresses new option entries)
    2. stale data -> CASH
    3. fade-spike rules -> FADE_SPIKE_PUTS
    4. data-driven long-vol gates (2-of-3 scored, VXX or UVXY) -> LONG_VOL_TACTICAL
    5. else CASH

    vix/vix3m/vx1/vx2/gate_posture/gate_score are still accepted as params
    (callers already have them from the UW term-structure fetch and macro
    gate read, and RegimeResult still reports them for the dashboard) but
    are no longer used to compute posture directly — FADE_SPIKE_PUTS has
    its own independent spike detection (see
    vix_signals.evaluate_fade_spike(), driven by fade_spike_ok),
    backwardation/contango no longer has a posture-level meaning now that
    SVIX_ON/FLATTEN_SVIX are gone, and the macro gate no longer gates
    anything at the posture level either (it had only ever gated SVIX_ON).

    LONG_VOL_TACTICAL used to fire on a pure Aug-Oct calendar bias alone —
    replaced 2026-08-25 with monitor/vix_longvol_gates.py's scored,
    data-driven gates (cheap-vol floor, term-structure flattening,
    momentum). Calendar is dropped from this decision entirely; the
    calendar_month/bias_family machinery below is kept only for the
    RegimeResult.calendar_bias dashboard field, not as a trigger.

    VXX/UVXY rotation added 2026-09-01: previously VXX was the only ticker
    this posture ever traded (`longvol_gates` was a single result). Now
    both tickers' gates are evaluated independently and
    _pick_longvol_ticker() decides which one (if either) actually gets
    traded — see its docstring for the rotation/tie-break rule. Returns a
    4-tuple now (posture, bias_family, reasons, longvol_ticker); the
    ticker is None whenever posture isn't LONG_VOL_TACTICAL.
    `vxx_gates`/`uvxy_gates` default to None (not confirmed) so existing
    callers that don't yet pass them simply never trigger LONG_VOL_TACTICAL
    via this path, rather than raising.
    """
    reasons: list[str] = []
    bias_family, bias_sign = _CALENDAR_BIAS.get(calendar_month, ("neutral", "0"))

    if VIX_KILL_SWITCH:
        reasons.append("VIX_KILL_SWITCH=true -> no new option entries")
        return CASH, bias_family, reasons, None

    if data_age_sec > VIX_STALE_SECONDS:
        reasons.append(f"UW data stale ({data_age_sec:.0f}s > {VIX_STALE_SECONDS}s) -> no new buys")
        return CASH, bias_family, reasons, None

    if fade_spike_ok:
        reasons.append("fade-spike criteria met (see vix_signals) -> FADE_SPIKE_PUTS")
        return FADE_SPIKE_PUTS, bias_family, reasons, None

    longvol_ticker = _pick_longvol_ticker(vxx_gates, uvxy_gates)
    if longvol_ticker is not None:
        chosen_gates = vxx_gates if longvol_ticker == "VXX" else uvxy_gates
        reasons.append(
            f"long-vol gates {chosen_gates.score}/3 confirmed for {longvol_ticker} "
            f"(see monitor/vix_longvol_gates.py) -> LONG_VOL_TACTICAL"
        )
        reasons.extend(chosen_gates.reasons)
        both_confirmed = (vxx_gates is not None and vxx_gates.confirmed
                           and uvxy_gates is not None and uvxy_gates.confirmed)
        if both_confirmed:
            both_gate_c = vxx_gates.gate_c_momentum and uvxy_gates.gate_c_momentum
            if both_gate_c:
                reasons.append(
                    f"both VXX and UVXY confirmed with their own Gate C also confirmed -- picked {longvol_ticker} "
                    f"on momentum (VXX={vxx_gates.momentum_pct}, UVXY={uvxy_gates.momentum_pct})"
                )
            else:
                reasons.append(
                    f"both VXX and UVXY confirmed via Gate A+B alone (Gate C not independently confirming for "
                    f"both) -- defaulted to {longvol_ticker}, not a genuine momentum comparison"
                )
        return LONG_VOL_TACTICAL, bias_family, reasons, longvol_ticker

    # Neither confirmed -- Gate A/B are identical for both tickers, so show
    # VXX's full reasons (includes A/B) plus UVXY's own Gate C line only,
    # rather than duplicating the shared A/B lines twice.
    if vxx_gates is not None:
        reasons.extend(vxx_gates.reasons)
        if uvxy_gates is not None and uvxy_gates.reasons:
            reasons.append(uvxy_gates.reasons[-1])  # UVXY's Gate C line (A/B lines are identical, skipped)
    elif uvxy_gates is not None:
        reasons.extend(uvxy_gates.reasons)
    reasons.append("no posture condition met -> CASH")
    return CASH, bias_family, reasons, None


def run(fade_spike_ok: bool = False) -> RegimeResult:
    gate_posture, gate_score = _read_gate()

    uw = None
    try:
        uw = get_client()
        term = uw.vix_term()
        data_age_sec = time.time() - term.get("fetched_at", time.time())
    except UWError as exc:
        print(f"[vix_regime] UW vix_term() failed: {exc}")
        term = {"vix": None, "vix3m": None, "vx1": None, "vx2": None}
        data_age_sec = float("inf")

    # Evaluated independently (not one try/except) so a failure on one
    # ticker's gate read doesn't also discard the other's -- fail closed
    # per-ticker, matching this codebase's convention elsewhere.
    vxx_gates = None
    uvxy_gates = None
    if uw is not None:
        try:
            vxx_gates = evaluate_longvol_gates(uw, term.get("vix"), term.get("vix3m"), ticker="VXX")
        except Exception as exc:  # noqa: BLE001
            print(f"[vix_regime] VXX long-vol gate evaluation failed, treating as not confirmed: {exc}")
        try:
            uvxy_gates = evaluate_longvol_gates(uw, term.get("vix"), term.get("vix3m"), ticker="UVXY")
        except Exception as exc:  # noqa: BLE001
            print(f"[vix_regime] UVXY long-vol gate evaluation failed, treating as not confirmed: {exc}")

    calendar_month = _calendar_month()
    posture, bias_family, reasons, longvol_ticker = compute_posture(
        vix=term.get("vix"), vix3m=term.get("vix3m"),
        vx1=term.get("vx1"), vx2=term.get("vx2"),
        gate_posture=gate_posture, gate_score=gate_score,
        calendar_month=calendar_month, data_age_sec=data_age_sec,
        fade_spike_ok=fade_spike_ok, vxx_gates=vxx_gates, uvxy_gates=uvxy_gates,
    )

    if term.get("vx1") is not None and term.get("vx2") is not None:
        regime = "BACKWARDATION" if term["vx1"] > term["vx2"] else "CONTANGO"
    elif term.get("vix") is not None and term.get("vix3m"):
        regime = "BACKWARDATION" if term["vix"] > term["vix3m"] else "CONTANGO"
    else:
        regime = "UNKNOWN"

    def _gate_detail(g: "LongVolGateResult | None") -> dict | None:
        if g is None:
            return None
        return {
            "gate_a_cheap": g.gate_a_cheap, "gate_b_term_structure": g.gate_b_term_structure,
            "gate_c_momentum": g.gate_c_momentum, "momentum_pct": g.momentum_pct,
            "score": g.score, "confirmed": g.confirmed, "reasons": g.reasons,
        }

    result = RegimeResult(
        vix=term.get("vix"), vix3m=term.get("vix3m"), vx1=term.get("vx1"), vx2=term.get("vx2"),
        regime=regime, days_in_regime=_days_in_regime(regime),
        gate_posture=gate_posture, gate_score=gate_score,
        calendar_month=calendar_month, calendar_bias=bias_family,
        posture=posture, longvol_ticker=longvol_ticker,
        vxx_gate_detail=_gate_detail(vxx_gates), uvxy_gate_detail=_gate_detail(uvxy_gates),
        reasons=reasons, data_age_sec=data_age_sec,
    )

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_file = VIX_DATA_DIR / f"regime_{ts}.json"
    out_file.write_text(json.dumps(result.as_dict(), indent=2))

    return result


if __name__ == "__main__":
    r = run()
    print(f"posture={r.posture} regime={r.regime} vix={r.vix} vix3m={r.vix3m}")
    for reason in r.reasons:
        print(f"  - {reason}")
