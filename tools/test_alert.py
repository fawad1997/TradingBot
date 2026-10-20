#!/usr/bin/env python3
"""Send one test alert through whatever channel config.json selects, and say exactly what went wrong if it fails.

Run this BEFORE starting the bot, so a misconfigured webhook is found now rather than during the first kill
switch. It uses the same config, the same secrets and the same notifier the bot uses, so a pass here means the
bot's alerts will work too.

    python tools/test_alert.py
    python tools/test_alert.py --message "custom text"
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ConfigManager, load_secrets          # noqa: E402
from app.notifications.channels import build                # noqa: E402

HINTS = {
    "discord": "NOTIFY_URL must be a Discord channel webhook URL: Channel settings -> Integrations -> "
               "Webhooks -> New Webhook -> Copy Webhook URL. It starts https://discord.com/api/webhooks/",
    "ntfy": "NOTIFY_URL must be your full ntfy topic URL, e.g. https://ntfy.sh/<something-long-and-unguessable>",
    "webhook": "NOTIFY_URL must be an endpoint that accepts POST {\"text\": \"...\"}",
    "email": "SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_TO must all be set. Gmail needs an APP PASSWORD, "
             "not your account password.",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--message", default="Test alert - if you can read this, alerts are working.")
    a = ap.parse_args()

    cfg = ConfigManager().app.alerts
    secrets = load_secrets()
    channel = str(getattr(cfg, "channel", "none")).lower()

    print(f"  alerts enabled : {cfg.enabled}")
    print(f"  channel        : {channel}")
    if not cfg.enabled:
        print('\n  Alerts are OFF. Set "alerts": {"enabled": true} in config/config.json.')
        return 1

    notifier = build(channel, secrets, 0)
    if notifier is None:
        print(f"\n  The {channel} channel is NOT configured, so alerts would go to the log only.")
        print(f"  {HINTS.get(channel, 'Check .env for the credential this channel needs.')}")
        return 1

    url = getattr(secrets, "notify_url", None)
    if url:
        print(f"  endpoint       : {url[:38]}...{url[-6:]}  (masked)")

    notifier.start()
    notifier.send(a.message)
    for _ in range(150):                      # up to ~15s for the worker to drain the queue
        if notifier.q.empty():
            break
        time.sleep(0.1)
    time.sleep(1.0)                           # let the final POST finish
    notifier.stop()

    if notifier.failures:
        print(f"\n  FAILED after retries ({notifier.failures}). The message did not get through.")
        print(f"  {HINTS.get(channel, '')}")
        print("  The log above this line carries the HTTP status or exception.")
        return 1
    print("\n  Sent. Check your Discord channel / phone - the message should be there within a few seconds.")
    print("  If nothing arrives, the endpoint accepted the request but delivers somewhere else: re-copy the URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
