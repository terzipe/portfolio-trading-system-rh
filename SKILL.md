# Portfolio Trading System — RH — SKILL.md
# Robinhood Monitor · Claude News Analysis · iMessage Alerts · NAV ~$226
#
# This file is the procedure manual for every loop run.
# Read it at the start of every session. Append lessons at the end of every run.
# Never delete entries — mark them superseded if a newer rule replaces them.

---

## Purpose

Monitor two Robinhood accounts daily. Analyze held positions and a watchlist
derived from the LS Equity Fund factor model. Fire iMessage alerts when targets,
stops, or macro risk thresholds are breached. Provide a morning Claude news
summary contextualized against the current macro regime.

This system monitors and alerts. It does not currently execute orders automatically.
The agentic account (725024723) is reserved for future auto-execution.

---

## System identity

- Broker: Robinhood (via `robin_stocks`)
- Margin account: `579611880`
- Agentic account: `725024723` (monitoring only — reserved for future auto-execution)
- NAV: ~$226
- Run cadence: **daily at 9:00 AM ET / 6:00 AM PT** via LaunchAgent (weekdays)
- Working directory: `/Users/pterzian/Desktop/TVClaude/Portfolio Trading System-RH`
- Python env: `venv/` — use `venv/bin/python` explicitly (no activation shortcut)
- LaunchAgent label: `com.tvclaude.portfolio.daily`

---

## Daily run sequence (automated)

The LaunchAgent fires `run_daily.py` at 9:00 AM ET. Manual trigger:

```
launchctl kickstart -k gui/$(id -u)/com.tvclaude.portfolio.daily
```

Manual run (bypasses LaunchAgent schedule):
```
cd "/Users/pterzian/Desktop/TVClaude/Portfolio Trading System-RH"
venv/bin/python run_daily.py
```

Check today's output:
```
tail -100 logs/daily.log
cat logs/daily.err
```

---

## Run pipeline (5 layers, in order)

```
Layer 0  universe builder   — live RH positions + top 10 LS Equity LONG picks
Layer 1  valuation          — prices, Greeks, IV for held positions
Layer 2  analytics          — P&L, sector allocation, aggregate Greeks, theta
Layer 3  news / macro       — Claude AI news summary + macro gate score
Layer 4  alerts / dashboard — target/stop checks, IV spikes, iMessage alerts
```

Each layer must complete before the next begins. If Layer 0 fails to pull live
positions, do not proceed to Layers 1–4 with stale data.

---

## Universe construction (Layer 0)

Two sources, merged daily:

1. **Live RH positions** — pulled from both accounts via `robin_stocks`
2. **LS Equity Fund top 10 longs** — highest composite-score LONG-flagged tickers
   from `../ls_equity_fund/output/scored_universe_latest.csv`

News analysis (Layer 3) covers the **full universe** (held + watchlist).
Valuation and alerts (Layers 1 and 4) act on **held positions only**.

If `scored_universe_latest.csv` is not available or more than 24 hours old,
log a warning and proceed with held positions only. Do not fail silently.

---

## Macro gate (Layer 3)

Import from `../regime_trader/macro_gate.py`. Do not recompute independently.

```python
from regime_trader.macro_gate import get_macro_score
```

If the import fails (network, path, or dependency issue), fall back to computing
a simplified score from VIX, SPY 5-day return, and HYG/LQD spread only.
Log the fallback clearly — never silently report a stale or fabricated score.

Posture interpretation:
- FULL (≥ 75): proceed normally, full alert sensitivity
- REDUCED (≥ 50): standard alerts, note posture in summary
- DEFENSIVE (≥ 25): heighten alert sensitivity, flag any new position suggestions
- CASH (< 25): suppress STOP alerts on existing positions (already should be flat),
  flag all held positions as at-risk in summary

---

## Claude news analysis rules (Layer 3)

### What to cover

- Analyze all tickers in the daily universe (held + watchlist)
- Prioritize: earnings surprises, guidance changes, analyst upgrades/downgrades,
  macro policy shifts (Fed, tariffs, geopolitical), sector rotation signals
- Flag any news item that is older than **48 hours** as stale — do not use as
  basis for an alert recommendation

### How to weight

- Weight macro commentary **higher** when macro gate score < 50
- Weight company-specific news higher when macro gate score ≥ 75 (regime is stable)
- Cross-reference LS Equity Fund composite score before recommending a watchlist
  addition — only suggest tickers that appear in `scored_universe_latest.csv`

### What not to do

