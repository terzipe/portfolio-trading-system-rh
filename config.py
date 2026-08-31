import os
import pathlib
from dotenv import load_dotenv

load_dotenv()

ACCOUNT_BUDGET = float(os.getenv("ACCOUNT_BUDGET", 66000))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", 0.20))
MIN_DTE = int(os.getenv("MIN_DTE", 45))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", 0.05))

RH_USERNAME = os.getenv("RH_USERNAME")
RH_PASSWORD = os.getenv("RH_PASSWORD")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
IMESSAGE_RECIPIENT = os.getenv("IMESSAGE_RECIPIENT")

BASE_DIR = pathlib.Path(__file__).parent
SNAPSHOTS_DIR = BASE_DIR / "data" / "snapshots"
POSITIONS_FILE = BASE_DIR / "data" / "positions" / "positions.json"

SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── VIX Trader BOT (SRS v1.4) ───────────────────────────────────────────────
# Independent of MIN_DTE above — VIX options ignore the equity MIN_DTE=45
# (SRS §2 decision #12). Never import the equity MIN_DTE into vix_options.py.
UW_API_KEY = os.getenv("UW_API_KEY")
UW_TIER = os.getenv("UW_TIER", "basic")  # basic|advanced
UW_HAS_WEBSOCKET = os.getenv("UW_HAS_WEBSOCKET", "false").lower() == "true"
UW_HAS_CME_FUTURES = os.getenv("UW_HAS_CME_FUTURES", "false").lower() == "true"

# Alpaca paper trading (VIX bot's broker; no AGENTIC/MARGIN split — one
# paper account per API key pair, unlike the RH account this replaced).
ALPACA_API_KEY_ID = os.getenv("ALPACA_API_KEY_ID")
ALPACA_API_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY")

VIX_SLEEVE_MAX_PCT = float(os.getenv("VIX_SLEEVE_MAX_PCT", 0.05))
VIX_SVIX_MAX_PCT = float(os.getenv("VIX_SVIX_MAX_PCT", 0.05))
VIX_MAX_CONTRACTS = int(os.getenv("VIX_MAX_CONTRACTS", 5))
VIX_KILL_SWITCH = os.getenv("VIX_KILL_SWITCH", "false").lower() == "true"

ENABLE_VIX_AUTO_SELL = os.getenv("ENABLE_VIX_AUTO_SELL", "true").lower() == "true"
ENABLE_VIX_AUTO_BUY = os.getenv("ENABLE_VIX_AUTO_BUY", "false").lower() == "true"
ENABLE_VIX_AUTO_ROLL = os.getenv("ENABLE_VIX_AUTO_ROLL", "false").lower() == "true"

VIX_SPIKE_LEVEL = float(os.getenv("VIX_SPIKE_LEVEL", 25))
VIX_SVIX_STOP_PCT = float(os.getenv("VIX_SVIX_STOP_PCT", -0.15))
VIX_SVIX_MTD_STOP_PCT = float(os.getenv("VIX_SVIX_MTD_STOP_PCT", -0.20))
VIX_SLEEVE_DAILY_LOSS_PCT = float(os.getenv("VIX_SLEEVE_DAILY_LOSS_PCT", 0.02))
VIX_OPTION_ROLL_PCT = float(os.getenv("VIX_OPTION_ROLL_PCT", 0.25))
VIX_OPTION_TP_PCT = float(os.getenv("VIX_OPTION_TP_PCT", 0.50))
VIX_OPTION_SL_PCT = float(os.getenv("VIX_OPTION_SL_PCT", -0.50))
VIX_RUNAWAY_PCT = float(os.getenv("VIX_RUNAWAY_PCT", 0.50))

VIX_MIN_DTE = int(os.getenv("VIX_MIN_DTE", 10))
VIX_MAX_DTE = int(os.getenv("VIX_MAX_DTE", 21))
VIX_CALL_MIN_DTE = int(os.getenv("VIX_CALL_MIN_DTE", 21))
VIX_CALL_MAX_DTE = int(os.getenv("VIX_CALL_MAX_DTE", 45))

