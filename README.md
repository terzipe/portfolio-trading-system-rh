# Portfolio Trading System — RH

Robinhood portfolio monitor with Claude AI news analysis and iMessage alerts. Runs daily at 9:00 AM ET, tracks held positions and a watchlist derived from the LS Equity Fund factor model, and fires alerts when targets/stops are hit or macro risk shifts.

---

## Architecture

```
run_daily.py
  │
  ├── Layer 0  universe builder   — live RH positions + top LS Equity LONG picks
  ├── Layer 1  valuation          — prices, Greeks, IV for held positions
  ├── Layer 2  analytics          — P&L, sector allocation, aggregate Greeks
  ├── Layer 3  news / macro       — Claude AI news summary per ticker + regime_trader macro gate
  └── Layer 4  alerts / dashboard — target/stop checks, IV spikes, iMessage
```

Two accounts monitored:
- **Margin account** `579611880`
- **Agentic account** `725024723`

---

## Quick Start

### View today's dashboard output
```bash
tail -100 "logs/daily.log"
```

### View errors
```bash
cat "logs/daily.err"
```

### Run manually
```bash
cd "/Users/pterzian/Desktop/TVClaude/Portfolio Trading System-RH"
venv/bin/python run_daily.py
```

### Run research (screen tickers and pick contracts)
```bash
venv/bin/python run_research.py
# Review output, then edit data/positions/positions.json before running run_daily.py
```

---

## Automation

Runs via LaunchAgent at **9:00 AM ET / 6:00 AM PT** on weekdays.

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
- `logs/daily.log` — stdout
- `logs/daily.err` — stderr

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

Alerts fire to iMessage via AppleScript and print to the dashboard.

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
├── run_daily.py          # main entry point (daily cron)
├── run_research.py       # one-time ticker screening + contract picking
├── config.py             # env vars, paths, budget constants
├── .env                  # credentials (not committed)
├── monitor/
│   ├── layer0_universe.py
│   ├── layer1_valuation.py
│   ├── layer2_analytics.py
│   ├── layer3_news.py
│   └── layer4_alerts.py
├── broker/
│   └── robinhood.py      # RH login wrapper
├── research/
│   ├── screener.py       # Claude-based ticker screening
│   └── contract_picker.py
├── alerts/
│   └── imessage.py       # AppleScript iMessage sender
├── data/
│   ├── positions/positions.json   # manually maintained holdings
│   └── snapshots/                 # daily JSON snapshots
└── logs/
    ├── daily.log
    └── daily.err
```
