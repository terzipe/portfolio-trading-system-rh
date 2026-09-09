"""
send_alert() -- fan one alert out to iMessage AND a Discord webhook.

Each channel is independent and fail-soft: one being down (or unconfigured)
never blocks the other or the caller. iMessage behaves exactly as it did
before this module existed; Discord fires only when a webhook URL is
resolved (explicit `webhook_url`, else `os.getenv(webhook_env)`).

Call sites pass the per-bot webhook, e.g.
    send_alert("VIX Trader:\\n" + msg, webhook_url=config.DISCORD_WEBHOOK_VIX)
"""
import os

from alerts.discord import send_discord
from alerts.imessage import send_imessage


def send_alert(
    message: str,
    *,
    webhook_url: str | None = None,
    webhook_env: str | None = None,
    imessage: bool = True,
    imessage_recipient: str | None = None,
) -> None:
    if imessage:
        send_imessage(message, recipient=imessage_recipient)
    url = webhook_url or (os.getenv(webhook_env) if webhook_env else None)
    send_discord(message, url)