VIX_LOOP_SECONDS = int(os.getenv("VIX_LOOP_SECONDS", 900))
VIX_STALE_SECONDS = int(os.getenv("VIX_STALE_SECONDS", 900))
VIX_SESSION_DEAD_COOLDOWN_SEC = int(os.getenv("VIX_SESSION_DEAD_COOLDOWN_SEC", 900))
VIX_TOKEN_REAUTH_WARN_SEC = int(os.getenv("VIX_TOKEN_REAUTH_WARN_SEC", 7200))
VIX_SHADOW_BOOK_MAX_AGE_SEC = int(os.getenv("VIX_SHADOW_BOOK_MAX_AGE_SEC", 3600))

VIX_DATA_DIR = BASE_DIR / "data" / "vix"
VIX_DATA_DIR.mkdir(parents=True, exist_ok=True)
VIX_SESSION_STATE_FILE = VIX_DATA_DIR / "session_state.json"
VIX_SHADOW_BOOK_FILE = VIX_DATA_DIR / "last_known_positions.json"
VIX_STATE_FILE = VIX_DATA_DIR / "state.json"
VIX_PAPER_LEDGER_FILE = VIX_DATA_DIR / "paper_ledger.jsonl"
VIX_ROLL_ALERT_STATE_FILE = VIX_DATA_DIR / "roll_alert_state.json"
VIX_SVIX_LADDER_STATE_FILE = VIX_DATA_DIR / "svix_ladder_state.json"
VIX_PERCENTILE_STATE_FILE = VIX_DATA_DIR / "vix_percentile_state.json"

# ── SVIX ladder strategy (replaces the old contango-carry SVIX_ON/FLATTEN_SVIX
# posture entirely — see VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md v2.0) ────
# Entry rungs are percentile-of-trailing-10y-VIX-distribution, not fixed VIX
# levels (see monitor/vix_percentile.py) -- 99th is the top rung and IS the
# tail-guard ceiling outright, no separate MAX_ARM_LEVEL constant.
VIX_PERCENTILE_RUNGS = [90, 92.5, 95, 97.5, 99]
VIX_PERCENTILE_LOOKBACK_YEARS = int(os.getenv("VIX_PERCENTILE_LOOKBACK_YEARS", 10))
# How often the FRED-derived threshold table is allowed to refresh. Weekly,
# not daily: at a 10y window one day's close moves the boundary negligibly
# (see spec §2) -- this is a floor on refresh frequency, not a schedule.
VIX_PERCENTILE_REFRESH_INTERVAL_DAYS = int(os.getenv("VIX_PERCENTILE_REFRESH_INTERVAL_DAYS", 7))
# Cache older than this blocks arming NEW campaigns (two missed weekly
# refreshes); an already-open campaign keeps running on the stale table
# rather than being force-flattened.
VIX_PERCENTILE_STALE_DAYS = int(os.getenv("VIX_PERCENTILE_STALE_DAYS", 14))

VIX_LADDER_RUNG_DOLLARS = float(os.getenv("VIX_LADDER_RUNG_DOLLARS", 5000))
VIX_LADDER_BUDGET_PCT = float(os.getenv("VIX_LADDER_BUDGET_PCT", 0.15))
VIX_LADDER_PULLBACK_PCT = float(os.getenv("VIX_LADDER_PULLBACK_PCT", 0.03))
VIX_LADDER_TP_STEPS = [0.25, 0.50, 0.75, 1.00]

