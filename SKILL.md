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

---

## Lessons learned
- 2026-06-29: Clean daily run. Macro UNKNOWN (score unavailable), no alerts fired; system initialized successfully with 2 held positions and 10 watchlist tickers, confirming baseline operation — flag any future runs where macro score remains UNKNOWN as a data-pipeline issue requiring explicit fallback logging.

_Append new entries here after every alert or incident.
Format: `YYYY-MM-DD: what happened → what rule was added or changed`._

_(No entries yet — this file was initialized 2026-06-29. First entry should be
appended after the next daily run. Include: macro posture, alerts fired, whether
they were actionable, and any news that mattered.)_