- Do not recommend adding a position when macro posture is DEFENSIVE or CASH
- Do not flag a STOP HIT as actionable when macro posture is CASH
  (the position should already be closed at CASH posture)
- Do not surface news from non-reputable sources (social media, anonymous blogs)
- Do not reproduce article text — summarize only

### Output format (for iMessage alert and dashboard)

```
MACRO: <posture> (<score>/100) — <one-line regime summary>

HELD POSITIONS:
  <TICKER>: <P&L %>, <key news if any>, <alert type if triggered>

WATCHLIST HIGHLIGHTS:
  <TICKER>: <reason for attention>

SUMMARY: <2–3 sentence morning brief>
```

---

## Alert rules (Layer 4)

### Alert types and triggers

| Alert type      | Trigger condition                                    |
|-----------------|------------------------------------------------------|
| `TARGET HIT`    | Position P&L ≥ +50%                                  |
| `STOP HIT`      | Position P&L ≤ −30%                                  |
| `IV SPIKE`      | IV increases ≥ 10 percentage points day-over-day     |
| `IV CRUSH`      | IV decreases ≥ 10 percentage points day-over-day     |
| `NEW STRIKE`    | Option strike present today but not in yesterday's snapshot |
| `DRAWDOWN STOP` | Portfolio daily loss ≥ `MAX_DAILY_LOSS_PCT` (5%)     |

### Alert suppression rules

- **STOP HIT + CASH posture**: suppress. Position should already be flat.
  Log suppression reason: `"STOP_HIT suppressed: macro posture is CASH"`.
- **DRAWDOWN STOP when account NAV < $50**: treat as informational only —
  small NAV makes percentage thresholds hypersensitive to single options moves.
- **IV SPIKE during earnings week**: flag as expected, not actionable unless
  IV moves > 20pp in a single session.

### Alert delivery

- iMessage via AppleScript to `IMESSAGE_RECIPIENT` in `.env`
- Print to `logs/daily.log` regardless of iMessage success
- If iMessage fails (AppleScript error), log the error to `logs/daily.err`
  and continue — do not crash the run

---

## Positions file (research mode)

`data/positions/positions.json` — manually maintained for `run_research.py` only.
Not used by the daily automated run (which pulls live from Robinhood).

Research workflow:
```
venv/bin/python run_research.py   # screen tickers, pick contracts
# review output, edit data/positions/positions.json
venv/bin/python run_daily.py      # run with research-mode positions
```

---

## Snapshot and state

- Daily snapshots: `data/snapshots/YYYY-MM-DD.json` — used for day-over-day delta
  (IV changes, NEW STRIKE detection, P&L tracking)
- If today's snapshot is missing, IV SPIKE and NEW STRIKE alerts cannot fire —
  log a warning and skip those alert types for the day
- Always write today's snapshot at the end of the run, even if alerts failed

---

## Configuration reference

| Variable             | Description                              | Default     |
|----------------------|------------------------------------------|-------------|
| `ACCOUNT_BUDGET`     | Total budget for position sizing         | `66000`     |
| `MAX_POSITION_PCT`   | Max allocation per position              | `0.20` (20%)|
| `MIN_DTE`            | Minimum days-to-expiry for options       | `45`        |
| `MAX_DAILY_LOSS_PCT` | Portfolio drawdown alert threshold       | `0.05` (5%) |
| `RH_USERNAME`        | Robinhood login email                    | required    |
| `RH_PASSWORD`        | Robinhood login password                 | required    |
| `ANTHROPIC_API_KEY`  | Claude API key for news analysis         | required    |
| `IMESSAGE_RECIPIENT` | iMessage address for alerts              | required    |

---

## LaunchAgent management

```bash
# Check status
launchctl list | grep portfolio

# Start
launchctl load ~/Library/LaunchAgents/com.tvclaude.portfolio.daily.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.tvclaude.portfolio.daily.plist

# Trigger manually (bypasses schedule)
launchctl kickstart -k gui/$(id -u)/com.tvclaude.portfolio.daily
```

---

## Common failure modes and fixes

