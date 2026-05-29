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
PAGE_READY_TIMEOUT_MS = 30000
PAGE_READY_POLL_MS = 500
TARGET_READY_TIMEOUT_MS = 10000
TARGET_READY_POLL_MS = 250

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
    "CHECK AVAILABILITY",
    "AVAILABLE",
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
        "button.applyButton",
        "a.applyButton",
        "input.applyButton",
        "button.contactButton",
        "a.contactButton",
        "input.contactButton",
        "button.btn-primary",
        "a.btn-primary",
        "input.btn-primary",
        "button",
        "a",
        "[role='button']",
        "[onclick]",
        "input[type='button']",
        "input[type='submit']",
    ]
)


@dataclass
class PlanResult:
    plan_name: str
    button_text: str
    status: str
    mode: str
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


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


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


def upper_text(value: str) -> str:
    return normalize_space(value).upper()


def floor_plan_label_key(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\([^)]*\)", "", value)
    return normalize_key(value)


def text_matches_floor_plan(text: str, plan_name: str) -> bool:
    text_key = floor_plan_label_key(text)
    plan_key = normalize_key(plan_name)
    return text_key == plan_key


def make_plan_aliases(plan_name: str) -> list[str]:
    return [
        plan_name,
        f"{plan_name} (monthly income required)",
    ]


def plan_matches_text(plan_name: str, text: str) -> bool:
    haystack = upper_text(text)
    return any(upper_text(alias) in haystack for alias in make_plan_aliases(plan_name))


