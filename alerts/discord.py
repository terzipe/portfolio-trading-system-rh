"""
Post an alert to a Discord webhook. stdlib only (urllib) so the loop
crash-handlers can call it without depending on `requests`.

Fail-soft by contract: never raises, never blocks the caller. A missing
URL, a network error, a timeout, or a non-2xx response are all logged to
stderr and swallowed -- an alerting outage must never take down a trading
run (same policy as alerts/imessage.py).
"""
import json
import sys
import urllib.error
import urllib.request

# Discord's hard limit on a webhook `content` field is 2000 chars; leave
# headroom for the ellipsis and any future prefixing.
_MAX_CONTENT = 1900


def send_discord(message: str, webhook_url: str | None) -> None:
    if not webhook_url:
        return
    body = message if len(message) <= _MAX_CONTENT else message[: _MAX_CONTENT - 1] + "…"
    data = json.dumps({"content": body}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "tvclaude-alerts/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                print(f"[alerts] Discord webhook returned HTTP {resp.status}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- alerting must never crash a run
        print(f"[alerts] Discord delivery failed ({exc}) -- continuing without crashing the run.", file=sys.stderr)
