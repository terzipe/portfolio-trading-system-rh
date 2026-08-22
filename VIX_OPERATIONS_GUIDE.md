# VIX Trader BOT — Operations Guide

Quick reference for running the bot unattended. For the full build history and
"why" behind each design decision, see `SKILL.md`'s VIX Trader section. This
file only covers *what happens when* and *what to check*.

Broker: **Alpaca paper trading**. Data: **Unusual Whales**. As of 2026-08-20,
`ENABLE_VIX_AUTO_SELL` / `ENABLE_VIX_AUTO_BUY` / `ENABLE_VIX_AUTO_ROLL` are all
`true` — the bot is fully unattended.

---

## 1. When SVIX is entered and exited

SVIX is **shares only** (no SVIX options in this bot) and, as of 2026-08-20,
is bought/sold entirely by its own stateful campaign —
`monitor/vix_ladder.py` — completely independent of the options posture
engine (§2). It's not a per-cycle decision; it's a multi-day campaign
persisted in `data/vix/svix_ladder_state.json`, replacing the old
`SVIX_ON`/`FLATTEN_SVIX` calm-market posture entirely. Full spec:
`VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md`.

### Entry (scale-in)
- No SVIX activity while VIX ≤ `VIX_LADDER_ARM_LEVEL` (30). The instant a
  cycle observes VIX > 30, the campaign **arms**.
- Buys $`VIX_LADDER_RUNG_DOLLARS` (5,000) at each VIX rung — 30, 40, 50,
  60, ... — the instant that level is observed (no close-confirmation
  needed), continuing until the next full rung would exceed the campaign's
  budget (`VIX_LADDER_BUDGET_PCT`, 15% of NAV, evaluated live against
  currently-held cost basis), at which point the final tranche is sized to
  the exact residual.
- Each rung level fires **at most once** per campaign — no re-buying the
  same rung on a whipsaw retest.
- **Tail-guard ceiling:** rungs above `VIX_LADDER_MAX_ARM_LEVEL` (70) are
  never bought — a VIX blowout into crisis territory (70+) stops adding new
  risk, so max buying is the rungs 30/40/50/60/70 ($25k at the default rung
  size, still bounded by the 15%-of-NAV budget). The ceiling caps the rung
  *level*, it is **not** a one-way latch: if VIX later rounds back down under
  70 while still in the buying regime, any skipped lower rungs become
  eligible again (resume-on-the-way-down). Existing holdings, peak tracking,
  and take-profit are unaffected — the ceiling only blocks *new* rungs above
  it.
- **No re-entry lock / one-per-day cap applies** — the ladder can buy
  several rungs in a single day if VIX rips through multiple levels; its
  own rung history is what prevents duplicates, not the generic per-day
  lock other entries still use.

### Peak / pullback / re-arm
- Tracks `campaign_peak_vix` — the highest VIX seen since arming, never
  decreases while the campaign is open.
- **Pulled back** = current VIX is ≥3% (`VIX_LADDER_PULLBACK_PCT`) below
  `campaign_peak_vix`. This single signal both stops further rung buying
  *and* starts take-profit selling — no separate hold phase in between.
- **Re-arm on a new high:** if VIX later pushes to a fresh campaign high
  (past the old peak) and crosses the next unbought rung, buying resumes
  automatically — using budget freed up by any earlier take-profit sells.
  A partially-sold position never sits permanently stuck waiting for a
  second leg up (this was a real gap in the first draft of the design,
  fixed before shipping — see `SKILL.md`).

### Exit (take-profit)
- While pulled back: sells 25% of the campaign's peak share count at each
  of four P&L thresholds — +25%, +50%, +75%, +100% — against the blended
  cost of currently-held shares. The final step sells everything
  remaining, not a rounded quarter, so nothing gets stranded.
- If new rungs are bought after some take-profit steps have already fired
  (the re-arm case above), the step counter **restarts at 0/4**, now
  measured against the new, larger peak share count.
- Full reset to idle only when held shares reach zero — the next campaign
  only arms on a fresh VIX≤30→VIX>30 crossing. Also self-heals: if real
  positions ever show zero SVIX while the campaign thinks otherwise (e.g.
  a manual flatten), it resets to idle automatically next cycle.

**No P&L-based stop-loss** — the 15%-of-NAV budget cap is the *only* risk
control (deliberately: the strategy is supposed to hold through some
drawdown while scaling in, so a P&L stop would fight the strategy itself).
`VIX_SVIX_STOP_PCT`, `VIX_SVIX_MTD_STOP_PCT`, `VIX_SLEEVE_DAILY_LOSS_PCT`,
and `VIX_RUNAWAY_PCT` remain defined in `.env`/`config.py` but unwired.

---

## 2. When options are bought, closed, or rolled

Two independent systems touch options — entry posture, and per-position P&L
management. They run every cycle, in that order.

### Entry (posture-driven, `vix_signals.decide_actions()`)

