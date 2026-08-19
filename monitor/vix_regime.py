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

from config import VIX_DATA_DIR, VIX_SPIKE_LEVEL, VIX_STALE_SECONDS, VIX_KILL_SWITCH
from data.unusual_whales import get_client, UWError

REGIME_TRADER_PATH = Path(__file__).parent.parent.parent / "regime_trader"
if str(REGIME_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(REGIME_TRADER_PATH))

SVIX_ON = "SVIX_ON"
LONG_VOL_TACTICAL = "LONG_VOL_TACTICAL"
FADE_SPIKE_PUTS = "FADE_SPIKE_PUTS"
CASH = "CASH"
FLATTEN_SVIX = "FLATTEN_SVIX"

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
    reasons: list[str] = field(default_factory=list)
    data_age_sec: float = 0.0
    fetched_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "vix": self.vix, "vix3m": self.vix3m, "vx1": self.vx1, "vx2": self.vx2,
            "regime": self.regime, "days_in_regime": self.days_in_regime,
            "gate": self.gate_posture, "gate_score": self.gate_score,
            "calendar_month": self.calendar_month, "calendar_bias": self.calendar_bias,
            "posture": self.posture, "reasons": self.reasons,
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
) -> tuple[str, str, list[str]]:
    """
    Posture priority, highest first (Impl Plan §3):
    1. kill switch -> flatten-only
    2. stale data -> no buy
    3. VIX>=25 or backwardation -> FLATTEN_SVIX
    4. fade-spike rules -> FADE_SPIKE_PUTS
    5. contango + VIX<25 + gate ok -> SVIX_ON
    6. Aug-Oct bias or explicit long-vol signal -> LONG_VOL_TACTICAL
    7. else CASH
    """
    reasons: list[str] = []
    bias_family, bias_sign = _CALENDAR_BIAS.get(calendar_month, ("neutral", "0"))

    if VIX_KILL_SWITCH:
        reasons.append("VIX_KILL_SWITCH=true -> flatten-only")
        return FLATTEN_SVIX, bias_family, reasons

    if data_age_sec > VIX_STALE_SECONDS:
        reasons.append(f"UW data stale ({data_age_sec:.0f}s > {VIX_STALE_SECONDS}s) -> no new buys")
        return CASH, bias_family, reasons

    if vx1 is not None and vx2 is not None:
        backwardation = vx1 > vx2
        contango = vx2 > vx1
        curve_detail = f"VX1={vx1}, VX2={vx2}"
    elif vix is not None and vix3m is not None:
        backwardation = vix > vix3m
        contango = vix < vix3m
        curve_detail = f"VIX={vix}, VIX3M={vix3m}, ratio={vix/vix3m:.3f}" if vix3m else "VIX3M unavailable"
    else:
        reasons.append("no term-structure data available -> no new buys")
        return CASH, bias_family, reasons

    if (vix is not None and vix >= VIX_SPIKE_LEVEL) or backwardation:
        reasons.append(f"VIX>={VIX_SPIKE_LEVEL} or backwardation ({curve_detail}) -> FLATTEN_SVIX")
        return FLATTEN_SVIX, bias_family, reasons

    if fade_spike_ok:
        reasons.append("fade-spike criteria met (see vix_signals) -> FADE_SPIKE_PUTS")
        return FADE_SPIKE_PUTS, bias_family, reasons

    if contango and (vix is not None and vix < VIX_SPIKE_LEVEL) and gate_posture != "CASH":
        reasons.append(
            f"contango + VIX<{VIX_SPIKE_LEVEL} + gate={gate_posture} -> SVIX_ON "
            f"(calendar_bias={bias_family}{bias_sign}, month={calendar_month})"
        )
        return SVIX_ON, bias_family, reasons

    if bias_family == "long_vol":
        reasons.append(f"Aug-Oct calendar bias, month={calendar_month} -> LONG_VOL_TACTICAL")
        return LONG_VOL_TACTICAL, bias_family, reasons

    reasons.append("no posture condition met -> CASH")
    return CASH, bias_family, reasons


def run(fade_spike_ok: bool = False) -> RegimeResult:
    gate_posture, gate_score = _read_gate()

    try:
        uw = get_client()
        term = uw.vix_term()
        data_age_sec = time.time() - term.get("fetched_at", time.time())
    except UWError as exc:
        print(f"[vix_regime] UW vix_term() failed: {exc}")
        term = {"vix": None, "vix3m": None, "vx1": None, "vx2": None}
        data_age_sec = float("inf")

    calendar_month = _calendar_month()
    posture, bias_family, reasons = compute_posture(
        vix=term.get("vix"), vix3m=term.get("vix3m"),
        vx1=term.get("vx1"), vx2=term.get("vx2"),
        gate_posture=gate_posture, gate_score=gate_score,
        calendar_month=calendar_month, data_age_sec=data_age_sec,
        fade_spike_ok=fade_spike_ok,
    )

    if term.get("vx1") is not None and term.get("vx2") is not None:
        regime = "BACKWARDATION" if term["vx1"] > term["vx2"] else "CONTANGO"
    elif term.get("vix") is not None and term.get("vix3m"):
        regime = "BACKWARDATION" if term["vix"] > term["vix3m"] else "CONTANGO"
    else:
        regime = "UNKNOWN"

    result = RegimeResult(
        vix=term.get("vix"), vix3m=term.get("vix3m"), vx1=term.get("vx1"), vx2=term.get("vx2"),
        regime=regime, days_in_regime=_days_in_regime(regime),
        gate_posture=gate_posture, gate_score=gate_score,
        calendar_month=calendar_month, calendar_bias=bias_family,
        posture=posture, reasons=reasons, data_age_sec=data_age_sec,
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
