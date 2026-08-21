# SVIX Ladder Strategy — Requirements v1.0

Final, fully-specified requirements after multi-turn design review (2026-08-20).
Supersedes the SVIX-related portions of the original SRS v1.4 posture engine.
This document is the source of truth for implementation — see
`VIX_OPERATIONS_GUIDE.md` for the resulting operator-facing behavior once built,
and `SKILL.md`'s VIX Trader section for build history/lessons.

## 1. What this replaces

Retires, completely, for SVIX specifically:
- `SVIX_ON` posture (calm-market contango buy, VIX<25) — `monitor/vix_regime.py`
- `FLATTEN_SVIX` posture *as it applies to SVIX* (VIX≥25 or backwardation exit)
- `monitor/vix_kill_switch.py` (the -15% SVIX P&L auto kill switch) — deleted
  entirely, module and all references, since sized/budgeted DCA replaces a
  P&L-based stop as the risk control.

Unchanged, independent of this strategy:
- `FADE_SPIKE_PUTS` and `LONG_VOL_TACTICAL` postures (UVXY/VXX options) —
  these trade options, not SVIX shares, and keep operating as-is.
- The manual "Flatten SVIX now" dashboard button — still the human override
  for anything unforeseen. No changes needed: it already sells whatever
  quantity the broker reports for SVIX, regardless of the ladder's internal
  lot-tracking.
- The global manual `VIX_KILL_SWITCH` env flag (operator override, unrelated
  to the P&L-based auto switch being removed).

## 2. Trigger

No SVIX activity while VIX ≤ 30. The ladder campaign **arms** the instant a
cycle observes VIX > 30.

## 3. Budget

Campaign budget = **15% of NAV, evaluated live every cycle** — not locked
once at arm time. Available room for new buys = `0.15 * NAV - (cost basis of
SVIX shares currently held)`. This live, current-holdings-based definition
is required for the re-arm mechanism (§7) to work: selling shares via
take-profit frees up budget for new rungs on a later leg up.

## 4. Entry ladder (scale-in)

- Fixed $5,000 tranches at VIX rungs 30, 40, 50, 60, 70, 80... continuing
  indefinitely (no max rung), bounded only by the budget in §3.
- A rung fires **the instant a cycle observes VIX ≥ that level** — no
  close-confirmation required.
- If the next full $5,000 tranche would exceed available budget, size that
  final tranche to the exact residual instead, then stop scaling (until
  budget frees up again per §7).
- A given rung level fires at most once per continuous campaign (no
  re-buying the same rung on a whipsaw back through it) — tracked via
  persisted rung history (§9).

## 5. Peak / pullback signal

Track `campaign_peak_vix` = highest VIX observed since the campaign armed
(monotonically non-decreasing while the campaign is open).

**Pulled back** state = current VIX is ≥3% below `campaign_peak_vix`. This
single signal drives both halves of §6 below — there is no separate hold
phase between "stop buying" and "start selling."

## 6. Buy/sell conditions (evaluated fresh every cycle — not a one-shot pipeline)

- **Buy** a rung when: VIX ≥ the next unbought rung level, AND *not*
  currently in the pulled-back state, AND budget available (§3).
- **Sell** the next take-profit chunk when: currently in the pulled-back
  state, AND there's an unclaimed step at the current P&L level (§8).

Because both conditions re-evaluate every cycle against a live budget and a
monotonic `campaign_peak_vix`, the campaign can move between buying and
selling phases multiple times within one continuous episode — this is the
deliberate fix for the whipsaw case (§7).

## 7. Re-arm on a new high (whipsaw handling)

If, after some take-profit selling has occurred, VIX pushes to a **new**
campaign high (exceeding the previous `campaign_peak_vix`) and crosses the
next unbought rung, buying resumes automatically per §6 — using budget freed
up by the earlier sells. The campaign does not need to go flat and
re-arm from scratch to participate in a second leg up.

Track `campaign_peak_shares` = highest cumulative SVIX share count ever
held in this campaign (a running high-water mark; increases only when new
rungs are bought, never decreases on its own).

## 8. Take-profit ladder

- While in the pulled-back state (§5): sell **25% of `campaign_peak_shares`**
  (non-compounding — four equal chunks summing to exactly 100% of that
  high-water mark) at each of four position P&L thresholds: +25%, +50%,
  +75%, +100%, evaluated against the blended (weighted-average) cost basis
  of currently-held shares.
- **Restart on re-arm:** if new rungs are bought after some take-profit
  steps have already fired (§7), the step counter resets to 0-of-4 and all
  four future chunks are recomputed as 25% of the *new*, larger
  `campaign_peak_shares` — not a continuation of the old percentage base.
  Shares already sold stay sold; this only governs steps not yet taken.

## 9. Position tracking

Per-rung (FIFO lot) records: timestamp, VIX level at purchase, price,
quantity. Used for accurate cost-basis accounting and an audit trail (same
FIFO convention already used elsewhere in this codebase, e.g.
`vix_ledger.fifo_realized_pnl()`). The take-profit P&L thresholds themselves
are evaluated against the blended/weighted-average cost of current holdings,
not per-lot independently.

## 10. State persistence

New module (`monitor/vix_ladder.py`) + new state file
(`data/vix/svix_ladder_state.json`), checked/updated every cycle in both
`loop_daily_vix.py` and `loop_intraday_vix.py`. Tracks: campaign
armed/idle, `campaign_peak_vix`, `campaign_peak_shares`, rung purchase
history, take-profit steps completed (0-4, resets per §8), current
pulled-back/not state. Full reset to idle only when held shares reach
zero — the next campaign arms fresh only on a new VIX≤30 → VIX>30 crossing.