| Posture | Action | Contract picked by |
|---|---|---|
| `FADE_SPIKE_PUTS` | Buy 1 put (UVXY primary, VXX fallback if UVXY's book is too thin/wide) | `vix_options.pick_put()` — 10-21 DTE, ~10% OTM |
| `LONG_VOL_TACTICAL` | Buy 1 VXX call | `vix_options.pick_call()` — 21-45 DTE, delta 0.40-0.60 |

`FADE_SPIKE_PUTS` fires when **all** of: UVXY is up ≥30% in the last ≤10
sessions *or* VIX spiked to ≥25 from a sub-20 base, **and** the most recent
close is a lower high (spike already fading). `LONG_VOL_TACTICAL` fires
Aug/Sep/Oct when nothing else is held and no put/call is already open.

### Contract sizing (`vix_executor._size_option_contracts()`)

Each option entry sizes to the **smaller** of two limits:
- **Sleeve budget:** `VIX_SLEEVE_MAX_PCT` (5%) × NAV, minus option value
  already held, divided by one contract's premium (mid × 100), rounded down.
- **Flat cap:** `VIX_MAX_CONTRACTS` (5) contracts, regardless of budget.

The flat cap is almost always what binds — e.g. a ~$399k account buying a
VXX call at ~$0.83 mid: the 5% budget would allow ~240 contracts, but the
5-contract cap caps it at 5 (~$412 total). Sizing to 0 (premium exceeds
headroom, or a zero/untradeable quote) is skipped with a reason, not
ordered.

### Re-entry lock (when the next option entry is allowed)

Options entries are held to **one new entry per calendar day**, and once a
position is closed/flattened *this* day, no new entry into it happens again
until the next day (`vix_executor._entry_locked()` /
`last_flatten_session`). This is a whipsaw guard — in normal operation it
rarely binds, since `LONG_VOL_TACTICAL` only changes on a month boundary.
It *will* bind for the rest of the day after any manual close done for
testing. Skips show as `re-entry locked — flattened this session` or
`1 new entry per session already used`. Because the loops run **weekdays
only**, the practical earliest re-entry after a same-day close is the next
weekday's 9:00 AM cycle. **The SVIX ladder is exempt** (see §1) — its own
rung history prevents duplicates instead.

### Exit / management (P&L-driven, `vix_signals.decide_option_management()`,
independent of posture — runs on every open UVXY/VXX option regardless of
what posture currently says)

| Trigger | Action |
|---|---|
| `pnl_pct <= -50%` (`VIX_OPTION_SL_PCT`) | **Close** (stop-loss) |
| `pnl_pct >= +50%` (`VIX_OPTION_TP_PCT`) | **Close** (take-profit) |
| `pnl_pct >= +25%` (`VIX_OPTION_ROLL_PCT`) | **Roll** (close old, open a fresh same-size replacement in a new DTE window) |

Stop-loss and take-profit are checked *before* the roll threshold, so a
position that's up 50%+ closes outright rather than rolling. `pnl_pct` comes
from a **live UW mark** fetched fresh every cycle — if UW can't match the
contract or has no real market on it, `pnl_pct` is `None` and the position is
silently skipped (fails closed, never guesses).

**Order pricing:** entries pay the live ask (marketable). Closes/rolls
re-quote the live bid immediately before submitting and price there,
falling back to the position's last-known mark only if a fresh quote can't
be fetched. All option orders are **limit orders, never market orders** — on
a thin contract an order can still sit unfilled for a while even at a fresh
bid/ask. Check the dashboard's Manual controls to close by hand if needed.

---

## 3. Kill switch

`VIX_KILL_SWITCH` in `.env` (default `false`). You flip it manually — there
is no auto-trip mechanism. (An earlier auto kill switch existed briefly,
tripping on a -15% SVIX P&L stop, but was removed 2026-08-20 in favor of
the ladder's own 15%-of-NAV budget cap as the risk control — a P&L stop
would have fought the ladder strategy itself, which is supposed to hold
through some drawdown while scaling in.)

**What it does when `true`:**
- Options posture forced to `CASH` — no new `FADE_SPIKE_PUTS`/
  `LONG_VOL_TACTICAL` entries.
- Ladder rung buys (`BUY_SVIX_RUNG`) are blocked too — same "no new risk"
  gating as any other entry.
- Flattens/closes/take-profit sells (`SELL_SVIX_ALL`, `CLOSE_OPTION`,
  `SELL_SVIX_PARTIAL`) are **exempt** — they still go through as long as
  the session is healthy. Exits are never blocked by the kill switch, by
  design.

**What it does NOT do:**
- Does not cancel orders already submitted and sitting unfilled — cancel
  those directly in Alpaca if needed.
- Does not fire until the next process start. Edit `.env`, then either wait
  for the next scheduled cycle or force it immediately:
  ```
  launchctl kickstart -k gui/$(id -u)/com.tvclaude.vix.intraday
  ```
- Reset: edit `.env` back to `false`.

**If the session is DEAD at the same time the kill switch is on:** zero
orders happen at all (not even an exit) — it's alert-only in that case,
since there's no usable broker client to submit through.

---

## 4. Schedule