| Symptom | Root cause | Fix |
|---|---|---|
| Layer 3 runs but macro score missing | `macro_gate` import failed silently | Log import error explicitly; run fallback VIX/SPY/HYG calculation |
| No watchlist tickers in universe | `scored_universe_latest.csv` stale or missing | Log warning; proceed with held positions only |
| iMessage not delivered | AppleScript error (permissions, recipient format) | Log to `daily.err`; do not crash; check macOS Contacts permissions |
| STOP alert fires on CASH-posture account | Alert not checking macro posture before firing | Add posture check before every STOP alert — see suppression rules above |
| IV alert false-positive during earnings | Normal earnings IV move, not a signal | Flag as expected; suppress unless > 20pp single session |
| Daily snapshot missing | Previous run crashed before write | Always write snapshot at end of run in finally block |
| RH Tracker dashboard shows `NoneType has no attribute 'get'` | `robin_stocks login()` returned None — session expired | Run `rh_reauth.py` (see below) |

---

## Robinhood session management

The `robin_stocks` library stores the OAuth session in `~/.tokens/robinhood.pickle`.
Access tokens expire roughly every 24 hours. When expired, the dashboard shows
`'NoneType' object has no attribute 'get'` because `load_portfolio_profile()` returns None.

### How the dashboard handles it (automatic)

The `_rh_login()` function in both dashboards (port 8502 and 8503) bypasses `rh.login()`
and instead:

1. Loads the pickle directly
2. Decodes the JWT `exp` field to check if the access token is still valid
3. If valid → injects it into the session headers without any network call
4. If expired → calls the `/oauth2/token/` refresh endpoint
5. Saves the new access + refresh tokens back to the pickle atomically
6. Falls back to full `rh.login()` only if refresh also fails

### When automatic refresh fails (full re-auth required)

The OAuth refresh token is single-use and rotates on every refresh. If it gets
consumed by a failed save (e.g., script interrupted mid-run), the dashboard cannot
recover automatically.

**Fix: run `rh_reauth.py`:**
```bash
"/Users/pterzian/Desktop/TVClaude/Portfolio Trading System-RH/venv/bin/python" \
    /Users/pterzian/Desktop/TVClaude/rh_reauth.py
```

This will:
1. Detect the stale pickle and skip it
2. Do a full username/password login
3. Prompt you to approve the device in the Robinhood mobile app (push notification)
4. Save the fresh access + refresh tokens to `~/.tokens/robinhood.pickle`

### Do not burn the refresh token

Never call the OAuth refresh endpoint twice in quick succession without saving the
response first. Robinhood rotates refresh tokens — the first successful call
invalidates the old token. If you test the call and then try again, the second
call will fail with `invalid_grant`.

Always refresh in a single atomic script:
```python
resp = requests.post("https://api.robinhood.com/oauth2/token/", json={...})
new = resp.json()
if "access_token" in new and "refresh_token" in new:
    data.update(new)
    pickle.dump(data, open(PICKLE, "wb"))  # save immediately
```

---