# ── LONG_VOL_TACTICAL data-driven gates (replaces the old pure Aug-Oct
# calendar trigger -- see monitor/vix_longvol_gates.py). Score >= 2 of 3
# fires the posture; calendar is dropped from the decision entirely.
# Gate A: VIX below this percentile of trailing VIX_LONGVOL_PERCENTILE_
# LOOKBACK_YEARS of history == "cheap" (own lookback window, decoupled
# from the SVIX ladder's VIX_PERCENTILE_LOOKBACK_YEARS -- see
# monitor/vix_percentile.py's get_gate_a_threshold()/refresh()).
# 15th-pct-of-10y (2026-08-25 sweep) found the best win rate (67%) in the
# full grid, but a 10y lookback spans multiple full vol cycles (2018
# Volmageddon, 2020 COVID, 2022 bear market) -- VIX hadn't closed below
# that threshold even once in the trailing 2 years as of 2026-08-26,
# leaving the gate structurally unable to fire in the current regime.
# 20th-pct-of-7y (2026-08-26 lookback sweep) was the best result at any
# shorter lookback -- 61% win rate (vs 67%) but 28 signals vs 18, and
# fires multiple times in Aug 2026 alone vs. zero for the 10y version --
# a real "more opportunities for slightly lower per-signal quality"
# tradeoff, confirmed his choice over the higher-win-rate/lower-frequency
# original.
VIX_LONGVOL_CHEAP_PERCENTILE = float(os.getenv("VIX_LONGVOL_CHEAP_PERCENTILE", 20))
VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS = float(os.getenv("VIX_LONGVOL_PERCENTILE_LOOKBACK_YEARS", 7))
# Gate B (term structure) and Gate C (VXX momentum) both compare "now" to
# this many trading sessions ago -- one shared, configurable lookback.
VIX_LONGVOL_LOOKBACK_SESSIONS = int(os.getenv("VIX_LONGVOL_LOOKBACK_SESSIONS", 15))
# Gate C: VXX must be up at least this much over the lookback to confirm
# (a magnitude floor so it doesn't fire on noise).
VIX_LONGVOL_MOMENTUM_MIN_PCT = float(os.getenv("VIX_LONGVOL_MOMENTUM_MIN_PCT", 0.10))
# Gate B: the VIX/VIX3M ratio must have fallen by at least this fraction of
# its value N sessions ago to confirm (0.0 = any decrease at all counts,
# the original definition -- backtesting 2026-08-25 found this too loose,
# firing on essentially any wiggle).
VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT = float(os.getenv("VIX_LONGVOL_TERM_STRUCTURE_MIN_PCT", 0.05))
VIX_LONGVOL_MIN_GATES = int(os.getenv("VIX_LONGVOL_MIN_GATES", 2))

