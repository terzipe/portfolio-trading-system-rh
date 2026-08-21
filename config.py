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

# ── SVIX ladder strategy (replaces the old contango-carry SVIX_ON/FLATTEN_SVIX
# posture entirely — see VIX_SVIX_LADDER_STRATEGY_REQUIREMENTS.md) ──────────
VIX_LADDER_ARM_LEVEL = float(os.getenv("VIX_LADDER_ARM_LEVEL", 30))
VIX_LADDER_RUNG_STEP = float(os.getenv("VIX_LADDER_RUNG_STEP", 10))
VIX_LADDER_RUNG_DOLLARS = float(os.getenv("VIX_LADDER_RUNG_DOLLARS", 5000))
VIX_LADDER_BUDGET_PCT = float(os.getenv("VIX_LADDER_BUDGET_PCT", 0.15))
VIX_LADDER_PULLBACK_PCT = float(os.getenv("VIX_LADDER_PULLBACK_PCT", 0.03))
VIX_LADDER_TP_STEPS = [0.25, 0.50, 0.75, 1.00]

# ── Google Drive uploads (SRS §10.1 — non-fatal, off by default) ──────────
ENABLE_GDRIVE_UPLOAD = os.getenv("ENABLE_GDRIVE_UPLOAD", "false").lower() == "true"
GDRIVE_SERVICE_ACCOUNT_JSON = os.path.expanduser(
    os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "~/.tokens/gdrive_service_account.json")
)
GDRIVE_FOLDER_DOCS_ID = os.getenv("GDRIVE_FOLDER_DOCS_ID", "")
GDRIVE_FOLDER_DAILY_ID = os.getenv("GDRIVE_FOLDER_DAILY_ID", "")
GDRIVE_FOLDER_LESSONS_ID = os.getenv("GDRIVE_FOLDER_LESSONS_ID", "")
GDRIVE_CONVERT_HTML_TO_GDOC = os.getenv("GDRIVE_CONVERT_HTML_TO_GDOC", "true").lower() == "true"