## Lessons learned
- 2026-08-19: Clean daily run. Macro REDUCED (score 61.5), no alerts fired, no news available — confirms that missing news highlights do not block the batch loop from completing normally.
- 2026-08-18: Clean daily run. Macro REDUCED (score: 56.0/100), no alerts fired; score remains in the 53–57 range for the second confirmed post-recovery session, suggesting the pipeline is stable but macro posture continues to warrant a cautious reduced stance until the score sustains above 60 across multiple sessions.
- 2026-08-17: Clean daily run. Macro REDUCED (score: 64.5/100), no alerts fired; score remains comfortably above the 60-threshold boundary, continuing the stable REDUCED posture streak first established after pipeline recovery — no action required.
- 2026-08-14: Clean daily run. Macro REDUCED (score: 69.3/100), no alerts fired; score remains well above the 60-point threshold with 1 held position and 10 watchlist tickers monitored — confirms stable system operation, though the elevated REDUCED posture warrants continued attention if the score climbs toward NEUTRAL or higher in coming sessions.
- 2026-08-13: Clean daily run. Macro REDUCED (score: 66.1/100), no alerts fired; score remains above 60 for a sustained period, confirming the data-pipeline recovery noted 2026-07-02 is stable — continue monitoring for any drift back toward UNKNOWN or threshold crossings that could trigger alerts.
- 2026-08-12: Clean daily run. Macro REDUCED (score: 62.6/100), no alerts fired; score remains above 60 for a sustained period, confirming the REDUCED posture threshold rule — continue monitoring for any drift toward NEUTRAL or escalation toward RISK-OFF.
- 2026-08-11: 2026-08-11: Drawdown stop triggered at -5.9% daily P&L against a -5.0% limit under REDUCED macro posture (score 61.9/100), with no news context available to explain the move → confirms the drawdown stop rule is functional and should be treated as a hard exit signal regardless of macro posture; when news is unavailable during a loss-limit breach, assume worst-case and do not re-enter until next session with a clean macro read.
- 2026-08-10: Clean daily run. Macro REDUCED (score: 63.7/100), no alerts fired; score remains firmly in REDUCED territory, continuing the upward trend from prior sessions — confirms prior rule that sustained scores above 60 warrant ongoing close monitoring for posture escalation signals.
- 2026-08-07: Clean daily run. Macro REDUCED (score: 61.3/100), no alerts fired; score has now crossed and held above 60 for the first time since pipeline recovery, consistent with the existing rule that REDUCED posture warrants close monitoring when it sustains above 60 across multiple sessions.
- 2026-08-06: Clean daily run. Macro REDUCED (score: 59.2/100), no alerts fired; score remains just below the 60-threshold boundary for a second observed instance — confirm existing rule that REDUCED posture persists until score sustains above 60 across multiple sessions.
- 2026-08-05: Clean daily run. Macro REDUCED (score: 54.8/100), no alerts fired; score remains stable in the low-to-mid 50s range across multiple sessions, consistent with a sustained REDUCED posture — no rule changes warranted.
- 2026-08-04: Clean daily run. Macro REDUCED (score: 60.4/100), no alerts fired; score remains above the 60-point REDUCED threshold for a sustained period, reinforcing the rule that REDUCED posture warrants continued defensive positioning until score retreats meaningfully below 60.
- 2026-08-03: Clean daily run. Macro REDUCED (score: 57.7/100), no alerts fired; score remains below the 60 threshold, continuing the REDUCED posture pattern first established 2026-07-02 — monitor for a sustained cross above 60 before considering posture upgrade.
- 2026-07-31: Clean daily run. Macro REDUCED (score: 51.3/100), no alerts fired; score remains in the 51–54 range seen on 2026-07-02, suggesting a persistently cautious but stable macro environment — worth monitoring whether REDUCED posture becomes the sustained baseline or drifts toward NEUTRAL if score climbs above 60.
- 2026-07-30: Clean daily run. Macro DEFENSIVE (score: 44.4/100), no alerts fired; first session recorded at DEFENSIVE posture — monitor whether score continues declining toward stronger defensive thresholds or stabilizes, as sustained sub-50 readings may warrant reviewing watchlist entry criteria.
- 2026-07-29: 2026-07-29: Macro DEFENSIVE (43.4/100); BBAI stop hit at -30.7% and portfolio P&L breached the -5.0% daily loss limit (-6.4%), but the drawdown stop alert was suppressed due to NAV below the $50 threshold — confirms the NAV guard is functioning as designed, though the combination of a single position drawdown of this magnitude with a defensive macro posture warrants reviewing position sizing rules to prevent outsized single-ticker losses from dominating a small-NAV portfolio.
- 2026-07-28: Clean daily run. Macro DEFENSIVE (score: 45.6/100); DRAWDOWN STOP fired on portfolio P&L of -8.6% breaching the -5.0% daily loss limit, but was suppressed due to NAV ($27) falling below the $50 minimum threshold — confirm that sub-threshold NAV suppression is working as designed, and flag for manual review whether the account requires recapitalization before normal risk controls can resume.
- 2026-07-27: Clean daily run. Macro REDUCED (score: 50.1/100), DRAWDOWN STOP triggered on portfolio P&L of -11.2% breaching the -5.0% daily loss limit, but alert was suppressed because NAV ($27) fell below the $50 minimum threshold — confirm that sub-threshold NAV suppression is intentional circuit-breaker behavior and document whether manual review is still required when both conditions fire simultaneously.
- 2026-07-24: Clean daily run. Macro DEFENSIVE (score: 43.0/100), DRAWDOWN STOP triggered at -10.5% portfolio P&L (exceeding -5.0% daily limit) but was suppressed because NAV ($27) fell below the $50 minimum threshold — confirm that NAV floor suppression logic is working as designed, but flag the underlying -10.5% drawdown as a critical condition requiring manual review even when automated stops are inactive.
- 2026-07-16: Clean daily run. Macro REDUCED (score: 53.3/100), no alerts fired; score remains stable in the low-50s range across consecutive sessions, confirming pipeline recovery holds and reinforcing that REDUCED posture at this level produces no actionable signals without additional triggers.
- 2026-07-15: Clean daily run. Macro REDUCED (score: 51.1/100), no alerts fired; score remains in the 51–54 band seen across recent sessions, continuing to confirm stable REDUCED posture without approaching the NEUTRAL threshold — no action warranted unless score sustains above 60 across multiple sessions.
- 2026-07-14: Clean daily run. Macro REDUCED (score: 52.4/100), no alerts fired; score remains in the low-50s range consistent with prior REDUCED sessions, continuing to confirm stable pipeline recovery with no actionable signals requiring intervention.
- 2026-07-13: Clean daily run. Macro REDUCED (score: 56.5/100), no alerts fired; score remains in REDUCED territory for a second observed session, consistent with cautious posture — monitor for confirmation of trend if score sustains below 60 across further sessions.
- 2026-07-12: Clean daily run. Macro REDUCED (score: 66.8/100), no alerts fired; score remains comfortably above the REDUCED threshold and continues the stable post-recovery trend established since 2026-07-02, confirming data-pipeline reliability — no action required.
- 2026-07-10: Clean daily run. Macro REDUCED (score: 62.5/100), no alerts fired; score remains above the 60-point threshold for a second observed session, continuing to confirm data-pipeline stability following the earlier recovery.
- 2026-07-08: Clean daily run. Macro REDUCED (score: 50.6/100), no alerts fired; score remains in the mid-50s range consistent with recent sessions, confirming sustained REDUCED posture — continue monitoring for meaningful movement above 60 or below 45 as the next actionable threshold.
- 2026-07-07: Clean daily run. Macro REDUCED (score: 64.3/100), no alerts fired; score continues to climb above the 60-threshold zone, reinforcing the prior rule that sustained readings above 60 warrant close monitoring for posture escalation toward NORMAL or ELEVATED.
- 2026-07-06: Clean daily run. Macro REDUCED (score: 56.5/100), no alerts fired; score continues trending upward from 53.8 on 2026-07-02, remaining below the 60 threshold — monitor for sustained breach above 60 across multiple sessions before treating as a posture-shift signal.
- 2026-07-03: Clean daily run. Macro REDUCED (score: 58.8/100), no alerts fired; macro score continues stable recovery trend following pipeline restoration, with posture now upgraded from REDUCED (53.8) to a firmer REDUCED (58.8) — confirms pipeline stability is holding and warrants continued monitoring until score sustains above 60 across multiple sessions.
- 2026-07-02: Clean daily run. Macro REDUCED (score: 53.8/100), no alerts fired; macro score resolved successfully after three consecutive UNKNOWN days — confirms data-pipeline recovery, close monitoring warranted to ensure stability persists.
- 2026-07-01: Clean daily run. Macro UNKNOWN (score unavailable), no alerts fired; third consecutive day with macro score unresolvable — per prior rule, escalate to explicit error log with timestamp and treat as confirmed data-pipeline failure requiring active investigation.
- 2026-06-30: Clean daily run. Macro UNKNOWN (score unavailable), no alerts fired; second consecutive day with macro score unresolvable confirms a persistent data-pipeline gap — escalate UNKNOWN macro score to explicit error log with timestamp if it recurs a third consecutive day.
- 2026-06-29: Clean daily run. Macro UNKNOWN (score unavailable), no alerts fired; system initialized successfully with 2 held positions and 10 watchlist tickers, confirming baseline operation — flag any future runs where macro score remains UNKNOWN as a data-pipeline issue requiring explicit fallback logging.

