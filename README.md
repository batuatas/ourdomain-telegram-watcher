# OurDomain GitHub Actions Telegram Watcher

Watches the OurDomain Amsterdam South East RentCafe floorplans page and sends Telegram alerts when selected floor plans show an availability-like action button.

The watcher uses Playwright with headless Chromium because the page is JavaScript-rendered and may present browser checks to simple HTTP clients. It is designed to run from GitHub Actions every 5 minutes, with state persisted in `ourdomain_state.json`.

## Safety

This GitHub bot is intentionally safer than the old Tampermonkey watcher:

- It does not auto-click Apply, Start Application, Reserve, or Lease by default.
- It does not submit applications.
- It does not log in.
- It does not fill forms.
- It only detects visible floorplan action/contact button state and sends a Telegram message.

## Watched Floor Plans

- 2 Bedroom Superior
- 2 Bedroom
- 1 Bedroom Superior
- 1 Bedroom
- Studio Suite
- Executive Plus Studio
- Standard Studio
- Superior Studio
- Executive Studio
- Executive Plus Studio - Furnished

## GitHub Actions Setup

Add these repository secrets:

1. Go to **Settings -> Secrets and variables -> Actions -> New repository secret**.
2. Add `TELEGRAM_BOT_TOKEN`.
3. Add `TELEGRAM_CHAT_ID`.

Never commit real Telegram credentials. `.env` is ignored and should stay local.

The workflow lives at `.github/workflows/ourdomain-check.yml` and runs on:

- manual `workflow_dispatch`
- cron `3-59/5 * * * *`, which runs every 5 minutes with a small offset

GitHub scheduled workflows can be delayed during busy periods, so this is near-real-time rather than exact.

## Smoke Test

After the repository is on GitHub and secrets are configured:

1. Open **Actions -> OurDomain Telegram Watcher**.
2. Select **Run workflow**.
3. Set `send_test_message=true`.
4. Run the workflow.

You should receive:

```text
✅ OurDomain GitHub Actions Telegram test works.
```

## Manual GitHub Run

After the smoke test, run the same workflow again with `send_test_message=false`. The first watcher run initializes `ourdomain_state.json` without sending all current alert-worthy statuses unless `SEND_EXISTING_ON_FIRST_RUN=true`.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Edit `.env` and set your local Telegram values:

```text
TELEGRAM_BOT_TOKEN=<your BotFather token>
TELEGRAM_CHAT_ID=<your chat id>
```

Run continuously:

```bash
python ourdomain_telegram_bot.py
```

Run once, matching GitHub Actions:

```bash
python ourdomain_github_once.py
```

## State Persistence

`ourdomain_state.json` stores the last observed button text, status, alert signature, check time, and context per floor plan. It contains no Telegram secrets and is intentionally tracked by git.

GitHub Actions commits this file after each run when it changes:

```text
chore: update OurDomain state [skip ci]
```

This lets the next scheduled run avoid duplicate alerts for unchanged statuses.
