"""
Send an iMessage via AppleScript. macOS only.
"""
import subprocess
from config import IMESSAGE_RECIPIENT


def send_imessage(message: str, recipient: str | None = None):
    to = recipient or IMESSAGE_RECIPIENT
    if not to:
        print(f"[alerts] iMessage recipient not set — skipping. Message:\n{message}")
        return
    script = f'tell application "Messages" to send "{message}" to buddy "{to}" of (service 1 whose service type is iMessage)'
    subprocess.run(["osascript", "-e", script], check=True)