# ── SVIX manual campaign (monitor/svix_manual_campaign.py) — a SECOND,
# independent SVIX campaign alongside the ladder above. The ladder buys SVIX
# at high-VIX percentile rungs on the way down after a spike; this one buys
# at low, literal SVIX price levels and holds through calm regimes, so it
# needs its own fast, leading-indicator-driven exit instead of the ladder's
# budget-cap-only risk control. Confirmed design, 2026-08-29. Entry is
# manual (dashboard button only, no auto-buy path); exit is automatic.
# State (data/vix/svix_manual_state.json) and order submission are fully
# isolated from vix_ladder.py's state AND from vix_executor.execute_actions()'s
# shared data/vix/state.json whipsaw-guard (see svix_manual_campaign.py
# module docstring) -- both campaigns share one Alpaca account/ticker, so
# every order this campaign submits is sized off its OWN ledger, never off
# Alpaca's aggregate SVIX position.
SVIX_MANUAL_RUNGS = [20.0, 18.0, 16.0, 14.0, 12.0, 10.0]  # literal SVIX price, descending
SVIX_MANUAL_RUNG_DOLLARS = float(os.getenv("SVIX_MANUAL_RUNG_DOLLARS", 3000))
SVIX_MANUAL_BUDGET_DOLLARS = float(os.getenv("SVIX_MANUAL_BUDGET_DOLLARS", 15000))
# Tier-2 (compression) arms a resting stop this far below the price at the
# moment it arms. Calibrated via backtest_svix_manual.py --sweep, re-run
# 2026-08-29 under the live VIX_LEADING_TIER3_CONFIRM_DAYS=2 regime: 3%
# (tighter than the original 5%) paired with term_min_pct=5% gave the best
# realized P&L in the grid at the same drawdown-avoided as looser stops --
# see the VIX_LEADING_TERM_STRUCTURE_MIN_PCT comment above for the full
# comparison.
SVIX_MANUAL_STOP_PCT = float(os.getenv("SVIX_MANUAL_STOP_PCT", 0.03))
# Tier-1 (VVIX/VIX divergence) wired live 2026-08-29 -- previously advisory
# only. Arms a WIDER, looser stop than tier 2's (more room, since tier 1 is
# the earliest/least-confirmed signal in his priority ordering) -- the stop
# only ever ratchets TIGHTER as a stronger tier confirms or price falls
# further, never loosens (see svix_manual_campaign.run_exit_cycle()).
#
# Swept 2026-08-29 (backtest_svix_manual.py, fixed at the already-calibrated
# comp=15/term=5%/stop=3%): NOT a strictly-dominant win at any value, a real
# tradeoff between protection and P&L --
#   5%  -> BEST drawdown-avoided found in this whole calibration effort,
#          +8.3pp (vs. +6.2pp with tier 1 unwired) -- but realized P&L drops
#          to $22,318 (-9% vs. $24,463 unwired).
#   15% -> realized P&L $24,491, essentially matches the unwired baseline on
#          BOTH metrics -- tier 1 becomes mostly harmless padding at this
#          width (rarely breached before tier 2/3 take over).
#   8-10% (the untested starting guess) -> worst of both worlds: lower P&L
#          AND no better protection than unwired. Do not use.
# Defaulted to 5% -- his original framing of this whole campaign explicitly
# prioritized avoiding drawdowns over maximizing P&L ("its imperative to
# avoid drawdowns by exiting when vol begins to show signs of elevation").
# Set SVIX_MANUAL_TIER1_STOP_PCT=0.15 instead if P&L should be weighted
# higher than the extra ~2pp of drawdown protection.
SVIX_MANUAL_TIER1_STOP_PCT = float(os.getenv("SVIX_MANUAL_TIER1_STOP_PCT", 0.05))
SVIX_MANUAL_STATE_FILE = VIX_DATA_DIR / "svix_manual_state.json"
SVIX_MANUAL_CACHE_FILE = VIX_DATA_DIR / "svix_manual_cache.json"