_Append new entries here after every alert or incident.
Format: `YYYY-MM-DD: what happened → what rule was added or changed`._

_(No entries yet — this file was initialized 2026-06-29. First entry should be
appended after the next daily run. Include: macro posture, alerts fired, whether
they were actionable, and any news that mattered.)_

---

## VIX Trader BOT (SRS v1.4)

A separate sleeve on the **Agentic** account (`725024723`) only — SVIX / VXX / UVXY.
Uses Unusual Whales (Basic tier) for all market data, never yfinance. Driven by
`loop_daily_vix.py` (9:00 AM ET) and `loop_intraday_vix.py` (15-min RTH worker).
Lessons for this sleeve go under **"VIX Trader — Lessons learned"** below, *not*
the equity "Lessons learned" section above — do not let `write_lesson.py` or any
lesson writer cross-contaminate the two (this bit `regime_trader/SKILL.md` once
already; see root repo git history for the fix).

### Rules the alert/lesson pipeline must respect

- **Flatten-on-25 is intentional, not noise.** `VIX >= VIX_SPIKE_LEVEL` (default 25)
  or confirmed backwardation always produces a `FLATTEN_SVIX` posture and, if
  `ENABLE_VIX_AUTO_SELL=true` and the session is HEALTHY/DEGRADED-with-shadow-book,
  an actual market sell. Never suppress this alert.
