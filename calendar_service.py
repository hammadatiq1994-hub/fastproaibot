"""
Google Calendar availability + booking.

Uses OAuth2 user credentials (token.json produced by generate_token.py).
All datetimes are timezone-aware (config.TIMEZONE) and serialized as ISO-8601.

If Google credentials are missing, a local JSON fallback (bookings.json) is used
so the rest of the chatbot can still be developed and tested.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger("chatbot.calendar")


def _load_creds() -> Optional[Credentials]:
    if not config.google_ready():
        return None
    try:
        creds = Credentials.from_authorized_user_file(
            str(config.GOOGLE_TOKEN_FILE), config.GOOGLE_SCOPES
        )
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
            config.GOOGLE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return creds
    except Exception as exc:
        logger.error("Failed to load Google credentials: %s", exc)
        return None


def _calendar_service():
    creds = _load_creds()
    if creds is None:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_working_window() -> Tuple[time, time]:
    sh, sm = config.parse_hhmm(config.WORKING_HOURS_START)
    eh, em = config.parse_hhmm(config.WORKING_HOURS_END)
    return time(sh, sm), time(eh, em)


def _parse_lunch() -> Optional[Tuple[time, time]]:
    if not config.LUNCH_BREAK_START or not config.LUNCH_BREAK_END:
        return None
    try:
        sh, sm = config.parse_hhmm(config.LUNCH_BREAK_START)
        eh, em = config.parse_hhmm(config.LUNCH_BREAK_END)
        return time(sh, sm), time(eh, em)
    except ValueError:
        return None


def _iter_candidate_slots(tz: ZoneInfo) -> List[datetime]:
    """Generate timezone-aware slot starts for the next AVAILABILITY_DAYS days."""
    start_t, end_t = _parse_working_window()
    lunch = _parse_lunch()
    duration = timedelta(minutes=config.SLOT_DURATION_MINUTES)
    now = datetime.now(tz)
    today = now.date()
    slots: List[datetime] = []

    for offset in range(config.AVAILABILITY_DAYS):
        day: date = today + timedelta(days=offset)
        if day.weekday() not in config.BUSINESS_WEEKDAYS:
            continue
        cursor = datetime.combine(day, start_t, tzinfo=tz)
        day_end = datetime.combine(day, end_t, tzinfo=tz)
        while cursor + duration <= day_end:
            slot_end = cursor + duration
            if lunch:
                lunch_start = datetime.combine(day, lunch[0], tzinfo=tz)
                lunch_end = datetime.combine(day, lunch[1], tzinfo=tz)
                overlaps_lunch = cursor < lunch_end and slot_end > lunch_start
                if overlaps_lunch:
                    cursor += duration
                    continue
            # Skip slots that have already started (plus a 15-minute buffer).
            if cursor <= now + timedelta(minutes=15):
                cursor += duration
                continue
            slots.append(cursor)
            cursor += duration
    return slots


def _load_local_bookings() -> List[Dict[str, Any]]:
    if not config.BOOKINGS_FILE.exists():
        return []
    try:
        return json.loads(config.BOOKINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_local_booking(record: Dict[str, Any]) -> None:
    bookings = _load_local_bookings()
    bookings.append(record)
    config.BOOKINGS_FILE.write_text(
        json.dumps(bookings, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _busy_from_local() -> List[Tuple[datetime, datetime]]:
    busy = []
    tz = config.TIMEZONE
    for rec in _load_local_bookings():
        try:
            start = datetime.fromisoformat(rec["start"])
            end = datetime.fromisoformat(rec["end"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            busy.append((start, end))
        except (KeyError, ValueError):
            continue
    return busy


def _busy_from_google(service, time_min: datetime, time_max: datetime) -> List[Tuple[datetime, datetime]]:
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": config.TIMEZONE_NAME,
        "items": [{"id": config.CALENDAR_ID}],
    }
    try:
        result = service.freebusy().query(body=body).execute()
    except HttpError as exc:
        logger.error("Google freebusy query failed: %s", exc)
        return []

    calendars = result.get("calendars", {})
    cal = calendars.get(config.CALENDAR_ID, {})
    busy = []
    for block in cal.get("busy", []):
        start = datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
        busy.append((start.astimezone(config.TIMEZONE), end.astimezone(config.TIMEZONE)))
    return busy


def _overlaps(start: datetime, end: datetime, busy: List[Tuple[datetime, datetime]]) -> bool:
    for b_start, b_end in busy:
        if start < b_end and end > b_start:
            return True
    return False


def _local_appointments_for_email(email: str) -> List[Dict[str, Any]]:
    """Filter the local bookings.json fallback by attendee email."""
    target = email.strip().lower()
    found: List[Dict[str, Any]] = []
    for rec in _load_local_bookings():
        if str(rec.get("email", "")).strip().lower() != target:
            continue
        found.append(
            {
                "start": rec.get("start", ""),
                "end": rec.get("end", ""),
                "name": rec.get("name", ""),
                "email": target,
                "reason": rec.get("reason", ""),
                "event_id": "",
                "source": "local",
            }
        )
    return found


def _google_appointments_for_email(
    service, email: str, lookback_days: int = 180, lookahead_days: int = 365
) -> List[Dict[str, Any]]:
    """Search the calendar for events whose attendee email matches."""
    target = email.strip().lower()
    tz = config.TIMEZONE
    now = datetime.now(tz)
    query = {
        "calendarId": config.CALENDAR_ID,
        "q": target,
        "timeMin": (now - timedelta(days=lookback_days)).isoformat(),
        "timeMax": (now + timedelta(days=lookahead_days)).isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
    try:
        result = service.events().list(**query).execute()
    except HttpError as exc:
        logger.error("Google event lookup failed: %s", exc)
        return []

    found: List[Dict[str, Any]] = []
    for item in result.get("items", []):
        attendees = item.get("attendees") or []
        attendee_emails = [str(a.get("email", "")).lower() for a in attendees]
        description = str(item.get("description", ""))
        if target not in attendee_emails and target not in description.lower():
            continue

        start_raw = item.get("start", {}) or {}
        end_raw = item.get("end", {}) or {}
        start_iso = start_raw.get("dateTime")
        end_iso = end_raw.get("dateTime")
        # Skip all-day events: they carry a date but no time, so they are not slots.
        if not start_iso:
            continue
        end_iso = end_iso or start_iso

        name = ""
        for attendee in attendees:
            if str(attendee.get("email", "")).lower() == target:
                name = str(attendee.get("displayName", ""))
                break
        if not name:
            name = str(item.get("summary", "")).replace("Appointment:", "").split("--")[0].strip()

        reason = ""
        for line in description.splitlines():
            if line.strip().lower().startswith("reason / service:"):
                reason = line.split(":", 1)[1].strip()
                break

        found.append(
            {
                "start": start_iso,
                "end": end_iso,
                "name": name,
                "email": target,
                "reason": reason,
                "event_id": item.get("id", ""),
                "source": "google",
            }
        )
    return found


def find_appointments_by_email(email: str) -> List[Dict[str, Any]]:
    """
    Return every appointment booked with the given email, sorted by start time.

    Uses Google Calendar when configured, otherwise the bookings.json fallback.
    Each item: {start, end, name, email, reason, event_id, source}.
    """
    email = (email or "").strip()
    if not email:
        return []

    service = _calendar_service()
    if service is not None:
        appointments = _google_appointments_for_email(service, email)
    else:
        appointments = _local_appointments_for_email(email)
        logger.warning("Google Calendar not configured; searched local bookings.json")

    appointments.sort(key=lambda item: item.get("start", ""))
    return appointments


def get_available_slots() -> List[Dict[str, str]]:
    """
    Return free slots for the next week.

    Each item: {start, end, label} where start/end are ISO-8601 with offset.
    """
    tz = config.TIMEZONE
    candidates = _iter_candidate_slots(tz)
    if not candidates:
        return []

    duration = timedelta(minutes=config.SLOT_DURATION_MINUTES)
    time_min = candidates[0]
    time_max = candidates[-1] + duration

    service = _calendar_service()
    if service is not None:
        busy = _busy_from_google(service, time_min, time_max)
        logger.info("Loaded %d busy block(s) from Google Calendar", len(busy))
    else:
        busy = _busy_from_local()
        logger.warning("Google Calendar not configured; using local bookings.json")

    free: List[Dict[str, str]] = []
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for start in candidates:
        end = start + duration
        if _overlaps(start, end, busy):
            continue
        label = (
            f"{weekday_names[start.weekday()]} {start.strftime('%b %d')} "
            f"{start.strftime('%I:%M %p').lstrip('0')} - {end.strftime('%I:%M %p').lstrip('0')}"
        )
        free.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "label": label,
            }
        )
    return free


def create_event(
    *,
    start_iso: str,
    end_iso: str,
    name: str,
    email: str,
    phone: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """
    Create a calendar event after a free/busy re-check (prevents double-booking).

    Returns {ok, event_id, html_link, start, end, fallback}.
    Raises ValueError if the slot is no longer free or times are invalid.
    """
    tz = config.TIMEZONE
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    start = start.astimezone(tz)
    end = end.astimezone(tz)

    if end <= start:
        raise ValueError("End time must be after start time.")

    service = _calendar_service()
    if service is not None:
        busy = _busy_from_google(service, start, end)
        if _overlaps(start, end, busy):
            raise ValueError("That time slot is no longer available. Please pick another.")

        description_lines = [
            f"Booked via {config.BOT_NAME} chatbot.",
            f"Name: {name}",
            f"Email: {email}",
        ]
        if phone:
            description_lines.append(f"Phone: {phone}")
        if reason:
            description_lines.append(f"Reason / service: {reason}")

        body = {
            "summary": f"Appointment: {name} -- {config.BUSINESS_NAME}",
            "description": "\n".join(description_lines),
            "start": {"dateTime": start.isoformat(), "timeZone": config.TIMEZONE_NAME},
            "end": {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE_NAME},
            "attendees": [{"email": email, "displayName": name}],
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 60},
                    {"method": "popup", "minutes": 30},
                ],
            },
        }
        try:
            event = (
                service.events()
                .insert(
                    calendarId=config.CALENDAR_ID,
                    body=body,
                    sendUpdates="none",  # we send our own confirmation emails
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Failed to create Google Calendar event: %s", exc)
            raise ValueError("Could not create the calendar event. Please try again.") from exc

        return {
            "ok": True,
            "event_id": event.get("id", ""),
            "html_link": event.get("htmlLink", ""),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fallback": False,
        }

    # Local fallback (no Google credentials yet).
    if _overlaps(start, end, _busy_from_local()):
        raise ValueError("That time slot is no longer available. Please pick another.")

    record = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "name": name,
        "email": email,
        "phone": phone,
        "reason": reason,
        "created_at": datetime.now(tz).isoformat(),
    }
    _save_local_booking(record)
    logger.warning("Saved booking locally (Google Calendar not configured).")
    return {
        "ok": True,
        "event_id": f"local-{start.timestamp():.0f}",
        "html_link": "",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fallback": True,
    }