# ── Leading-indicator exit stack (monitor/vix_leading_signals.py) — VVIX/
# VIX divergence (tier 1, primary), VIX+VVIX range compression (tier 2),
# front-of-curve term structure (tier 3), confirmers (tier 4, never used
# alone). VVIX/SKEW come from yfinance (not on FRED); VIX/VIX3M continue to
# come from data/fred.py.
#
# Calibrated via backtest_svix_manual.py --sweep, RE-RUN 2026-08-29 after
# VIX_LEADING_TIER3_CONFIRM_DAYS=2 went live (the first 2026-08-29 sweep
# ran under the old 1-day assumption and is superseded by this one).
# compression_percentile=15 (bumped from 10 in the first sweep) remains
# best, unchanged. term_min_pct and stop_pct BOTH moved this round:
# term_min_pct 3%->5%, stop_pct 5%->3% -- best combo (comp=15, term=5%,
# stop=3%) gives $24,463 realized vs. the 3%/5% combo's $21,427 (+14%), at
# the SAME +6.2pp drawdown-avoided and slightly MORE time-in-market
# (43% vs 39%) -- not a tradeoff, strictly better on this backtest. This
# only works BECAUSE tier 3 now requires 2 consecutive days -- a looser
# single-day term threshold would have been too false-positive-prone (see
# the pre-2-day-gating numbers this superseded). VIX_LEADING_DIVERGENCE_MIN_PP
# swept 5/10/15% with ZERO effect on any result at any combo, again --
# confirms tier 1 is still advisory-only, unwired to any action.
#
# DANGER ZONE, found in this sweep, not present before: comp=5 or comp=10
# combined with term=5% and stop=10% (and a few nearby cells) produced
# NEGATIVE drawdown-avoided (-4.3pp) -- i.e. WORSE than never exiting at
# all. Do not loosen compression_percentile below ~15 while term_min_pct is
# at 5% without re-sweeping; the two interact.
VIX_LEADING_DIVERGENCE_SESSIONS = int(os.getenv("VIX_LEADING_DIVERGENCE_SESSIONS", 5))
VIX_LEADING_DIVERGENCE_MIN_PP = float(os.getenv("VIX_LEADING_DIVERGENCE_MIN_PP", 0.10))
VIX_LEADING_COMPRESSION_WINDOW = int(os.getenv("VIX_LEADING_COMPRESSION_WINDOW", 20))
VIX_LEADING_COMPRESSION_PERCENTILE = float(os.getenv("VIX_LEADING_COMPRESSION_PERCENTILE", 15))
VIX_LEADING_COMPRESSION_LOOKBACK_YEARS = float(os.getenv("VIX_LEADING_COMPRESSION_LOOKBACK_YEARS", 2))
# Tier 3 reuses vix_longvol_gates.term_structure_gate()'s machinery but with
# its own, sharper/shorter lookback -- Gate B's 15-session default is tuned
# for confirming an ENTRY, not for the earliest possible EXIT warning.
VIX_LEADING_TERM_STRUCTURE_SESSIONS = int(os.getenv("VIX_LEADING_TERM_STRUCTURE_SESSIONS", 5))
VIX_LEADING_TERM_STRUCTURE_MIN_PCT = float(os.getenv("VIX_LEADING_TERM_STRUCTURE_MIN_PCT", 0.05))
VIX_LEADING_SKEW_SESSIONS = int(os.getenv("VIX_LEADING_SKEW_SESSIONS", 5))
VIX_LEADING_SKEW_MIN_PCT = float(os.getenv("VIX_LEADING_SKEW_MIN_PCT", 0.03))
# Wired live 2026-08-29 after backtest_svix_manual.py --tier3-confirm-days
# investigation: the un-gated (1-day) version flattened 133/134 of all
# sells and same-day round-tripped 37% of entries (bought and immediately
# flattened before ever holding overnight) -- tier 3 alone was functioning
# as a near-single-tier system. Requiring 2 CONSECUTIVE trading days
# confirmed raised time-in-market 33%->39%, realized P&L $18,037->$21,427
# (+19%), and cut same-day round-trips to 30% of buys, at the cost of
# drawdown-avoided softening from +8.2pp to +6.2pp -- a real but modest
# tradeoff. 3-day was tested too and overshoots badly (drawdown-avoided
# collapses to +0.7pp, a real -32.8% mark got through nearly unprotected) --
# do not raise this past 2 without re-validating via the sweep.
VIX_LEADING_TIER3_CONFIRM_DAYS = int(os.getenv("VIX_LEADING_TIER3_CONFIRM_DAYS", 2))
VIX_LEADING_STATE_FILE = VIX_DATA_DIR / "vix_leading_state.json"

# ── Google Drive uploads (SRS §10.1 — non-fatal, off by default) ──────────
ENABLE_GDRIVE_UPLOAD = os.getenv("ENABLE_GDRIVE_UPLOAD", "false").lower() == "true"
GDRIVE_SERVICE_ACCOUNT_JSON = os.path.expanduser(
    os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "~/.tokens/gdrive_service_account.json")
)
GDRIVE_FOLDER_DOCS_ID = os.getenv("GDRIVE_FOLDER_DOCS_ID", "")
GDRIVE_FOLDER_DAILY_ID = os.getenv("GDRIVE_FOLDER_DAILY_ID", "")
GDRIVE_FOLDER_LESSONS_ID = os.getenv("GDRIVE_FOLDER_LESSONS_ID", "")
GDRIVE_CONVERT_HTML_TO_GDOC = os.getenv("GDRIVE_CONVERT_HTML_TO_GDOC", "true").lower() == "true"