| LaunchAgent | Fires | Behavior |
|---|---|---|
| `com.tvclaude.vix.daily` | 9:00 AM ET, weekdays | One batch cycle: session → regime → decide → execute → paper ledger → SKILL.md lesson → dashboard cache → Drive upload |
| `com.tvclaude.vix.intraday` | 9:30 AM ET, weekdays | Long-running worker; waits until 9:35 ET (RTH start), then runs a cycle every `VIX_LOOP_SECONDS` (900s / 15 min) until 15:55 ET, then exits |

Manual trigger (bypasses schedule, runs a real cycle right now):
```
launchctl kickstart -k gui/$(id -u)/com.tvclaude.vix.daily
launchctl kickstart -k gui/$(id -u)/com.tvclaude.vix.intraday
```

Single manual cycle without touching the LaunchAgent (useful for testing):
```
cd "Portfolio Trading System-RH" && venv/bin/python ../loop_intraday_vix.py --once
```
Add `--dry-run` to preview without submitting/recording anything.

---

## 5. Monitoring — where to look

### Dashboard (`regime_trader` → VIX Trader tab) — start here
- **System Health** panel: `UW_API_KEY` set/missing, **Last real cycle**
  timestamp (the single best "is it alive" signal — during market hours this
  should be no more than ~15-20 min old; if it's stale, something stopped),
  LaunchAgent loaded/not-loaded status, and raw tails of all 4 log files.
- **Account (Alpaca)**: real equity/cash/buying power — ties exactly to
  Alpaca's own numbers.
- **Sleeve P&L**: unrealized + realized, sourced from Alpaca's own
  positions and `/account/activities` — includes trades placed manually
  (dashboard button, or directly in Alpaca's UI), not just bot-placed ones.
- **SVIX Ladder**: campaign status — armed/idle, peak VIX, rungs bought,
  take-profit steps completed, plus any orders **submitted but not yet
  confirmed filled** (the `pending_orders` line, added with the 2026-08-21
  race-condition fix — a rung/sell is only promoted to a "real" holding once
  Alpaca confirms the fill, so a queued after-hours order shows here in the
  meantime). The place to check "where is the ladder right now" without
  reading `data/vix/svix_ladder_state.json` directly.
- **Bot decision log**: every action the bot considered, including
  skipped/rejected ones with a reason — this is the *only* place to see
  proposed-but-not-executed actions (they never hit Alpaca's activity feed).
- **Manual controls**: flatten SVIX, close any open option position by hand.

### Logs
`~/Library/Logs/TVClaude/`: `vix_daily.log`/`.err`, `vix_intraday.log`/`.err`.

What to look for:
- `state=DEAD reason=...` — session isn't usable that cycle. Common reasons:
  `ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not set`, `account/equity check
  failed`, `positions call failed`, `BOOK_MISMATCH: held:[] but shadow book
  had SVIX/VXX/UVXY within max age` (fail-closed if positions unexpectedly
  come back empty). After a DEAD result, there's a 15-min cooldown before
  the next attempt (`VIX_SESSION_DEAD_COOLDOWN_SEC`) — seeing DEAD twice in a
  row within 15 min is expected, not a new problem.
- `order unconfirmed — stopping cycle` — an order was submitted but didn't
  reach a confirmed state within 20s; the cycle stops rather than risking a
  second order on top of an uncertain one.
- `[FATAL] loop_daily_vix.py crashed` / `[FATAL] loop_intraday_vix.py crashed`
  — both scripts send an iMessage on an uncaught crash as of 2026-08-20 (same
  self-contained `osascript` fallback in both, deliberately not routed
  through the normal alerts module in case that's what's broken). For
  intraday specifically, this only catches what escapes the per-cycle loop
  entirely (startup errors, the RTH-wait sleep path) — a per-cycle exception
  *inside* the loop is still caught locally and logged as `[intraday] cycle
  failed (continuing)` with no alert, and it just tries again next interval.
  That's intentional resilience, not a gap — but it means a string of quiet
  per-cycle failures still won't page you; check the log directly if the
  dashboard's "Last real cycle" timestamp looks stale.
- `[SUPPRESSED] ...` — an alert was intentionally not sent to iMessage
  (still logged, still in the dashboard). Two suppression rules: repeated
  `ENABLE_VIX_AUTO_BUY=false` rejections (not applicable now that it's
  `true`), and duplicate roll-candidate alerts on the same position within
  4 hours unless P&L moved ≥10 points.

### iMessage alerts
- Daily: sent every cycle if there's anything to report (any executed order,
  any non-trivial skip, or a DEAD session).
- Intraday: sent only on a "notable" event — posture change, an executed
  order, a DEAD session, or a rejected action — not every cycle. A quiet
  intraday run with no alert is normal.

### SKILL.md lessons
`Portfolio Trading System-RH/SKILL.md`, "VIX Trader — Lessons learned"
section — one line appended per daily cycle (`posture=..., VIX=...,
session=..., actions=N, executed=N, paper_signals=N`), newest at the top.
Useful for a quick historical scan of session health without opening logs.
