#!/usr/bin/env python3
"""
One-shot runner for GitHub Actions.

Loads configuration from environment variables, runs one OurDomain check, and
exits. It never prints Telegram secrets.
"""

from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from ourdomain_telegram_bot import DEFAULT_OURDOMAIN_URL, check_once


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    url = os.getenv("OURDOMAIN_URL", DEFAULT_OURDOMAIN_URL).strip() or DEFAULT_OURDOMAIN_URL
    state_path = os.getenv("STATE_PATH", "ourdomain_state.json").strip() or "ourdomain_state.json"

    print(f"[ourdomain-once] url={url}", flush=True)
    print(f"[ourdomain-once] state_path={state_path}", flush=True)
    print(
        f"[ourdomain-once] send_existing_on_first_run={os.getenv('SEND_EXISTING_ON_FIRST_RUN', 'false')}",
        flush=True,
    )
    print(f"[ourdomain-once] send_heartbeat={os.getenv('SEND_HEARTBEAT', 'true')}", flush=True)
    print(
        f"[ourdomain-once] heartbeat_interval_minutes={os.getenv('HEARTBEAT_INTERVAL_MINUTES', '60')}",
        flush=True,
    )
    print(f"[ourdomain-once] headless={os.getenv('HEADLESS', 'true')}", flush=True)
    print(f"[ourdomain-once] browser_locale={os.getenv('BROWSER_LOCALE', 'en-GB')}", flush=True)
    print(f"[ourdomain-once] slow_mo_ms={os.getenv('SLOW_MO_MS', '0')}", flush=True)

    try:
        found, alert_worthy, sent = check_once(
            token=token,
            chat_id=chat_id,
            url=url,
            state_path=state_path,
        )
    except Exception as exc:
        print(f"[ourdomain-once] FATAL: {exc!r}", file=sys.stderr, flush=True)
        return 1

    print(
        f"[ourdomain-once] done plans_found={found} alert_worthy={alert_worthy} alerts_sent={sent}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
