# SVIX Ladder Strategy — Requirements v2.0

Final, fully-specified requirements after multi-turn design review (2026-08-24).
Supersedes v1.0 of this document (2026-08-20, dollar-level ladder) and the
`VIX_LADDER_MAX_ARM_LEVEL=70` tail-guard ceiling added 2026-08-22. This
document is the source of truth for implementation — see
`VIX_OPERATIONS_GUIDE.md` for the resulting operator-facing behavior once
built, and `SKILL.md`'s VIX Trader section for build history/lessons.

## 1. What this replaces

Retires the **dollar-level ladder** entirely:
- Fixed VIX rungs 30/40/50/60/70 (`VIX_LADDER_ARM_LEVEL`,
  `VIX_LADDER_RUNG_STEP`) — replaced by percentile-derived rungs (§3).
- `VIX_LADDER_MAX_ARM_LEVEL=70` hard ceiling — replaced outright by the top
  percentile rung (99th, §3). No separate ceiling constant; the same
  resume-on-the-way-down semantics the old ceiling had now fall out of the
  99th rung being the last one in the list.

Unchanged, carried forward from v1.0 without modification:
- Budget sizing: $5,000/rung, 15%-of-NAV live budget cap (§4).
- Peak/pullback signal, buy/sell conditions, re-arm-on-new-high whipsaw
  handling, take-profit ladder, position tracking, and state persistence
  (§5–§9 below) — all still expressed in raw VIX-percent terms, not
  percentile terms. See §6 for why.
- Everything in v1.0 §1 ("What this replaces" re: `SVIX_ON`/`FLATTEN_SVIX`/
  `vix_kill_switch.py`) and the untouched-systems list (options postures,
  manual "Flatten SVIX now" button, manual `VIX_KILL_SWITCH`).

## 2. Percentile threshold source

- **Data:** FRED `VIXCLS` daily series (VIX index close), fetched fresh in
  full on each recompute — no incremental merge/gap-handling needed, the
  10-year pull is cheap.
- **Lookback:** trailing 10 years, spans both the 2008 and 2020 vol regimes
  by design.
- **Recompute cadence: weekly**, as part of the existing `vix.daily` Monday
  run (first trading day of the week). Chosen over daily because at a
  10-year window a single day's close moves the percentile boundary
  negligibly — the boundary only meaningfully shifts when an old extreme
  (e.g. a 2008 or 2020 print) ages out past the 10-year-ago cutoff, which is
  calendar-driven, not reactive to current-week vol. Weekly captures
  essentially the same information as daily at a fifth of the external-call
  surface, and keeps the rung table stable for legible week-over-week log
  comparison.
- **Formula:** for each target percentile *p*, find the VIX close level *L*
  such that the fraction of days in the trailing-10y series with close < L
  equals *p*. This produces a static table of five VIX-level thresholds
  (one per rung, §3), refreshed weekly. The live per-cycle comparison in
  `vix_ladder.py` then stays architecturally identical to v1.0: compare
  `vix_now` (still sourced from `uw.vix_term()`, unchanged) against a rung
  *level* — the level is just read from the weekly-refreshed table instead
  of a hardcoded constant.
- **Staleness guard:** if the weekly refresh fails (FRED unreachable, empty
  response, etc.), reuse the last cached threshold table and log a warning
  rather than blocking evaluation. If the cache is more than 14 days stale
  (two missed refreshes), refuse to arm new campaigns until a refresh
  succeeds — existing open campaigns continue operating on the stale table
  rather than being force-flattened.
- **New state artifact:** `data/vix/vix_percentile_state.json` — refresh
  timestamp, the five percentile→VIX-level thresholds, and the lookback
  window's start/end dates (for audit/debugging).

## 3. Entry ladder (scale-in) — percentile rungs

- Five rungs, at the 90th / 92.5th / 95th / 97.5th / 99th percentile of the
  trailing-10y VIX close distribution (§2), each resolved weekly to a
  concrete VIX level.
- $5,000 tranche per rung (unchanged from v1.0 §4), same residual-sizing
  behavior when a full tranche would exceed available budget.
- A rung fires the instant a cycle observes VIX ≥ that rung's *current*
  resolved level — no close-confirmation required (unchanged mechanic).
- **99th is the ceiling.** No rung exists above it; a VIX print at or beyond
  the 99th-percentile level buys (at most) the 99th rung and stops — same
  behavior the old `VIX_LADDER_MAX_ARM_LEVEL=70` cap produced, now implicit
  in the rung list itself rather than a separate constant. A literal
  10-year-record close (100th percentile by construction — no prior day in
  the window closed higher) is **not** given its own rung: it's already
  covered by "top 1% of days in a decade," and the ceiling's purpose (stop
  escalating risk at the extreme, not keep adding size as things get more
  extreme) argues against a discrete tier above 99th.
- A given rung fires at most once per continuous campaign (unchanged, same
  persisted rung-history tracking as v1.0 §4/§9).

## 4. Budget

Unchanged from v1.0 §3: 15% of NAV, evaluated live every cycle. Available
room for new buys = `0.15 * NAV - (cost basis of SVIX shares currently
held)`.

## 5. Arm trigger

No SVIX activity while VIX is below the 90th-percentile threshold (§2/§3).
The campaign **arms** the instant a cycle observes VIX ≥ that threshold —
same instant-arm mechanic as v1.0 §2, just keyed to the percentile-derived
level instead of the fixed VIX 30.

## 6. Peak / pullback signal, buy/sell conditions, re-arm, take-profit — unchanged

Kept in **raw VIX-percent terms**, not percentile terms, by explicit
decision: the pullback trigger and P&L take-profit ladder are driven by
VIX's *change from campaign peak* and the position's own P&L, not by
absolute VIX level or where VIX sits in the historical distribution —
switching the entry side to percentile doesn't require touching either.

One known consequence, not a defect: percentile rungs are not evenly
spaced in VIX points (the tail is fat — 90th→92.5th may be a couple of
points while 97.5th→99th can be much wider), unlike the old evenly-spaced
30/40/50/60/70 ladder. A fixed 3%-of-peak pullback therefore no longer has
a consistent relationship to "how many rungs back did we retreat" across a
campaign — it may cross several rung boundaries near the lower rungs and
none near the upper ones. This doesn't change any exit logic; it just means
rung count and pullback magnitude aren't as visually correlated as before.

All of v1.0 §5–§9 carry forward verbatim:
- §5 Peak/pullback signal (`campaign_peak_vix`, 3%-below-peak = pulled back)
- §6 Buy/sell conditions (evaluated fresh every cycle)
- §7 Re-arm on a new high (whipsaw handling), `campaign_peak_shares`
- §8 Take-profit ladder (25% of `campaign_peak_shares` per +25/50/75/100pp
  P&L step, blended cost basis, restart-on-re-arm)
- §9 Position tracking (FIFO lots, blended cost basis for P&L thresholds)

## 7. State persistence

`monitor/vix_ladder.py` + `data/vix/svix_ladder_state.json` — unchanged
from v1.0 §10 (campaign armed/idle, peak tracking, rung history, take-profit
steps, pulled-back state, full reset only when held shares reach zero).

New, in addition: the percentile-threshold refresh (§2) is a separate,
independently-cached concern (`data/vix/vix_percentile_state.json`) — it
does not reset with the campaign and persists across arm/idle cycles, since
it describes the historical distribution, not campaign state.
