#!/usr/bin/env python3
"""
OurDomain / RentCafe floorplan watcher -> Telegram notifications.

The watcher only detects and reports visible availability-like action buttons.
It does not log in, fill forms, submit applications, or click into an
application flow.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_OURDOMAIN_URL = (
    "https://southeast-thisisourdomain.securerc.co.uk/onlineleasing/"
    "ourdomain-amsterdam-south-east/floorplans.aspx"
)

TARGET_FLOOR_PLANS = [
    "2 Bedroom Superior",
    "2 Bedroom",
    "1 Bedroom Superior",
    "1 Bedroom",
    "Studio Suite",
    "Executive Plus Studio",
    "Standard Studio",
    "Superior Studio",
    "Executive Studio",
    "Executive Plus Studio - Furnished",
]

UNAVAILABLE_BUTTON_TEXTS = {
    "GET NOTIFIED",
    "CONTACT FOR AVAILABILITY",
    "NOT AVAILABLE",
    "NO APARTMENTS AVAILABLE",
}

AVAILABLE_BUTTON_TEXTS = {
    "APPLY",
    "APPLY NOW",
    "SELECT",
    "SELECT APARTMENT",
    "START",
    "START APPLICATION",
    "RESERVE",
    "RESERVE NOW",
    "LEASE",
    "LEASE NOW",
    "CONTINUE",
    "CONTINUE APPLICATION",
    "AVAILABLE NOW",
}

NON_ACTION_BUTTON_TEXTS = {
    "GO",
    "SEARCH",
    "FILTER",
    "RESET",
}

ACTION_BUTTON_SELECTOR = ", ".join(
    [
        "button.contactButton",
        "a.contactButton",
        "button.btn-primary",
        "a.btn-primary",
        "input[type='button']",
        "input[type='submit']",
    ]
)


@dataclass
class PlanResult:
    plan_name: str
    button_text: str
    status: str
    alert_worthy: bool
    alert_signature: str
    context: str
    checked_at: str


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        print(f"Invalid integer for {name}: {value!r}. Using {default}.", file=sys.stderr)
        return default


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_space(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_space(value).casefold())


def floor_plan_label_key(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\([^)]*\)", "", value)
    return normalize_key(value)


def text_matches_floor_plan(text: str, plan_name: str) -> bool:
    text_key = floor_plan_label_key(text)
    plan_key = normalize_key(plan_name)
    return text_key == plan_key


def normalize_button_text(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().upper()


def compact_context(value: str, limit: int = 900) -> str:
    lines = [normalize_space(line) for line in value.splitlines()]
    lines = [line for line in lines if line]
    seen: set[str] = set()
    compacted: list[str] = []

    for line in lines:
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        compacted.append(line)

    text = "\n".join(compacted)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def classify_button_text(button_text: str, previous_button_text: str = "") -> tuple[str, bool]:
    normalized = normalize_button_text(button_text)

    if not normalized:
        return "missing_button", False

    if normalized in UNAVAILABLE_BUTTON_TEXTS:
        return "unavailable", False

    if normalized in AVAILABLE_BUTTON_TEXTS:
        return "available", True

    if previous_button_text and normalized != normalize_button_text(previous_button_text):
        return "unknown_changed", True

    if not previous_button_text:
        return "unknown_changed", True

    return "unknown", False


def make_alert_signature(plan_name: str, status: str, button_text: str, context: str) -> str:
    context_key = normalize_space(context).casefold()
    if len(context_key) > 500:
        context_key = context_key[:500]
    return "|".join([normalize_key(plan_name), status, normalize_button_text(button_text), context_key])


def load_state(path: str) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {"version": 1, "plans": {}}

    try:
        with state_path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read state file {path}: {exc!r}. Starting with empty state.", file=sys.stderr)
        return {"version": 1, "plans": {}}

    if not isinstance(state, dict):
        return {"version": 1, "plans": {}}
    if not isinstance(state.get("plans"), dict):
        state["plans"] = {}
    state.setdefault("version", 1)
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")

    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")

    tmp_path.replace(state_path)


def safe_text(handle: Any) -> str:
    candidates: list[str] = []

    try:
        candidates.append(handle.inner_text())
    except PlaywrightError:
        pass

    for attr in ("value", "aria-label", "title"):
        try:
            value = handle.get_attribute(attr)
        except PlaywrightError:
            value = None
        if value:
            candidates.append(value)

    for candidate in candidates:
        cleaned = normalize_space(candidate)
        if cleaned:
            return cleaned

    return ""


def is_visible(handle: Any) -> bool:
    try:
        return bool(handle.is_visible())
    except PlaywrightError:
        return False


def find_floor_plan_tabs(page: Any) -> dict[str, Any]:
    tabs: dict[str, Any] = {}

    preferred = page.locator('a[id^="ui-tab-"]').element_handles()
    for handle in preferred:
        if not is_visible(handle):
            continue
        text = safe_text(handle)
        for plan in TARGET_FLOOR_PLANS:
            if text_matches_floor_plan(text, plan):
                tabs.setdefault(plan, handle)

    missing = [plan for plan in TARGET_FLOOR_PLANS if plan not in tabs]
    if missing:
        candidates = page.locator("a, button, [role='tab'], [role='button'], li, div, span").element_handles()
        for handle in candidates:
            if not missing:
                break
            if not is_visible(handle):
                continue
            text = safe_text(handle)
            for plan in list(missing):
                if text_matches_floor_plan(text, plan):
                    tabs.setdefault(plan, handle)
                    missing.remove(plan)

    return tabs


def click_floor_plan(handle: Any, plan_name: str) -> bool:
    try:
        handle.scroll_into_view_if_needed(timeout=5000)
        handle.click(timeout=10000)
        return True
    except PlaywrightError as exc:
        print(f"[ourdomain] Could not click floor plan {plan_name!r}: {exc!r}", file=sys.stderr, flush=True)
        return False


def extract_button_context(button_handle: Any, page: Any) -> str:
    script = """
    (element) => {
      const selectors = [
        '.floorplan-card',
        '.floorPlanCard',
        '.floorplan',
        '.unit-container',
        '.card',
        '.apartment',
        '.fp-card',
        'li',
        'tr',
        'section',
        'article',
        'div'
      ];
      let current = element;
      while (current && current !== document.body) {
        if (current.matches && selectors.some((selector) => current.matches(selector))) {
          const text = (current.innerText || current.textContent || '').trim();
          if (text.length >= 20) {
            return text;
          }
        }
        current = current.parentElement;
      }
      return (document.body.innerText || document.body.textContent || '').trim();
    }
    """

    try:
        text = button_handle.evaluate(script)
    except PlaywrightError:
        try:
            text = page.locator("body").inner_text(timeout=2000)
        except PlaywrightError:
            text = ""

    return compact_context(text)


def find_visible_action_buttons(page: Any) -> list[Any]:
    buttons: list[Any] = []
    seen: set[str] = set()

    for handle in page.locator(ACTION_BUTTON_SELECTOR).element_handles():
        if not is_visible(handle):
            continue

        try:
            box = handle.bounding_box()
        except PlaywrightError:
            box = None

        text = safe_text(handle)
        if normalize_button_text(text) in NON_ACTION_BUTTON_TEXTS:
            continue
        marker = f"{normalize_button_text(text)}|{box}"
        if marker in seen:
            continue
        seen.add(marker)
        buttons.append(handle)

    return buttons


def choose_best_button(buttons: list[Any]) -> Optional[Any]:
    if not buttons:
        return None

    scored: list[tuple[int, Any]] = []
    for button in buttons:
        text = normalize_button_text(safe_text(button))
        if text in AVAILABLE_BUTTON_TEXTS:
            score = 0
        elif text not in UNAVAILABLE_BUTTON_TEXTS and text:
            score = 1
        elif text in UNAVAILABLE_BUTTON_TEXTS:
            score = 2
        else:
            score = 3
        scored.append((score, button))

    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def inspect_plan(page: Any, plan_name: str, tab_handle: Any, previous: dict[str, Any], checked_at: str) -> PlanResult:
    clicked = click_floor_plan(tab_handle, plan_name)
    if clicked:
        page.wait_for_timeout(1400)

    buttons = find_visible_action_buttons(page)
    print(f"[ourdomain] {plan_name}: found {len(buttons)} visible action/contact button(s).", flush=True)

    button = choose_best_button(buttons)
    button_text = safe_text(button) if button else ""
    context = extract_button_context(button, page) if button else ""
    previous_button_text = str(previous.get("last_button_text", ""))
    status, alert_worthy = classify_button_text(button_text, previous_button_text)
    signature = make_alert_signature(plan_name, status, button_text, context)

    return PlanResult(
        plan_name=plan_name,
        button_text=normalize_space(button_text),
        status=status,
        alert_worthy=alert_worthy,
        alert_signature=signature,
        context=context,
        checked_at=checked_at,
    )


def format_telegram_message(result: PlanResult, url: str) -> str:
    context = result.context or "No extra visible context found."
    if len(context) > 1200:
        context = context[:1199].rstrip() + "..."

    parts = [
        "🏠 <b>OurDomain availability change</b>",
        f"<b>Floor plan:</b> {html.escape(result.plan_name)}",
        f"<b>Status:</b> {html.escape(result.status)}",
        f"<b>Button text:</b> {html.escape(result.button_text or 'No visible button')}",
        f"<b>Context:</b>\n{html.escape(context)}",
        f"<b>URL:</b> {html.escape(url, quote=True)}",
        f"<b>Checked:</b> {html.escape(result.checked_at)}",
    ]
    return "\n".join(parts)


def format_heartbeat_message(
    checked_at: str,
    checks_since_last: int,
    total_checks: int,
    found_plans: int,
    alert_worthy: int,
    alerts_sent: int,
    url: str,
) -> str:
    parts = [
        "✅ <b>OurDomain watcher heartbeat</b>",
        f"<b>Checked:</b> {html.escape(checked_at)}",
        f"<b>Checks since last heartbeat:</b> {checks_since_last}",
        f"<b>Total successful checks:</b> {total_checks}",
        f"<b>Floor plans found:</b> {found_plans}/{len(TARGET_FLOOR_PLANS)}",
        f"<b>Alert-worthy states this run:</b> {alert_worthy}",
        f"<b>Availability alerts sent this run:</b> {alerts_sent}",
        f"<b>URL:</b> {html.escape(url, quote=True)}",
    ]
    return "\n".join(parts)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set; cannot send Telegram alert.")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set; cannot send Telegram alert.")

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(endpoint, json=payload, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Telegram sendMessage failed: {response.status_code} {response.text}")


def update_state_for_result(
    state: dict[str, Any],
    result: PlanResult,
    should_store_alert_signature: bool,
) -> None:
    state.setdefault("plans", {})
    state["plans"][result.plan_name] = {
        "last_button_text": result.button_text,
        "last_status": result.status,
        "last_alert_signature": result.alert_signature if should_store_alert_signature else "",
        "last_checked_at": result.checked_at,
        "last_seen_context": result.context,
    }


def update_heartbeat_counters(state: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    heartbeat = state.get("heartbeat")
    if not isinstance(heartbeat, dict):
        heartbeat = {}
        state["heartbeat"] = heartbeat

    total_checks = int(heartbeat.get("total_checks", 0) or 0) + 1
    checks_since_last = int(heartbeat.get("checks_since_last", 0) or 0) + 1
    heartbeat["total_checks"] = total_checks
    heartbeat["checks_since_last"] = checks_since_last
    return heartbeat, checks_since_last, total_checks


def heartbeat_is_due(heartbeat: dict[str, Any], checked_at: str, interval_minutes: int) -> bool:
    last_sent = parse_utc(str(heartbeat.get("last_sent_at", "")))
    checked = parse_utc(checked_at)

    if last_sent is None or checked is None:
        return True

    elapsed_seconds = (checked - last_sent).total_seconds()
    return elapsed_seconds >= interval_minutes * 60


def check_once(
    token: str = "",
    chat_id: str = "",
    url: Optional[str] = None,
    state_path: Optional[str] = None,
) -> tuple[int, int, int]:
    url = (url or os.getenv("OURDOMAIN_URL", DEFAULT_OURDOMAIN_URL)).strip() or DEFAULT_OURDOMAIN_URL
    state_path = (state_path or os.getenv("STATE_PATH", "ourdomain_state.json")).strip() or "ourdomain_state.json"
    state = load_state(state_path)
    plans_state = state.get("plans", {})
    is_first_run = not plans_state
    send_existing_on_first_run = env_bool("SEND_EXISTING_ON_FIRST_RUN", False)
    send_heartbeat = env_bool("SEND_HEARTBEAT", True)
    heartbeat_interval_minutes = max(1, env_int("HEARTBEAT_INTERVAL_MINUTES", 60))
    checked_at = now_utc()

    found_plans = 0
    alert_worthy = 0
    sent = 0

    headless = env_bool("HEADLESS", True)
    slow_mo = env_int("SLOW_MO_MS", 0)
    locale = os.getenv("BROWSER_LOCALE", "en-GB").strip() or "en-GB"

    print(f"[ourdomain] url={url}", flush=True)
    print(f"[ourdomain] state_path={state_path}", flush=True)
    print(f"[ourdomain] send_existing_on_first_run={send_existing_on_first_run}", flush=True)
    print(
        f"[ourdomain] send_heartbeat={send_heartbeat} "
        f"heartbeat_interval_minutes={heartbeat_interval_minutes}",
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        try:
            context = browser.new_context(
                locale=locale,
                viewport={"width": 1440, "height": 1400},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                print("[ourdomain] Timed out waiting for network idle; continuing.", flush=True)

            try:
                page.wait_for_selector(
                    f'a[id^="ui-tab-"], {ACTION_BUTTON_SELECTOR}',
                    timeout=45000,
                )
            except PlaywrightTimeoutError:
                print("[ourdomain] No tabs/buttons appeared before timeout; continuing with visible scan.", flush=True)

            title = ""
            try:
                title = page.title()
            except PlaywrightError:
                pass
            if "just a moment" in title.casefold():
                print("[ourdomain] Page title indicates a Cloudflare challenge.", flush=True)

            tabs = find_floor_plan_tabs(page)
            print(f"[ourdomain] found {len(tabs)} target floor plan tab(s).", flush=True)

            for plan_name in TARGET_FLOOR_PLANS:
                previous = plans_state.get(plan_name, {})
                tab = tabs.get(plan_name)

                if not tab:
                    result = PlanResult(
                        plan_name=plan_name,
                        button_text="",
                        status="missing_tab",
                        alert_worthy=False,
                        alert_signature=make_alert_signature(plan_name, "missing_tab", "", ""),
                        context="",
                        checked_at=checked_at,
                    )
                    print(f"[ourdomain] {plan_name}: missing_tab", flush=True)
                    update_state_for_result(state, result, bool(previous.get("last_alert_signature")))
                    continue

                found_plans += 1
                result = inspect_plan(page, plan_name, tab, previous, checked_at)
                if result.alert_worthy:
                    alert_worthy += 1

                last_signature = str(previous.get("last_alert_signature", ""))
                should_send = (
                    result.alert_worthy
                    and result.alert_signature != last_signature
                    and (not is_first_run or send_existing_on_first_run)
                )

                print(
                    f"[ourdomain] {plan_name}: status={result.status} "
                    f"button_text={result.button_text!r} alert_worthy={result.alert_worthy} "
                    f"send={should_send}",
                    flush=True,
                )

                if should_send:
                    send_telegram_message(token, chat_id, format_telegram_message(result, url))
                    sent += 1
                    time.sleep(1.0)

                store_signature = bool(result.alert_worthy)
                update_state_for_result(state, result, store_signature)

            heartbeat, checks_since_last, total_checks = update_heartbeat_counters(state)
            should_send_heartbeat = (
                send_heartbeat
                and heartbeat_is_due(heartbeat, checked_at, heartbeat_interval_minutes)
            )

            print(
                f"[ourdomain] heartbeat checks_since_last={checks_since_last} "
                f"total_checks={total_checks} send={should_send_heartbeat}",
                flush=True,
            )

            if should_send_heartbeat:
                send_telegram_message(
                    token,
                    chat_id,
                    format_heartbeat_message(
                        checked_at=checked_at,
                        checks_since_last=checks_since_last,
                        total_checks=total_checks,
                        found_plans=found_plans,
                        alert_worthy=alert_worthy,
                        alerts_sent=sent,
                        url=url,
                    ),
                )
                sent += 1
                heartbeat["last_sent_at"] = checked_at
                heartbeat["checks_since_last"] = 0

            state["last_checked_at"] = checked_at
            state["url"] = url
            save_state(state_path, state)
        finally:
            browser.close()

    print(
        f"[ourdomain] done plans_found={found_plans} alert_worthy={alert_worthy} alerts_sent={sent}",
        flush=True,
    )
    return found_plans, alert_worthy, sent


def main() -> int:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    interval = env_int("CHECK_INTERVAL_SECONDS", 60)

    if interval < 30:
        print("CHECK_INTERVAL_SECONDS is too low. Using 30 seconds minimum.", file=sys.stderr)
        interval = 30

    print(f"[ourdomain] local continuous mode interval={interval}s", flush=True)

    while True:
        try:
            check_once(token=token, chat_id=chat_id)
        except KeyboardInterrupt:
            print("Stopped.", flush=True)
            return 0
        except Exception as exc:
            print(f"[ourdomain] ERROR: {exc!r}", file=sys.stderr, flush=True)

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
