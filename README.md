# Portfolio Trading System — RH

Robinhood portfolio monitor with Claude AI news analysis and iMessage alerts. Runs daily at 9:00 AM ET, tracks held positions and a watchlist derived from the LS Equity Fund factor model, and fires alerts when targets/stops are hit or macro risk shifts.

This system is a **monitor**, not an execution engine — alerts are advisory. `broker/robinhood.py` can place live orders (see Order Placement below), but nothing here does so automatically; every trade is a manual/explicit call.

---

## Architecture

```
loop_daily_rh.py                (cron entry point — wraps run_daily.py's layers directly)
  │
  ├── Layer 0  universe builder   — live RH positions + top LS Equity LONG picks
  ├── Layer 1  valuation          — prices, Greeks, IV for held positions
  ├── Layer 2  analytics          — P&L, sector allocation, aggregate Greeks
  ├── Layer 3  news / macro       — Claude AI news summary per ticker + regime_trader macro gate
  ├── Layer 4  alerts / dashboard — target/stop checks, IV spikes, iMessage
  ├── SKILL.md suppression filter — drops alerts matched by a known-noisy rule (see below)
  └── SKILL.md lesson writer      — Claude appends one dated lesson per run
```

`run_daily.py` is the same 4-layer pipeline for standalone/manual runs; `loop_daily_rh.py` (in the `TVClaude/` root, one level up) is what the cron actually invokes — it adds the suppression filter and lesson-writing step on top.

Two accounts monitored:
- **Margin account** `579611880`
- **Agentic account** `725024723`

---

## Quick Start

### View today's dashboard output
```bash
tail -100 ~/Library/Logs/TVClaude/daily.log
```

### View errors
```bash
cat ~/Library/Logs/TVClaude/daily.err
```

### Run manually
```bash
cd "/Users/pterzian/Desktop/TVClaude/Portfolio Trading System-RH"
venv/bin/python run_daily.py
```

### Run the full cron pipeline manually (suppression + lesson writer included)
```bash
cd /Users/pterzian/Desktop/TVClaude
"Portfolio Trading System-RH/venv/bin/python" loop_daily_rh.py
```

### Run research (screen tickers and pick contracts)
```bash
venv/bin/python run_research.py
# Review output, then edit data/positions/positions.json before running run_daily.py
```

---

## Automation

Runs via LaunchAgent (`com.tvclaude.portfolio.daily`) at **9:00 AM ET / 6:00 AM PT** on weekdays, invoking `loop_daily_rh.py`.

```bash
# Check status
launchctl list | grep portfolio

# Start agent
launchctl load ~/Library/LaunchAgents/com.tvclaude.portfolio.daily.plist

# Stop agent
launchctl unload ~/Library/LaunchAgents/com.tvclaude.portfolio.daily.plist

# Trigger manually (bypasses schedule)
launchctl kickstart -k gui/$(id -u)/com.tvclaude.portfolio.daily
```

Logs:
- `~/Library/Logs/TVClaude/daily.log` — stdout
- `~/Library/Logs/TVClaude/daily.err` — stderr

