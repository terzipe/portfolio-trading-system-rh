# VIX Trader BOT — Operations Guide

Quick reference for running the bot unattended. For the full build history and
"why" behind each design decision, see `SKILL.md`'s VIX Trader section. This
file only covers *what happens when* and *what to check*.

Broker: **Alpaca paper trading**. Data: **Unusual Whales**. As of 2026-08-20,
`ENABLE_VIX_AUTO_SELL` / `ENABLE_VIX_AUTO_BUY` / `ENABLE_VIX_AUTO_ROLL` are all
`true` — the bot is fully unattended.

---

## 1. When SVIX is entered and exited

SVIX is **shares only** (no SVIX options in this bot). One posture drives it,
recomputed every cycle by `monitor/vix_regime.py::compute_posture()`, checked
in this priority order:

| Priority | Condition | Posture | Effect |
|---|---|---|---|
| 1 | `VIX_KILL_SWITCH=true` | `FLATTEN_SVIX` | sell SVIX if held, no new buys |
| 1.5 | Auto kill switch tripped (SVIX P&L ≤ `VIX_SVIX_STOP_PCT`, -15% — see §3) | `FLATTEN_SVIX` | sell SVIX if held, no new buys/rolls, **stays tripped until manually reset** |
| 2 | UW data older than `VIX_STALE_SECONDS` (900s) | `CASH` | no new buys |
| 3 | VIX ≥ `VIX_SPIKE_LEVEL` (25) **or** VX1 > VX2 (backwardation) | `FLATTEN_SVIX` | **sell all SVIX** |
| 4 | Fade-spike criteria met (see §2) | `FADE_SPIKE_PUTS` | sell SVIX if held (conflict), buy puts instead |
| 5 | Contango + VIX < 25 + macro gate ≠ CASH | `SVIX_ON` | **buy SVIX** (if not already held) |
| 6 | Aug/Sep/Oct calendar bias | `LONG_VOL_TACTICAL` | sell SVIX if held (conflict), buy VXX calls instead |
| 7 | none of the above | `CASH` | sell SVIX if held, otherwise no-op |

**In plain terms:** SVIX is bought when the VIX curve is in contango, VIX is
under 25, and the macro gate isn't flashing CASH. It's sold the moment VIX
hits 25+, the curve flips to backwardation, either kill switch is active, or
UW data goes stale for 15+ minutes. Only one new SVIX buy is allowed per
calendar day (re-entry lock), and it's blocked entirely on any day a flatten
already happened.

**SVIX position P&L stop-loss** (`VIX_SVIX_STOP_PCT`, -15%) is wired in as of
2026-08-20 via `monitor/vix_kill_switch.py` — see §3. `VIX_SVIX_MTD_STOP_PCT`,
`VIX_SLEEVE_DAILY_LOSS_PCT`, and `VIX_RUNAWAY_PCT` are still defined in
`.env`/`config.py` but **not wired into any decision logic** — those remain a
gap to build, not a switch to flip, if you want a sleeve-wide or
month-to-date stop as well.

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

Two independent kill switches, both gate the same way (regime forces
`FLATTEN_SVIX`, executor blocks every entry/roll) but trigger and reset
differently.

### 3a. Manual — `VIX_KILL_SWITCH`

`VIX_KILL_SWITCH` in `.env` (default `false`). You flip it.

**What it does when `true`:**
- Regime engine forces posture to `FLATTEN_SVIX` immediately — highest
  priority check, before even reading VIX data.
- Executor blocks **every** entry and roll (`BUY_*`, `ROLL_OPTION`) with
  reason `VIX_KILL_SWITCH=true — flatten-only`.
- Flattens (`SELL_SVIX_ALL`, `CLOSE_OPTION`) are **exempt** — they still go
  through as long as the session is healthy. Flatten/close is never blocked
  by the kill switch, by design.

**What it does NOT do:**
- Does not cancel orders already submitted and sitting unfilled — cancel
  those directly in Alpaca if needed.
- Does not fire until the next process start. Edit `.env`, then either wait
  for the next scheduled cycle or force it immediately:
  ```
  launchctl kickstart -k gui/$(id -u)/com.tvclaude.vix.intraday
  ```
- Reset: edit `.env` back to `false`.

### 3b. Auto — SVIX P&L stop (`monitor/vix_kill_switch.py`)

Trips itself, on the bot's own decision, when the held SVIX position's
`pnl_pct <= VIX_SVIX_STOP_PCT` (-15%). Checked every cycle right after
positions are fetched, in both `loop_daily_vix.py` and `loop_intraday_vix.py`
(skipped entirely during `--dry-run`, so drills never trip it for real).

**Same gating effect as the manual switch** — `FLATTEN_SVIX` posture,
entries/rolls blocked with reason `auto kill switch tripped (SVIX P&L
stop)`, flattens still exempt — but a **separate, persisted state file**
(`data/vix/auto_kill_switch.json`), not the `.env` flag. State survives
process restarts.

**On trip:** an immediate, standalone iMessage fires right away (doesn't
wait for the cycle's normal end-of-run alert batch) — `🚨 VIX Trader AUTO
KILL SWITCH TRIPPED: ...`. The dashboard's System Health panel also shows it
prominently once the cache refreshes.

**Reset: manual only, by design** — it will never clear itself just because
price recovers above the stop. Two ways:
```
venv/bin/python -c "from monitor import vix_kill_switch; vix_kill_switch.reset()"
```
or the "Reset auto kill switch" button in the dashboard's Manual controls
(only visible while tripped).

**If the session is DEAD at the same time the kill switch is on:** zero
orders happen at all (not even the flatten) — it's alert-only in that case,
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