def normalize_button_text(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().upper()


def is_unavailable_action_text(text: str) -> bool:
    value = upper_text(text)
    return (
        value == "GET NOTIFIED"
        or "CONTACT FOR AVAILABILITY" in value
        or "NOT AVAILABLE" in value
        or "NO APARTMENTS AVAILABLE" in value
    )


def text_contains_unavailable_signal(text: str) -> bool:
    value = upper_text(text)
    return any(phrase in value for phrase in UNAVAILABLE_BUTTON_TEXTS)


def is_available_action_text(text: str) -> bool:
    value = upper_text(text)

    if is_unavailable_action_text(value) or text_contains_unavailable_signal(value):
        return False

    return (
        value == "CHECK AVAILABILITY"
        or "CHECK AVAILABILITY" in value
        or value == "AVAILABLE"
        or value == "AVAILABLE NOW"
        or value == "APPLY"
        or value == "APPLY NOW"
        or value == "SELECT"
        or value == "SELECT APARTMENT"
        or value == "START"
        or value == "START APPLICATION"
        or value == "RESERVE"
        or value == "RESERVE NOW"
        or value == "LEASE"
        or value == "LEASE NOW"
        or value == "CONTINUE"
        or value == "CONTINUE APPLICATION"
    )


def is_availability_text_signal(text: str) -> bool:
    value = upper_text(text)

    if is_unavailable_action_text(value) or text_contains_unavailable_signal(value):
        return False

    return "(AVAILABLE)" in value or "CHECK AVAILABILITY" in value


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


def classify_action_state(button_text: str, context: str, previous_button_text: str = "") -> tuple[str, bool]:
    normalized = normalize_button_text(button_text)

    if is_unavailable_action_text(button_text) or text_contains_unavailable_signal(context):
        return "unavailable", False

    if is_available_action_text(button_text) or is_availability_text_signal(context):
        return "available_like", True

    if not normalized:
        return "missing_button", False

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


def is_cloudflare_challenge(title: str, body_text: str) -> bool:
    text = "\n".join([title, body_text]).casefold()
    indicators = [
        "just a moment",
        "cloudflare",
        "checking your browser",
        "cf-challenge",
        "cf-mitigated",
        "challenge-platform",
        "verify you are human",
    ]
    return any(indicator in text for indicator in indicators)


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


def get_attr(handle: Any, name: str) -> str:
    try:
        return normalize_space(handle.get_attribute(name) or "")
    except PlaywrightError:
        return ""


def short_outer_html(handle: Any, limit: int = 1600) -> str:
    try:
        value = handle.evaluate("(el) => String(el.outerHTML || '')")
    except PlaywrightError:
        return ""
    return normalize_space(str(value))[:limit]


def get_tag_name(handle: Any) -> str:
    try:
        value = handle.evaluate("(el) => el.tagName ? el.tagName.toLowerCase() : ''")
    except PlaywrightError:
        return ""
    return normalize_space(str(value))


def get_floor_plan_tab_infos(page: Any) -> list[dict[str, Any]]:
    tab_infos: list[dict[str, Any]] = []
    seen_plans: set[str] = set()

    preferred = page.locator('a[id^="ui-tab-"]').element_handles()
    source = preferred

    if not source:
        source = page.locator("a, button, [role='tab'], [role='button'], li, div, span").element_handles()

    for handle in source:
        if not is_visible(handle):
            continue
        text = safe_text(handle)
        for plan in TARGET_FLOOR_PLANS:
            if plan in seen_plans:
                continue
            if text_matches_floor_plan(text, plan):
                seen_plans.add(plan)
                tab_infos.append(
                    {
                        "handle": handle,
                        "index": len(tab_infos),
                        "text": normalize_space(text),
                        "plan_name": plan,
                    }
                )
                break

    return tab_infos


def find_floor_plan_tabs(page: Any) -> dict[str, Any]:
    tabs: dict[str, Any] = {}

    for info in get_floor_plan_tab_infos(page):
        tabs.setdefault(str(info["plan_name"]), info["handle"])

    return tabs


def find_tab_for_plan(page: Any, plan_name: str) -> Optional[dict[str, Any]]:
    tabs = get_floor_plan_tab_infos(page)
    for info in tabs:
        if info["plan_name"] == plan_name:
            info["tabs"] = tabs
            return info
    return None


def click_floor_plan(handle: Any, plan_name: str) -> bool:
    try:
        handle.scroll_into_view_if_needed(timeout=5000)
        handle.click(timeout=10000)
        return True
    except PlaywrightError as exc:
        print(f"[ourdomain] Could not click floor plan {plan_name!r}: {exc!r}", file=sys.stderr, flush=True)
        return False


def get_visible_floor_plan_panels(page: Any) -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = []

    for handle in page.locator('div[id^="FP_Detail_"]').element_handles():
        if not is_visible(handle):
            continue

        try:
            panel_id = handle.get_attribute("id") or ""
        except PlaywrightError:
            panel_id = ""

        try:
            class_name = handle.get_attribute("class") or ""
        except PlaywrightError:
            class_name = ""

        text = safe_text(handle)
        panels.append(
            {
                "handle": handle,
                "id": panel_id,
                "class_name": class_name,
                "text": normalize_space(text),
            }
        )

    return panels


def find_active_panel_for_plan(page: Any, plan_name: str) -> Optional[dict[str, Any]]:
    panels = get_visible_floor_plan_panels(page)
    if not panels:
        return None

    active_panels = [
        panel
        for panel in panels
        if "active" in str(panel.get("class_name", "")).split()
    ]
    source = active_panels or panels

    for panel in source:
        if plan_matches_text(plan_name, str(panel.get("text", ""))):
            return panel

    if len(source) == 1:
        return source[0]

    useful = [
        panel
        for panel in source
        if (
            is_availability_text_signal(str(panel.get("text", "")))
            or is_unavailable_action_text(str(panel.get("text", "")))
            or is_available_action_text(str(panel.get("text", "")))
        )
    ]
    useful.sort(key=lambda panel: len(str(panel.get("text", ""))))
    return useful[0] if useful else source[0]


def button_looks_tied_to_plan(plan_name: str, button: dict[str, Any]) -> bool:
    if not button:
        return False

    handle = button.get("handle")
    haystack_parts = [
        str(button.get("text", "")),
        str(button.get("id", "")),
        str(button.get("class_name", "")),
        str(button.get("tag", "")),
        str(button.get("name", "")),
        str(button.get("value", "")),
        str(button.get("aria_label", "")),
        str(button.get("data_selenium_id", "")),
        str(button.get("onclick", "")),
        str(button.get("html", "")),
    ]

    if handle is not None:
        haystack_parts.extend(
            [
                get_attr(handle, "id"),
                get_attr(handle, "name"),
                get_attr(handle, "value"),
                get_attr(handle, "aria-label"),
                get_attr(handle, "data-selenium-id"),
                get_attr(handle, "onclick"),
                short_outer_html(handle),
            ]
        )

    return plan_matches_text(plan_name, " ".join(haystack_parts))


def find_visible_action_buttons(container_handle: Any) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, handle in enumerate(container_handle.query_selector_all(ACTION_BUTTON_SELECTOR)):
        if not is_visible(handle):
            continue

        try:
            box = handle.bounding_box()
        except PlaywrightError:
            box = None

        text = safe_text(handle)
        if normalize_button_text(text) in NON_ACTION_BUTTON_TEXTS:
            continue

        text = normalize_space(text)
        if not text or len(text) > 140:
            continue

        class_name = get_attr(handle, "class")
        tag = get_tag_name(handle)
        ident = get_attr(handle, "id")
        name = get_attr(handle, "name")
        value = get_attr(handle, "value")
        aria_label = get_attr(handle, "aria-label")
        data_selenium_id = get_attr(handle, "data-selenium-id")
        onclick = get_attr(handle, "onclick")

        marker = f"{normalize_button_text(text)}|{box}"
        if marker in seen:
            continue
        seen.add(marker)
        buttons.append(
            {
                "handle": handle,
                "index": index,
                "text": text,
                "class_name": class_name,
                "id": ident,
                "tag": tag,
                "name": name,
                "value": value,
                "aria_label": aria_label,
                "data_selenium_id": data_selenium_id,
                "onclick": onclick,
                "html": short_outer_html(handle),
            }
        )

    return buttons


def get_real_action_buttons_whole_page(page: Any) -> list[dict[str, Any]]:
    try:
        body = page.locator("body").element_handle(timeout=2000)
    except PlaywrightError:
        body = None

    if body is None:
        return []

    return find_visible_action_buttons(body)


def get_text_fallback_for_plan(page: Any, plan_name: str) -> Optional[dict[str, Any]]:
    panel = find_active_panel_for_plan(page, plan_name)

    if panel and is_availability_text_signal(str(panel.get("text", ""))) and plan_matches_text(plan_name, str(panel.get("text", ""))):
        return {
            "panel": panel,
            "text": str(panel.get("text", "")),
        }

    candidates: list[dict[str, Any]] = []
    for handle in page.locator("div, section, article, li, tr, td, span, table, tbody").element_handles():
        if not is_visible(handle):
            continue

        text = safe_text(handle)
        if not text or len(text) > 2500:
            continue

        upper = upper_text(text)
        has_useful_details = (
            "BED" in upper
            or "BATH" in upper
            or "RENT" in upper
            or "CHECK AVAILABILITY" in upper
        )

        if plan_matches_text(plan_name, text) and is_availability_text_signal(text) and has_useful_details:
            candidates.append(
                {
                    "handle": handle,
                    "id": get_attr(handle, "id"),
                    "class_name": get_attr(handle, "class"),
                    "text": normalize_space(text),
                }
            )

    candidates.sort(key=lambda item: len(str(item.get("text", ""))))
    if not candidates:
        return None

    return {
        "panel": candidates[0],
        "text": str(candidates[0].get("text", "")),
    }


def wait_until_target_ready(page: Any, plan_name: str, timeout_ms: int = TARGET_READY_TIMEOUT_MS) -> tuple[bool, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_panel: Optional[dict[str, Any]] = None
    last_button: Optional[dict[str, Any]] = None

    while time.monotonic() < deadline:
        panel = find_active_panel_for_plan(page, plan_name)
        last_panel = panel

        if panel and plan_matches_text(plan_name, str(panel.get("text", ""))):
            return True, panel, None

        buttons = get_real_action_buttons_whole_page(page)
        button = next((item for item in buttons if button_looks_tied_to_plan(plan_name, item)), None)
        last_button = button

        if button:
            return True, panel, button

        page.wait_for_timeout(TARGET_READY_POLL_MS)

    return False, last_panel, last_button


def result_from_button(
    plan_name: str,
    button: dict[str, Any],
    panel: Optional[dict[str, Any]],
    mode: str,
    previous: dict[str, Any],
    checked_at: str,
) -> PlanResult:
    button_text = normalize_space(str(button.get("text", "")))
    panel_text = str(panel.get("text", "")) if panel else ""
    context = compact_context(panel_text or " ".join([
        str(button.get("text", "")),
        str(button.get("id", "")),
        str(button.get("class_name", "")),
        str(button.get("onclick", "")),
    ]))
    previous_button_text = str(previous.get("last_button_text", ""))
    status, alert_worthy = classify_action_state(button_text, context, previous_button_text)
    signature = make_alert_signature(plan_name, status, button_text, context)

    return PlanResult(
        plan_name=plan_name,
        button_text=button_text,
        status=status,
        mode=mode,
        alert_worthy=alert_worthy,
        alert_signature=signature,
        context=context,
        checked_at=checked_at,
    )


def inspect_plan(page: Any, plan_name: str, tab_handle: Any, previous: dict[str, Any], checked_at: str) -> PlanResult:
    clicked = click_floor_plan(tab_handle, plan_name)
    if clicked:
        page.wait_for_timeout(1800)

    target_ready, ready_panel, ready_button = wait_until_target_ready(page, plan_name)
    tab_info = find_tab_for_plan(page, plan_name)
    tabs = tab_info.get("tabs", []) if tab_info else []
    tab_index = int(tab_info.get("index", -1)) if tab_info else -1

    panel = find_active_panel_for_plan(page, plan_name)
    panel_matches_target = bool(panel and plan_matches_text(plan_name, str(panel.get("text", ""))))

    if target_ready and ready_button and not panel_matches_target:
        print(f"[ourdomain] {plan_name}: using MOBILE_OR_ID_MATCHED_BUTTON", flush=True)
        return result_from_button(
            plan_name,
            ready_button,
            panel,
            "MOBILE_OR_ID_MATCHED_BUTTON",
            previous,
            checked_at,
        )

    if panel_matches_target and panel:
        panel_handle = panel["handle"]
        panel_text = str(panel.get("text", ""))
        panel_buttons = find_visible_action_buttons(panel_handle)
        print(
            f"[ourdomain] {plan_name}: panel={panel.get('id', '')!r} "
            f"found {len(panel_buttons)} visible action/contact button(s).",
            flush=True,
        )

        if panel_buttons:
            button = (
                next((item for item in panel_buttons if is_available_action_text(str(item.get("text", "")))), None)
                or next((item for item in panel_buttons if upper_text(str(item.get("text", ""))) == "GET NOTIFIED"), None)
                or panel_buttons[0]
            )
            return result_from_button(
                plan_name,
                button,
                panel,
                "ACTIVE_PANEL_BUTTON",
                previous,
                checked_at,
            )

        if is_availability_text_signal(panel_text):
            button = {
                "handle": None,
                "index": -1,
                "text": "CHECK AVAILABILITY",
                "id": "",
                "class_name": "text-fallback",
                "tag": "text",
            }
            return result_from_button(
                plan_name,
                button,
                panel,
                "ACTIVE_PANEL_TEXT_AVAILABLE_FALLBACK",
                previous,
                checked_at,
            )

    buttons = get_real_action_buttons_whole_page(page)
    id_matched_button = next((button for button in buttons if button_looks_tied_to_plan(plan_name, button)), None)
    if id_matched_button:
        print(f"[ourdomain] {plan_name}: using MOBILE_OR_ID_MATCHED_BUTTON", flush=True)
        return result_from_button(
            plan_name,
            id_matched_button,
            panel,
            "MOBILE_OR_ID_MATCHED_BUTTON",
            previous,
            checked_at,
        )

    if tabs and len(buttons) == len(tabs) and 0 <= tab_index < len(buttons):
        print(f"[ourdomain] {plan_name}: using ORDER_MATCHED_BUTTONS_NO_PANEL", flush=True)
        return result_from_button(
            plan_name,
            buttons[tab_index],
            panel,
            "ORDER_MATCHED_BUTTONS_NO_PANEL",
            previous,
            checked_at,
        )

    if len(buttons) == 1:
        print(f"[ourdomain] {plan_name}: using SINGLE_VISIBLE_BUTTON_AFTER_CLICK", flush=True)
        return result_from_button(
            plan_name,
            buttons[0],
            panel,
            "SINGLE_VISIBLE_BUTTON_AFTER_CLICK",
            previous,
            checked_at,
        )

    fallback = get_text_fallback_for_plan(page, plan_name)
    if fallback and fallback.get("panel") and plan_matches_text(plan_name, str(fallback.get("text", ""))):
        button = {
            "handle": None,
            "index": -1,
            "text": "CHECK AVAILABILITY",
            "id": "",
            "class_name": "text-fallback",
            "tag": "text",
        }
        print(f"[ourdomain] {plan_name}: using TEXT_AVAILABLE_FALLBACK", flush=True)
        return result_from_button(
            plan_name,
            button,
            fallback["panel"],
            "TEXT_AVAILABLE_FALLBACK",
            previous,
            checked_at,
        )

    context = compact_context(str(panel.get("text", ""))) if panel else ""
    reason = "TARGET_NOT_READY" if not target_ready else f"UNSAFE_BUTTON_COUNT tabs={len(tabs)}, buttons={len(buttons)}"
    status = "mapping_problem"
    signature = make_alert_signature(plan_name, status, reason, context)
    print(f"[ourdomain] {plan_name}: mapping_problem {reason}", flush=True)

    return PlanResult(
        plan_name=plan_name,
        button_text=reason,
        status=status,
        mode="MAPPING_PROBLEM",
        alert_worthy=False,
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
        f"<b>Mode:</b> {html.escape(result.mode)}",
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
    problem: str = "",
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
    if problem:
        parts.insert(2, f"<b>Problem:</b> {html.escape(problem)}")
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
        "last_mode": result.mode,
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


def wait_for_floor_plan_tabs_to_load(page: Any, timeout_ms: int = PAGE_READY_TIMEOUT_MS) -> tuple[bool, dict[str, Any], int]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    tabs: dict[str, Any] = {}
    buttons_count = 0

    while time.monotonic() < deadline:
        tabs = find_floor_plan_tabs(page)
        buttons_count = len(get_real_action_buttons_whole_page(page))

        if len(tabs) >= len(TARGET_FLOOR_PLANS):
            return True, tabs, buttons_count

        page.wait_for_timeout(PAGE_READY_POLL_MS)

    tabs = find_floor_plan_tabs(page)
    buttons_count = len(get_real_action_buttons_whole_page(page))
    return False, tabs, buttons_count


def send_heartbeat_if_due(
    state: dict[str, Any],
    token: str,
    chat_id: str,
    checked_at: str,
    send_heartbeat: bool,
    heartbeat_interval_minutes: int,
    found_plans: int,
    alert_worthy: int,
    alerts_sent: int,
    url: str,
    problem: str = "",
) -> int:
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

    if not should_send_heartbeat:
        return 0

    send_telegram_message(
        token,
        chat_id,
        format_heartbeat_message(
            checked_at=checked_at,
            checks_since_last=checks_since_last,
            total_checks=total_checks,
            found_plans=found_plans,
            alert_worthy=alert_worthy,
            alerts_sent=alerts_sent,
            url=url,
            problem=problem,
        ),
    )
    heartbeat["last_sent_at"] = checked_at
    heartbeat["checks_since_last"] = 0
    return 1


def check_once(
    token: str = "",
    chat_id: str = "",
    url: Optional[str] = None,
    state_path: Optional[str] = None,
) -> tuple[int, int, int]:
    url = (url or os.getenv("OURDOMAIN_URL", DEFAULT_OURDOMAIN_URL)).strip() or DEFAULT_OURDOMAIN_URL
    fallback_url = env_str("OURDOMAIN_FALLBACK_URL", "")
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
    print(f"[ourdomain] fallback_url={fallback_url or '(none)'}", flush=True)
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
            urls_to_try = [url]
            if fallback_url and fallback_url != url:
                urls_to_try.append(fallback_url)

            tabs: dict[str, Any] = {}
            used_url = url
            page_problem = ""
            cloudflare_challenge = False

            for index, candidate_url in enumerate(urls_to_try):
                used_url = candidate_url
                print(f"[ourdomain] loading_url={candidate_url}", flush=True)
                page.goto(candidate_url, wait_until="domcontentloaded", timeout=60000)

                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PlaywrightTimeoutError:
                    print("[ourdomain] Timed out waiting for network idle; continuing.", flush=True)

                title = ""
                try:
                    title = page.title()
                except PlaywrightError:
                    pass

                body_text = ""
                try:
                    body_text = page.locator("body").inner_text(timeout=5000)
                except PlaywrightError:
                    body_text = ""

                cloudflare_challenge = is_cloudflare_challenge(title, body_text)
                print(f"[ourdomain] cloudflare_challenge={cloudflare_challenge}", flush=True)

                if cloudflare_challenge:
                    page_problem = "Cloudflare challenge detected; previous state preserved and no availability decision was made."
                    if index + 1 < len(urls_to_try):
                        print("[ourdomain] primary had Cloudflare/page challenge; trying official fallback URL.", flush=True)
                        continue
                    break

                ready, tabs, buttons_count = wait_for_floor_plan_tabs_to_load(page)
                print(
                    f"[ourdomain] readiness_ok={ready} tabs_found={len(tabs)}/{len(TARGET_FLOOR_PLANS)} "
                    f"visible_action_buttons={buttons_count}",
                    flush=True,
                )

                if len(tabs) == 0 and index + 1 < len(urls_to_try):
                    page_problem = "No floor-plan tabs found on primary URL; trying official fallback URL."
                    print(f"[ourdomain] {page_problem}", flush=True)
                    continue

                if len(tabs) < len(TARGET_FLOOR_PLANS):
                    page_problem = (
                        f"Page did not expose all floor-plan tabs "
                        f"({len(tabs)}/{len(TARGET_FLOOR_PLANS)}); previous state preserved."
                    )
                    if index + 1 < len(urls_to_try):
                        print(f"[ourdomain] {page_problem} Trying fallback URL.", flush=True)
                        continue
                    break

                page_problem = ""
                break

            if cloudflare_challenge or page_problem or len(tabs) < len(TARGET_FLOOR_PLANS):
                sent += send_heartbeat_if_due(
                    state=state,
                    token=token,
                    chat_id=chat_id,
                    checked_at=checked_at,
                    send_heartbeat=send_heartbeat,
                    heartbeat_interval_minutes=heartbeat_interval_minutes,
                    found_plans=len(tabs),
                    alert_worthy=0,
                    alerts_sent=sent,
                    url=used_url,
                    problem=page_problem or "Temporary page-read failure; previous state preserved and no availability decision was made.",
                )

                state["last_checked_at"] = checked_at
                state["url"] = used_url
                save_state(state_path, state)
                print(
                    f"[ourdomain] done page_problem={page_problem!r} cloudflare_challenge={cloudflare_challenge} "
                    f"plans_found={len(tabs)} alert_worthy=0 alerts_sent={sent}",
                    flush=True,
                )
                return len(tabs), 0, sent

            print(f"[ourdomain] found {len(tabs)} target floor plan tab(s).", flush=True)

            for plan_name in TARGET_FLOOR_PLANS:
                previous = plans_state.get(plan_name, {})
                tab = tabs.get(plan_name)

                if not tab:
                    result = PlanResult(
                        plan_name=plan_name,
                        button_text="",
                        status="missing_tab",
                        mode="TAB_NOT_FOUND",
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
                    send_telegram_message(token, chat_id, format_telegram_message(result, used_url))
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
                        url=used_url,
                    ),
                )
                sent += 1
                heartbeat["last_sent_at"] = checked_at
                heartbeat["checks_since_last"] = 0

            state["last_checked_at"] = checked_at
            state["url"] = used_url
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