(Not the local `logs/` directory in this folder — LaunchAgents can't reliably write under `~/Desktop` due to macOS TCC sandboxing, so cron output goes to `~/Library/Logs/TVClaude/` instead. `logs/daily.log` here is a stale leftover from before that move.)

---

## Robinhood Session Management

`robin_stocks` access tokens expire ~24h. `monitor/layer0_universe.py::_rh_login()` handles this without a fresh interactive login on every run:

1. Loads `~/.tokens/robinhood.pickle` and checks the access token's JWT `exp`
2. If still valid, injects it directly (no network call)
3. If expired, refreshes via the OAuth `/oauth2/token/` endpoint and saves the rotated tokens back to the pickle
4. Only falls back to a full `rh.login()` if the refresh token itself is dead — that path triggers Robinhood's device-approval push challenge, which nobody's around to approve on a 9am unattended cron, and will 429-loop and fail

If you see `held: []` in the log for no reason, or a 429 on `get_prompts_status`, the refresh token has died. Fix:
```bash
"Portfolio Trading System-RH/venv/bin/python" /Users/pterzian/Desktop/TVClaude/rh_reauth.py
```
This does a full login and prompts for a device-approval tap on your phone, then saves a fresh pickle.

---

## Alert Suppression

`loop_daily_rh.py` runs every raw alert from Layer 4 through `_should_suppress_alert()` before sending, using rules read from `SKILL.md`:

| Rule | Suppresses |
|---|---|
| `STOP HIT` during `CASH` macro posture | Position should already be closed under CASH posture — alert is noise |
| `DRAWDOWN STOP` when NAV < $50 | Portfolio too small for the daily-loss-% threshold to mean anything; hypersensitive to single moves |

Suppressed alerts are logged (`[SUPPRESSED] ... — <reason>`) and included in the day's `SKILL.md` lesson, but never sent to iMessage. Add new rules to `_should_suppress_alert()` as noisy patterns are identified.

---

## Configuration

All settings come from `.env` in the project root:

| Variable | Description | Default |
|---|---|---|
| `ACCOUNT_BUDGET` | Total budget for position sizing | `66000` |
| `MAX_POSITION_PCT` | Max allocation per position | `0.20` (20%) |
| `MIN_DTE` | Minimum days-to-expiry for options | `45` |
| `MAX_DAILY_LOSS_PCT` | Portfolio drawdown alert threshold | `0.05` (5%) |
| `RH_USERNAME` | Robinhood login email | required |
| `RH_PASSWORD` | Robinhood login password | required |
| `ANTHROPIC_API_KEY` | Claude API key for news analysis | required |
| `IMESSAGE_RECIPIENT` | iMessage address for alerts | required |

---

## Alert Types

| Alert | Trigger |
|---|---|
| `TARGET HIT` | Position P&L ≥ +50% |
| `STOP HIT` | Position P&L ≤ -30% |
| `IV SPIKE / CRUSH` | IV moves ≥ 10 percentage points day-over-day |
| `NEW STRIKE` | Option strike detected that wasn't in yesterday's snapshot |
| `DRAWDOWN STOP` | Portfolio-level daily loss hits `MAX_DAILY_LOSS_PCT` |

Alerts fire to iMessage via AppleScript and print to the dashboard (subject to suppression — see above).

---

## Order Placement

`broker/robinhood.py` (`place_equity_order` / `place_option_order` / `cancel_order`) can place live orders on the real (non-paper) Robinhood accounts — nothing in this system calls it automatically, it's there for manual/explicit trades. As written today it always uses `rh.login()` (full username/password, not the pickle-refresh path — see Session Management above) and doesn't expose `account_number`, so it always targets the primary account and can hit the same device-approval-challenge risk on an unattended run.

For a one-off manual close on a specific account, it's simpler to reuse the already-working session directly instead:

```python
from monitor.layer0_universe import _rh_login, _AGENTIC_ACCOUNT  # or _MARGIN_ACCOUNT

rh = _rh_login()
rh.order_sell_market("MO", 3, account_number=_AGENTIC_ACCOUNT, timeInForce="gfd")
```

Two gotchas either way:
- `order_sell_market`/`order_buy_market` need `timeInForce="gfd"` (good-for-day) — `robin_stocks`' default `"gtc"` gets rejected by Robinhood on market orders (`'Invalid Good Til Canceled order.'`).
- Pass `account_number=` explicitly for a non-primary account — both `robin_stocks` and `broker/robinhood.py`'s wrapper default to the primary account otherwise.

---

## Watchlist / Universe

Layer 0 builds the universe from two sources:

1. **Held RH positions** — pulled live from both accounts
2. **LS Equity Fund top longs** — top 10 LONG-flagged tickers by composite score from `../ls_equity_fund/output/scored_universe_latest.csv`

News analysis (Layer 3) covers the full universe. Valuation and alerts (Layers 1/4) only act on held positions.

---

## Macro Gate

Layer 3 imports `macro_gate` from `../regime_trader` for a 6-signal macro score:

- Score 0–100; posture: `FULL` (≥75) / `REDUCED` (≥50) / `DEFENSIVE` / `CASH`
- Falls back to a VIX/SPY/credit spread calculation if the import fails

---

## Positions File

`data/positions/positions.json` — manually maintained list of held positions for research mode. Holds both share and option entries.

Example entries:
```json
[
  {"type": "share", "ticker": "MO", "quantity": 3, "cost_basis": 73.60, "sector": "Consumer Staples"},
  {"type": "option", "ticker": "AAPL", "strike": 200, "expiry": "2026-09-19",
   "option_type": "call", "contracts": 1, "cost_basis": 450, "sector": "Technology"}
]
```

Snapshots are saved daily to `data/snapshots/YYYY-MM-DD.json` for day-over-day comparison.

---

## Directory Structure

```
Portfolio Trading System-RH/
├── run_daily.py          # 4-layer pipeline, standalone/manual entry point
├── run_research.py       # one-time ticker screening + contract picking
├── config.py             # env vars, paths, budget constants
├── SKILL.md              # dated lesson log + alert-suppression rules + session-mgmt notes
├── .env                  # credentials (not committed)
├── monitor/
│   ├── layer0_universe.py   # includes _rh_login() pickle-refresh session handling
│   ├── layer1_valuation.py
│   ├── layer2_analytics.py
│   ├── layer3_news.py
│   └── layer4_alerts.py
├── broker/
│   └── robinhood.py      # RH login + place_equity_order/place_option_order/cancel_order
├── research/
│   ├── screener.py       # Claude-based ticker screening
│   └── contract_picker.py
├── alerts/
│   └── imessage.py       # AppleScript iMessage sender
├── data/
│   ├── positions/positions.json   # manually maintained holdings
│   └── snapshots/                 # daily JSON snapshots
└── logs/                 # stale — see "Automation" above for the real log path
    ├── daily.log
    └── daily.err

../                        # TVClaude root
├── loop_daily_rh.py       # cron entry point: wraps run_daily.py + suppression + lessons
└── rh_reauth.py           # manual full re-auth when the refresh token has died
```