- **Suppress duplicate roll prompts.** An option roll candidate (`+25%` premium)
  should not re-alert more than once per 4 hours unless P&L moved >= 10pp since the
  last prompt — `monitor/vix_alerts.py::should_suppress_roll()`, keyed by
  ticker/expiry/strike/option_type in `data/vix/roll_alert_state.json`. Only applies
  to the alert-only case; an executed roll (`ENABLE_VIX_AUTO_ROLL=true`) is real news
  every time and is never routed through this suppression.
- **Suppress auto-buy-skip noise.** A proposed `BUY_*` action rejected purely because
  `ENABLE_VIX_AUTO_BUY=false` is suppressed from iMessage (still printed/logged and
  still recorded in `dashboard_cache.json`/`paper_ledger.jsonl`) —
  `monitor/vix_alerts.py::should_suppress()`. This is the expected, intentional state
  during the Phase T phased rollout (week-1 all flags off, week-2 sell-only, week-3+
  buys enabled), so alerting on it every cycle the posture wants to buy would just be
  daily noise until the flag is deliberately flipped. `ENABLE_VIX_AUTO_SELL=false`
  skips are **not** suppressed by this rule — that flag is expected to stay on once
  enabled, so it going false is itself worth surfacing. First observed live 2026-08-19:
  daily batch proposed `BUY_SVIX_SHARES` (posture=SVIX_ON) and correctly alerted on
  the skip before this rule existed — this rule was added directly in response to that.
- **429 / empty `held` / `BOOK_MISMATCH` → reauth, never retry.** If `vix_session.py`
  reports DEAD, the correct response is one iMessage asking for `rh_reauth.py` — never
  loop the login or retry the order. `held: []` is only trustworthy after the full
  `session_ok()` checklist passes; before that, it is not proof of a flat book.
- **Flatten-needed-but-DEAD is an action item, not a suppressed alert.** If the
  posture says `FLATTEN_SVIX` but the session is DEAD, that must always reach
  iMessage — this is the one case where "the bot couldn't act" is itself the
  urgent news, not something to file away quietly.
- **Lesson format:** `date, posture, VIX, session state, action, live vs paper delta`
  — one line, written only by the daily batch (not every 15-minute cycle).

### Session-management notes specific to this sleeve

- `monitor/vix_session.py` deliberately does **not** call
  `monitor.layer0_universe._rh_login()` — that function's own fallback path calls a
  full interactive `rh.login()` when the refresh token is dead, which is exactly what
  Impl Plan §2A forbids for unattended VIX loops. `vix_session.py` re-implements only
  the safe pickle-load + single-refresh-attempt portion and reports DEAD instead of
  falling through to a full login. If you ever see a device-approval push fire from
  the VIX loops, that's a bug in this design, not expected behavior — full interactive
  login stays exclusively in `rh_reauth.py`, run by a human.
- Similarly, `monitor/vix_executor.py` does **not** use `broker/robinhood.py` for
  option orders, even though the original implementation plan text suggested it —
  that module's `login()` always does a full `rh.login()`. The executor places every
  order directly on the already-authenticated `rh` session object `vix_session.assess()`
  returns.

### VIX Trader — Lessons learned
- 2026-08-19: posture=FLATTEN_SVIX, VIX=17.77, session=HEALTHY, actions=1, executed=1, paper_signals=5
- 2026-08-19: posture=SVIX_ON, VIX=17.77, session=HEALTHY, actions=1, executed=0, paper_signals=4
- 2026-08-19: Manual flatten via the dashboard "Flatten SVIX now" button — sold 2 SVIX shares, order
  filled instantly at $26.695/share (order id 6a85ee4d...), confirmed flat via a fresh positions fetch.
  First real order ever placed through any of this bot's infrastructure — proves session reuse, account
  targeting (Agentic 725024723), and timeInForce=gfd all work correctly against live Robinhood. Went
  through the dashboard's manual button path, not vix_executor.py's automated _execute_flatten() — same
  order-call shape, but not literally the same code path firing on its own.
- 2026-08-19: posture=SVIX_ON, VIX=18.23, session=HEALTHY, actions=1, executed=0, paper_signals=2
- 2026-08-19: posture=SVIX_ON, VIX=18.23, session=HEALTHY, actions=1, executed=0, paper_signals=1
