#!/usr/bin/env python3
"""Export Close SMS threads for golden-set mining.

Read-only. Uses CLOSE_API_KEY from mcp-servers/close-gateway/.env and never
prints it. Searches outbound SMS for the booking template phrase, then emits
inbound SMS messages from the small context window around each matching send.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "mcp-servers" / "close-gateway" / ".env"
CLOSE_BASE = "https://api.close.com/api/v1"

PHRASES = [
    "I'd love to connect you with one of our coaches jumped on a call with you.",
    "I'd love to connect you with one of our coaches",
    "connect you with one of our coaches",
]

THREAD_WINDOW = 6
PAGE_LIMIT = 100
SLEEP_S = 0.15
MAX_SCAN_PAGES = 75

MANUAL_INTENT_OVERRIDES = {
    "none of those times are acceptable": "rescheduling",
    "i like wednesday at 2:00": "scheduling",
    "i can't do this time": "rescheduling",
    "not interested": "opt_out",
}


class CloseExportError(RuntimeError):
    pass


def read_close_api_key(path: Path = ENV_PATH) -> str:
    if not path.exists():
        raise CloseExportError(f"env file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "CLOSE_API_KEY":
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    raise CloseExportError(f"CLOSE_API_KEY not found in {path}")


def parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def contains_phrase(text: str | None, phrase: str) -> bool:
    return phrase.lower() in (text or "").lower()


def sms_time(sms: dict[str, Any]) -> str:
    return (
        sms.get("date_sent")
        or sms.get("activity_at")
        or sms.get("date_created")
        or sms.get("date_updated")
        or ""
    )


def sms_body(sms: dict[str, Any]) -> str:
    return sms.get("text") or sms.get("body") or ""


def sms_direction(sms: dict[str, Any]) -> str:
    return str(sms.get("direction") or "").lower()


def lead_phone(sms: dict[str, Any]) -> str:
    return sms.get("remote_phone") or sms.get("sender") or ""


def suggest_intent(body: str) -> str:
    """Lightweight labeling suggestion for mined candidates.

    This is intentionally conservative and reviewable. It is not ground truth;
    the human labeling pass can replace ``true_intent_suggestion`` with
    ``true_intent`` for eval.
    """
    text = " ".join((body or "").lower().split())
    if not text:
        return "other"
    if text in MANUAL_INTENT_OVERRIDES:
        return MANUAL_INTENT_OVERRIDES[text]

    opt_out = (
        "stop texting", "stop messaging", "unsubscribe", "take me off",
        "remove me", "not interested", "no longer interested",
        "don't text", "do not text",
    )
    if text in {"stop", "unsubscribe", "quit", "end"} or any(p in text for p in opt_out):
        return "opt_out"

    refund = ("refund", "money back", "reimburs", "reverse the charge", "charged twice", "chargeback")
    if any(p in text for p in refund):
        return "refund"

    # Appointment/call cancellation is rescheduling; program/subscription
    # cancellation is cancellation.
    reschedule = (
        "reschedule", "re-schedule", "move my", "move the", "push back",
        "change the time", "change my time", "can't do this time",
        "cant do this time", "can't make", "cannot make", "won't be able to talk",
        "will not be able to talk", "none of those times", "times don't work",
        "times dont work", "something came up", "family emergency",
        "cancel my appointment", "cancel the appointment", "cancel my call",
        "cancel the call",
    )
    if any(p in text for p in reschedule):
        return "rescheduling"

    cancellation = (
        "cancel my membership", "cancel my subscription", "cancel my program",
        "cancel my programme", "cancel my plan", "cancel my account",
        "cancel participation", "can't join", "cannot join", "quit the program",
    )
    if any(p in text for p in cancellation):
        return "cancellation"

    technical = (
        "link doesn't work", "link does not work", "link isn't working",
        "broken link", "page won't load", "page will not load", "can't log",
        "cannot log", "login", "password", "access my", "watch on my tv",
        "watch it on my tv", "error message",
    )
    if any(p in text for p in technical):
        return "technical"

    scheduling = (
        "book", "schedule", "appointment", "consultation", "talk to a coach",
        "speak to a coach", "call with", "set up a time", "available",
        "availability",
    )
    weekday = any(day in text for day in (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ))
    has_time = any(marker in text for marker in (":00", ":30", " am", " pm", "morning", "afternoon", "evening"))
    if any(p in text for p in scheduling) or (weekday and has_time):
        return "scheduling"

    coaching = (
        "meal plan", "nutrition", "workout", "protein", "calories", "coach",
        "superfoods", "diet", "supplement", "pre-workout", "weigh",
        "weight", "hormonal profile",
    )
    if any(p in text for p in coaching):
        return "coaching_checkin"

    return "other"


def polite_sleep() -> None:
    if SLEEP_S:
        time.sleep(SLEEP_S)


class CloseClient:
    def __init__(self, api_key: str):
        self._client = httpx.Client(
            base_url=CLOSE_BASE,
            auth=(api_key, ""),
            timeout=15.0,
            headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        delay = 1.0
        for attempt in range(6):
            try:
                resp = self._client.request(method, path, params=params, json=json_body)
            except httpx.HTTPError as exc:
                if attempt == 5:
                    raise CloseExportError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(wait)
                delay *= 2
                continue
            if resp.status_code >= 400:
                snippet = resp.text[:300].replace("\n", " ")
                raise CloseExportError(f"{method} {path} HTTP {resp.status_code}: {snippet}")
            return resp.json()
        raise CloseExportError(f"{method} {path} exhausted retries")


def data_search_payload(phrase: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
    object_type = "activity.sms"
    payload: dict[str, Any] = {
        "query": {
            "type": "and",
            "queries": [
                {"type": "object_type", "object_type": object_type},
                {
                    "type": "field_condition",
                    "field": {
                        "type": "regular_field",
                        "object_type": object_type,
                        "field_name": "direction",
                    },
                    "condition": {"type": "term", "values": ["outbound"]},
                },
                {
                    "type": "field_condition",
                    "field": {
                        "type": "regular_field",
                        "object_type": object_type,
                        "field_name": "text",
                    },
                    "condition": {"type": "text", "mode": "phrase", "value": phrase},
                },
            ],
        },
        "_fields": {
            object_type: [
                "id",
                "lead_id",
                "contact_id",
                "direction",
                "text",
                "remote_phone",
                "local_phone",
                "date_created",
                "date_sent",
                "activity_at",
                "status",
            ]
        },
        "_limit": limit,
    }
    if cursor:
        payload["cursor"] = cursor
    return payload


def search_outbound_sms(client: CloseClient, phrase: str, needed: int) -> tuple[list[dict[str, Any]], bool]:
    """Return matching outbound SMS and whether data_search was usable."""
    matches: list[dict[str, Any]] = []
    cursor = None
    usable = True
    while len(matches) < needed:
        page_limit = min(PAGE_LIMIT, needed - len(matches))
        try:
            data = client.request(
                "POST",
                "/data/search/",
                json_body=data_search_payload(phrase, page_limit, cursor),
            )
        except CloseExportError as exc:
            print(f"data_search unavailable for phrase {phrase!r}: {exc}", file=sys.stderr)
            usable = False
            break
        rows = data.get("data") or []
        if rows and not matches:
            print("data_search sample fields:", ", ".join(sorted(rows[0].keys())))
        matches.extend([r for r in rows if sms_direction(r) == "outbound" and contains_phrase(sms_body(r), phrase)])
        cursor = data.get("cursor")
        if not cursor or not rows:
            break
        polite_sleep()
    return matches, usable


def list_outbound_sms_scan(client: CloseClient, phrase: str, needed: int, offset: int) -> list[dict[str, Any]]:
    """Fallback: scan outbound SMS pages and filter text client-side."""
    matches: list[dict[str, Any]] = []
    skipped_matches = 0
    pages = 0
    before: str | None = None
    while len(matches) < needed and pages < MAX_SCAN_PAGES:
        params = {
            "_limit": PAGE_LIMIT,
            "_order_by": "-date_created",
            "direction": "outbound",
        }
        if before:
            params["date_created__lt"] = before
        data = client.request("GET", "/activity/sms/", params=params)
        rows = data.get("data") or []
        if rows and pages == 0:
            print("activity scan sample fields:", ", ".join(sorted(rows[0].keys())))
        if not rows:
            break
        for sms in rows:
            if sms_direction(sms) != "outbound" or not contains_phrase(sms_body(sms), phrase):
                continue
            if skipped_matches < offset:
                skipped_matches += 1
                continue
            matches.append(sms)
            if len(matches) >= needed:
                break
        before = rows[-1].get("date_created") or before
        pages += 1
        if pages % 10 == 0:
            print(f"Scanned {pages} outbound SMS pages; matched {len(matches)}")
        if data.get("has_more") is False:
            break
        polite_sleep()
    return matches


def fetch_lead_thread(client: CloseClient, lead_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    skip = 0
    while True:
        data = client.request(
            "GET",
            "/activity/sms/",
            params={
                "lead_id": lead_id,
                "_limit": PAGE_LIMIT,
                "_skip": skip,
                "_order_by": "date_created",
            },
        )
        page = data.get("data") or []
        rows.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        skip += len(page)
        polite_sleep()
    return sorted(rows, key=lambda row: parse_ts(sms_time(row)))


def context_bounds(thread: list[dict[str, Any]], outbound_idx: int) -> tuple[int, int]:
    start = max(0, outbound_idx - (THREAD_WINDOW // 2))
    end = min(len(thread), start + THREAD_WINDOW)
    start = max(0, end - THREAD_WINDOW)
    return start, end


def around_send_context(thread: list[dict[str, Any]], outbound_idx: int) -> list[dict[str, str]]:
    start, end = context_bounds(thread, outbound_idx)
    out = []
    for sms in thread[start:end]:
        out.append(
            {
                "direction": "inbound" if sms_direction(sms) == "inbound" else "outbound",
                "body": sms_body(sms),
                "timestamp": sms_time(sms),
            }
        )
    return out


def build_records(client: CloseClient, outbound: dict[str, Any]) -> list[dict[str, Any]]:
    lead_id = outbound.get("lead_id")
    outbound_id = outbound.get("id")
    if not lead_id or not outbound_id:
        return []

    thread = fetch_lead_thread(client, lead_id)
    outbound_idx = next((i for i, sms in enumerate(thread) if sms.get("id") == outbound_id), None)
    if outbound_idx is None:
        return []

    outbound_dt = parse_ts(sms_time(thread[outbound_idx]))
    prior_inbound = None
    for sms in reversed(thread[:outbound_idx]):
        if sms_direction(sms) == "inbound" and parse_ts(sms_time(sms)) <= outbound_dt:
            prior_inbound = sms
            break
    if prior_inbound is None:
        return []

    context = around_send_context(thread, outbound_idx)
    start, end = context_bounds(thread, outbound_idx)
    records: list[dict[str, Any]] = []
    for sms in thread[start:end]:
        if sms_direction(sms) != "inbound":
            continue
        body = sms_body(sms)
        sms_dt = parse_ts(sms_time(sms))
        records.append(
            {
                "message_id": sms.get("id") or "",
                "channel": "sms",
                "source": "close",
                "sender": lead_phone(sms),
                "body": body,
                "timestamp": sms_time(sms),
                "thread_context": context,
                "setter_sent_link_after": sms_dt <= outbound_dt,
                "notes": "",
                "true_intent_suggestion": suggest_intent(body),
            }
        )
    return records


def mine_records(client: CloseClient, limit: int, offset: int) -> tuple[list[dict[str, Any]], str]:
    needed = limit + offset
    data_search_failed = False
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    phrases_used: list[str] = []
    for phrase in PHRASES:
        print(f"Searching outbound SMS for phrase: {phrase!r}")
        matches, usable = search_outbound_sms(client, phrase, needed)
        data_search_failed = data_search_failed or not usable
        if matches:
            phrases_used.append(phrase)
            for sms in matches:
                sms_id = sms.get("id")
                if sms_id and sms_id not in seen_ids:
                    selected.append(sms)
                    seen_ids.add(sms_id)
            if len(selected) >= needed:
                return selected[offset : offset + limit], " | ".join(phrases_used)
        print(f"No data_search hits for phrase: {phrase!r}")

    print("Falling back to outbound SMS scan.", file=sys.stderr)
    selected = []
    seen_ids = set()
    phrases_used = []
    for phrase in PHRASES:
        print(f"Scanning outbound SMS for phrase: {phrase!r}")
        matches = list_outbound_sms_scan(client, phrase, needed, 0)
        if matches:
            phrases_used.append(phrase)
            for sms in matches:
                sms_id = sms.get("id")
                if sms_id and sms_id not in seen_ids:
                    selected.append(sms)
                    seen_ids.add(sms_id)
            if len(selected) >= needed:
                return selected[offset : offset + limit], " | ".join(phrases_used)
    if selected:
        return selected[offset : offset + limit], " | ".join(phrases_used)

    if data_search_failed:
        print("No matches found via data_search or scan.", file=sys.stderr)
    return [], PHRASES[-1]


def export(limit: int, offset: int, out_path: Path) -> list[dict[str, Any]]:
    api_key = read_close_api_key()
    client = CloseClient(api_key)
    records: list[dict[str, Any]] = []
    seen_message_ids: set[str] = set()
    try:
        outbound_matches, phrase = mine_records(client, limit, offset)
        print(f"Outbound matches selected: {len(outbound_matches)} using phrase {phrase!r}")
        for i, outbound in enumerate(outbound_matches, start=1):
            try:
                for record in build_records(client, outbound):
                    message_id = record.get("message_id")
                    if message_id and message_id not in seen_message_ids:
                        records.append(record)
                        seen_message_ids.add(message_id)
            except Exception as exc:  # noqa: BLE001
                outbound_id = outbound.get("id", "<unknown>")
                print(f"thread skipped {outbound_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            if i % 50 == 0:
                print(f"Processed {i} outbound sends; exported {len(records)} records")
            polite_sleep()
    finally:
        client.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump({"messages": records}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"Final exported records: {len(records)}")
    print(f"Wrote {out_path}")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--out", default=str(ROOT / "eval" / "fixtures" / "golden_mined.json"))
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.offset < 0:
        parser.error("--offset must be >= 0")

    try:
        export(args.limit, args.offset, Path(args.out))
    except CloseExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
