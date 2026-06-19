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
