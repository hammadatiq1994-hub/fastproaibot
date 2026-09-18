"""
Flask REST API for the WordPress AI chatbot.

Endpoints
---------
POST /api/chat            Conversational turn (RAG + OpenRouter).
GET  /api/availability    Free appointment slots for the next 7 days.
POST /api/book            Create a calendar event + send confirmation emails.
POST /api/session/reset   Clear in-memory conversation history for a session.
GET  /api/health          Liveness / config readiness (no secrets leaked).

Auth: every /api/* route except /api/health requires header
      X-API-Key: <API_SECRET_KEY>

Run on Windows (Waitress):
    python app.py
    waitress-serve --host=0.0.0.0 --port=5000 app:app
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS
from openai import OpenAI

import calendar_service
import config
import gmail_service
import rag

logger = config.setup_logging()

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# Restrict cross-origin access to the WordPress site, localhost, and preview hosts.
_PREVIEW_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    r"|^https?://([a-z0-9-]+\.)*monkeycode-ai\.live$",
    re.IGNORECASE,
)
CORS(
    app,
    origins=list(config.ALLOWED_ORIGINS) or "*",
    allow_headers=["Content-Type", "X-API-Key", "X-Session-Id"],
    methods=["GET", "POST", "OPTIONS"],
    supports_credentials=False,
)
DEMO_DIR = config.BASE_DIR / "demo"


def _origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    cleaned = origin.rstrip("/")
    if cleaned in config.ALLOWED_ORIGINS:
        return True
    return bool(_PREVIEW_ORIGIN_RE.match(cleaned))


def _is_same_origin() -> bool:
    """True when the browser is talking to this Flask process directly (demo page)."""
    origin = request.headers.get("Origin", "")
    if origin:
        return urlparse(origin).netloc == request.host
    referer = request.headers.get("Referer", "")
    if referer:
        return urlparse(referer).netloc == request.host
    return False

# ---------------------------------------------------------------------------
# In-memory session store  {session_id: {"messages": [...], "updated": ts}}
# Fine for a single Windows process. Swap for Redis if you scale out later.
# ---------------------------------------------------------------------------
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()

# Simple sliding-window rate limiter  {ip: deque[timestamps]}
_rate_buckets: Dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _extract_booking_state(history: List[Dict[str, str]], booked: bool = False) -> str:
    """Summarise which booking details are already known for this visitor."""
    text = " ".join(m.get("content", "") for m in history).lower()

    has_email = bool(re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", text))
    has_name = "my name is" in text or "i am " in text or "mera naam" in text

    lines = [
        f"- Email collected: {'YES' if has_email else 'NO - ask for it'}",
        f"- Name collected: {'YES' if has_name else 'NO - ask for it'}",
        f"- Booking confirmed: {'YES - do not offer booking again' if booked else 'NO'}",
    ]
    return "\n".join(lines)

SYSTEM_PROMPT = """You are {bot_name}, the warm and professional virtual assistant for {business} — like a friendly receptionist who knows the business inside-out.

PERSONALITY & TONE
- Friendly, welcoming and genuinely helpful. Sound human, warm and confident.
- Keep replies short and conversational (2-4 sentences unless listing items).
- Use a light emoji occasionally (👋 📅 💰 ✅) but stay professional — never spam them.
- Never be pushy. Never invent facts.
- Address the user by name if they share it.
- Match the user's language: if they write in Urdu, Spanish, or any other
  language, reply in that same language (keep business names as-is).

GROUNDING RULES
- Prefer the knowledge-base excerpts below for company-specific facts
  (prices, plan names, hours, address, phone, policies, what this firm offers).
- Answer the question FIRST. Quote prices, plan names, coverage, hours,
  and contact details when they appear in the excerpts. Do not skip a
  known answer and jump to booking.
- If the excerpts only partly answer the question, share what you do know,
  then fill in the rest with a general in-domain explanation.
- If the excerpts do not contain a specific answer, but the question is
  still in this firm's domain (tax, payroll, accounting, bookkeeping,
  business setup, licensing, insurance, BOI/CTA, compliance), give a
  helpful general answer from your professional knowledge. Be clear that
  it is general guidance, not this firm's quoted policy or a personalized
  determination. Do NOT say "I don't have that information" for in-domain
  questions.
- Never invent this firm's prices, plan features, guarantees, hours, or
  contact details. If those specifics are missing from the excerpts, give
  the general explanation and offer a consultation for an exact quote.
- If the question is clearly outside this firm's domain (sports, cooking,
  celebrity gossip, unrelated tech, etc.), say you focus on tax and
  business services and offer to book a consultation or answer a related
  question. Do not answer off-topic questions.
- Never give personalized tax, legal, or financial conclusions
  (e.g. "you qualify for X", "you owe $Y"). Explain the general rule and
  recommend a free consultation for their situation.
- If asked whether you are an AI, admit it honestly and warmly, then
  continue helping.

APPOINTMENT GUIDANCE
- Always try to be useful first (answer the question from the knowledge base).
- When the visitor shows buying intent, proactively offer to book: 
  "Would you like me to book a free consultation for you? 😊"
- BOOKING STATE — you are given the booking details collected so far:
{booking_state}
- Collect in this order: full name → email → reason/service. Phone is optional.
- Ask for ONLY ONE missing detail per reply. Never re-ask for a detail
  already provided above.
- Once name + email + reason are known, immediately tell the user that
  available time slots will appear as buttons below — do not ask again.
- After a slot is booked (see booking status above), congratulate the user,
  remind them of the confirmation email, and move the conversation to a
  warm close. NEVER offer to book again.
- Never claim a slot is booked until the system confirms it.

CHECKING AN EXISTING APPOINTMENT
- You CAN check appointments. Never say you are unable to access appointment
  details, and never refuse or send the visitor away for this.
- If the visitor wants to check, confirm, or look up an appointment they
  already made, ask for the email address they booked with. Never guess or
  invent whether a booking exists, and never state a time from memory.
- The appointment lookup itself is handled by the system. Keep this reply short.

HANDLING DIFFICULT MOMENTS
- If the user is frustrated or complains: acknowledge the feeling first
  ("I'm sorry for the trouble"), then help or offer the appointment.
- If the message is rude or inappropriate: stay calm and polite, redirect
  to how you can help. Never argue, never mirror rudeness.
- If the user goes off-topic (jokes, general chat): respond briefly and
  warmly, then gently steer back to the business.

QUICK REPLIES
You may attach suggested buttons for the widget by ending your reply with a
JSON block on its own last line, exactly in this form (no markdown fences):
__BUTTONS__[{{"label":"Book Appointment","value":"I would like to book an appointment"}},{{"label":"Check my appointment","value":"I would like to check my appointment"}},{{"label":"See services","value":"What services do you offer?"}}]
Omit __BUTTONS__ if no buttons are needed. When the user is ready to pick a
time, tell them slots will appear as buttons (the server injects them).

KNOWLEDGE BASE EXCERPTS
{context}

"""



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _parse_limit(spec: str) -> Tuple[int, int]:
    """'30 per minute' -> (30, 60)."""
    parts = spec.lower().replace("per", " ").split()
    count = int(parts[0]) if parts else 30
    unit = parts[-1] if len(parts) > 1 else "minute"
    seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(unit.rstrip("s"), 60)
    return count, seconds


def rate_limit(spec: str):
    count, window = _parse_limit(spec)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = _client_ip()
            now = time.time()
            with _rate_lock:
                bucket = _rate_buckets[key]
                while bucket and bucket[0] < now - window:
                    bucket.popleft()
                if len(bucket) >= count:
                    logger.warning("Rate limit exceeded for %s on %s", key, request.path)
                    return jsonify({"error": "Too many requests. Please wait a moment."}), 429
                bucket.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-API-Key", "")
        expected = config.API_SECRET_KEY
        if provided and expected and secrets.compare_digest(provided, expected):
            return fn(*args, **kwargs)
        # Demo page is served by this same process -- no key needed for same-origin.
        if _is_same_origin():
            return fn(*args, **kwargs)
        logger.warning("Unauthorized request from %s", _client_ip())
        return jsonify({"error": "Unauthorized."}), 401

    return wrapper


def _purge_sessions() -> None:
    cutoff = time.time() - config.SESSION_TTL_HOURS * 3600
    with _sessions_lock:
        stale = [sid for sid, rec in _sessions.items() if rec.get("updated", 0) < cutoff]
        for sid in stale:
            _sessions.pop(sid, None)


def _get_session(session_id: Optional[str]) -> Tuple[str, List[Dict[str, str]]]:
    _purge_sessions()
    if not session_id or len(session_id) > 64:
        session_id = str(uuid.uuid4())
    with _sessions_lock:
        rec = _sessions.setdefault(session_id, {"messages": [], "updated": time.time()})
        rec["updated"] = time.time()
        return session_id, rec["messages"]


def _save_session(session_id: str, messages: List[Dict[str, str]]) -> None:
    trimmed = messages[-config.MAX_HISTORY_MESSAGES :]
    with _sessions_lock:
        # Preserve extra fields (e.g. "mode") set by other helpers.
        rec = _sessions.setdefault(session_id, {"messages": [], "updated": time.time()})
        rec["messages"] = trimmed
        rec["updated"] = time.time()


def _get_session_mode(session_id: str) -> Optional[str]:
    with _sessions_lock:
        rec = _sessions.get(session_id)
        return rec.get("mode") if rec else None


def _set_session_mode(session_id: str, mode: Optional[str]) -> None:
    with _sessions_lock:
        rec = _sessions.setdefault(session_id, {"messages": [], "updated": time.time()})
        rec["mode"] = mode
        rec["updated"] = time.time()


def _is_session_booked(session_id: str) -> bool:
    with _sessions_lock:
        rec = _sessions.get(session_id)
        return bool(rec.get("booked")) if rec else False


def _set_session_booked(session_id: str, booked: bool = True) -> None:
    with _sessions_lock:
        rec = _sessions.setdefault(session_id, {"messages": [], "updated": time.time()})
        rec["booked"] = booked
        rec["updated"] = time.time()


def _openai_client() -> OpenAI:
    return OpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": config.WEBSITE_DOMAIN,
            "X-Title": config.BUSINESS_NAME,
        },
    )


_DOMAIN_RE = re.compile(
    r"\b("
    r"tax|taxes|taxation|irs|fincen|boi|cta|payroll|w-?2|1099|1098|"
    r"deduction|credit|withholding|sales tax|income tax|return|"
    r"accounting|bookkeeping|invoice|invoicing|ar/ap|reconciliation|"
    r"llc|s-?corp|c-?corp|dba|corporation|partnership|ein|itin|"
    r"license|licensing|permit|formation|incorporation|"
    r"insurance|liability|workers?\s*comp\w*|compensation|disability|bop|"
    r"plan|starter|advanced|enterprise|employee|employees|"
    r"consultation|compliance|bookkeeping|virtual office|"
    r"beneficial owner|financing|loan"
    r")\b",
    re.IGNORECASE,
)
_OUT_OF_DOMAIN_RE = re.compile(
    r"\b("
    r"recipe|cook|cooking|soccer|football|cricket|nba|nfl|mlb|nhl|"
    r"super bowl|world cup|movie|netflix|celebrity|weather|horoscope|"
    r"bitcoin|crypto|video game|lyrics|sports|score"
    r")\b",
    re.IGNORECASE,
)
_FOLLOWUP_RE = re.compile(
    r"^(and|also|what about|how about|tell me more|more details|that too)\b",
    re.IGNORECASE,
)


def _is_in_domain(message: str) -> bool:
    """True when the question is about this firm's field of work."""
    if _OUT_OF_DOMAIN_RE.search(message):
        return False
    if _DOMAIN_RE.search(message):
        return True
    return bool(_FOLLOWUP_RE.search(message.strip()))


def _extract_buttons(reply: str) -> Tuple[str, List[Dict[str, str]]]:
    """Pull a trailing __BUTTONS__[...json...] line out of the model reply."""
    buttons: List[Dict[str, str]] = []
    text = reply.strip()
    marker = "__BUTTONS__"
    idx = text.rfind(marker)
    if idx == -1:
        return text, buttons
    raw = text[idx + len(marker) :].strip()
    text = text[:idx].strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for item in parsed[:8]:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip()[:40]
                value = str(item.get("value", label)).strip()[:200]
                if label:
                    buttons.append({"label": label, "value": value})
    except json.JSONDecodeError:
        logger.debug("Could not parse __BUTTONS__ payload: %s", raw[:120])
    return text, buttons


_SLOT_REQUEST_RE = re.compile(
    r"\b(book|booking|schedule|reschedule|reserve|slot|slots|meeting|availability)\b",
    re.IGNORECASE,
)
# "appointment" is only a booking request when paired with an action word,
# so "what is an appointment?" does not open the slot picker.
_APPOINTMENT_REQUEST_RE = re.compile(
    r"\b(?:appointment|appt)\b[^.!?]{0,40}?\b(?:book|schedule|reserve|make|set|arrange|want|need|new|another)\b"
    r"|\b(?:book|schedule|reserve|make|set|arrange|want|need|new|another)\b[^.!?]{0,40}?\b(?:appointment|appt)\b",
    re.IGNORECASE,
)
_SHOW_MORE_RE = re.compile(r"show more appointment|more appointment time", re.IGNORECASE)
_AGREE_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|please|go ahead|sounds good|that works|confirm)\b",
    re.IGNORECASE,
)
_BOT_OFFERED_BOOKING = (
    "would you like me to book",
    "book a free consultation",
    "book an appointment",
    "see available times",
    "available times",
    "available time slot",
    "pick a time",
    "choose a slot",
)


def _last_assistant_message(history: List[Dict[str, str]]) -> str:
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            return (msg.get("content") or "").lower()
    return ""


def _intent_wants_slots(
    user_message: str,
    history: List[Dict[str, str]],
    booked: bool = False,
) -> bool:
    """
    True only when the visitor clearly wants to pick an appointment time.

    Slots are shown when the visitor explicitly asks to book, or agrees after
    the bot offered to book. Once a booking is confirmed for this session they
    are not shown again unless the visitor asks to book a new one.
    """
    text = user_message.lower().strip()

    if booked:
        # A completed booking must not reopen the slot list on "ok"/"thanks".
        return bool(
            re.search(r"\b(book|schedule|reserve|another|new appointment)\b", text)
        )

    if (
        _SLOT_REQUEST_RE.search(text)
        or _APPOINTMENT_REQUEST_RE.search(text)
        or _SHOW_MORE_RE.search(text)
    ):
        return True

    # Agreement is only enough when the bot's previous turn actually offered it.
    if _AGREE_RE.search(text):
        return any(phrase in _last_assistant_message(history) for phrase in _BOT_OFFERED_BOOKING)

    return False


def _slot_buttons(limit: int = 8) -> List[Dict[str, str]]:
    slots = calendar_service.get_available_slots()
    buttons = []
    for slot in slots[:limit]:
        buttons.append(
            {
                "label": slot["label"],
                "value": f"Book this slot: {slot['start']}",
                "start": slot["start"],
                "end": slot["end"],
            }
        )
    if len(slots) > limit:
        buttons.append({"label": "More times", "value": "Show more appointment times"})
    return buttons


def _looks_like_slot_choice(message: str) -> Optional[str]:
    """Return an ISO start string if the user picked a slot."""
    match = re.search(r"Book this slot:\s*(\S+)", message)
    if match:
        return match.group(1)
    # Also accept a raw ISO-8601 datetime pasted by the widget.
    try:
        dt = datetime.fromisoformat(message.strip())
        if dt.tzinfo is not None:
            return dt.isoformat()
    except ValueError:
        return None
    return None


# -- Appointment status lookup ----------------------------------------------
EMAIL_SEARCH_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_BOOK_ACTION_RE = re.compile(r"\b(book|schedule|reserve|create|make|new)\b", re.IGNORECASE)
_STATUS_ACTION_RE = re.compile(
    r"\b(check|status|when|what time|confirm|verify|find|look|scheduled|already)\b",
    re.IGNORECASE,
)
_LOOKUP_PHRASES = (
    "check my appointment",
    "check the appointment",
    "check appointment",
    "check my booking",
    "check booking",
    "check my slot",
    "check my reservation",
    "check status",
    "check my schedule",
    "my appointment",
    "my booking",
    "my reservation",
    "appointment status",
    "booking status",
    "appointment time",
    "booking time",
    "slot time",
    "when is my",
    "when's my",
    "what time is my",
    "confirm my appointment",
    "confirm my booking",
    "already booked",
    "do i have an appointment",
    "do i have a booking",
    "am i booked",
    "anything scheduled",
    "look up my appointment",
    "find my appointment",
)
_CANCEL_RE = re.compile(
    r"\b(cancel|never ?mind|forget it|no thanks|no thank you|stop)\b", re.IGNORECASE
)


_APPOINTMENT_WORD_RE = re.compile(
    r"\b(appointment|appt|bookings?|booked|reservations?|reserved|slots?|scheduled)\b",
    re.IGNORECASE,
)


def _is_appointment_lookup(message: str) -> bool:
    """
    True when the visitor is asking to check an existing appointment.

    Bookings are only treated as a *new* booking when the visitor uses an
    action word (book/schedule/reserve) without any status word. Everything
    else that pairs appointment wording with a status word is a lookup.
    """
    text = message.lower()

    # Strongest signal: an explicit "check / status / when" phrase.
    if any(phrase in text for phrase in _LOOKUP_PHRASES):
        if _BOOK_ACTION_RE.search(text) and not _STATUS_ACTION_RE.search(text):
            return False
        return True

    # Appointment wording + a status word (e.g. "can you verify my booking",
    # "my appointment time?", "appointment for x@y.com, please find it").
    if _APPOINTMENT_WORD_RE.search(text) and _STATUS_ACTION_RE.search(text):
        if _BOOK_ACTION_RE.search(text) and not _STATUS_ACTION_RE.search(text):
            return False
        return True

    # Appointment wording + an email address (e.g. "appointment, my email is
    # x@y.com"). A pure booking request with an email is never hijacked.
    if _extract_email(message) and _APPOINTMENT_WORD_RE.search(text):
        if _BOOK_ACTION_RE.search(text) and not _STATUS_ACTION_RE.search(text):
            return False
        return True

    return False


def _extract_email(message: str) -> Optional[str]:
    match = EMAIL_SEARCH_RE.search(message)
    return match.group(0) if match else None


def _is_email_only(message: str) -> bool:
    """True when the message is basically just an email address."""
    email = _extract_email(message)
    if not email:
        return False
    remainder = message.replace(email, "").strip(" .,:;-!?<>()[]")
    return len(remainder) <= 3


_REFUSAL_PATTERNS = (
    "can't access",
    "cannot access",
    "can not access",
    "don't have access",
    "do not have access",
    "unable to access",
    "no access to",
    "can't check",
    "cannot check",
    "unable to check",
    "can't look up",
    "cannot look up",
    "unable to look up",
    "can't retrieve",
    "cannot retrieve",
    "unable to retrieve",
    "don't have the ability",
    "do not have the ability",
    "not able to access",
)


def _looks_like_refusal(reply: str) -> bool:
    """True when the model claims it cannot check appointment data."""
    low = (reply or "").lower()
    return any(pattern in low for pattern in _REFUSAL_PATTERNS)


def _format_appointment_time(start_iso: str, end_iso: str) -> str:
    tz = config.TIMEZONE
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        return start_iso
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    start = start.astimezone(tz)

    try:
        end = datetime.fromisoformat(end_iso)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        end = end.astimezone(tz)
        end_text = end.strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        end_text = ""

    day = start.strftime("%A, %B %d, %Y")
    start_text = start.strftime("%I:%M %p").lstrip("0")
    if end_text:
        return f"{day} from {start_text} to {end_text} ({config.TIMEZONE_NAME})"
    return f"{day} at {start_text} ({config.TIMEZONE_NAME})"


def _appointment_status_reply(email: str) -> str:
    """
    Look up the visitor's appointments and report only the upcoming ones.

    Expired appointments are never listed when a future appointment exists.
    If the visitor has no future appointment at all, the most recent expired
    one is mentioned so they know that slot has passed.
    """
    try:
        appointments = calendar_service.find_appointments_by_email(email)
    except Exception as exc:
        logger.exception("Appointment lookup failed: %s", exc)
        return (
            "I'm sorry, I couldn't check your appointment right now. "
            "Please try again in a moment."
        )

    if not appointments:
        return (
            f"I couldn't find any appointment booked with {email}. "
            "Would you like me to book one for you?"
        )

    now = datetime.now(config.TIMEZONE)
    upcoming: List[Tuple[datetime, Dict[str, Any]]] = []
    expired: List[Tuple[datetime, Dict[str, Any]]] = []
    for appt in appointments:
        try:
            start = datetime.fromisoformat(appt.get("start", ""))
        except (TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=config.TIMEZONE)
        start = start.astimezone(config.TIMEZONE)
        (expired if start < now else upcoming).append((start, appt))

    if not upcoming and not expired:
        return (
            f"I found a booking for {email}, but its time could not be read. "
            "Please contact us to confirm."
        )

    # Upcoming appointments win: show only these, never the expired ones.
    if upcoming:
        if len(upcoming) == 1:
            appt = upcoming[0][1]
            return (
                "Yes, your appointment is confirmed for "
                f"{_format_appointment_time(appt.get('start', ''), appt.get('end', ''))}."
            )
        lines = ["Yes, you have these upcoming appointments:"]
        for _, appt in upcoming:
            lines.append(
                "- " + _format_appointment_time(appt.get("start", ""), appt.get("end", ""))
            )
        return "\n".join(lines)

    # No future appointment: report the latest expired one so the visitor knows.
    latest = expired[-1][1]
    return (
        "Your appointment on "
        f"{_format_appointment_time(latest.get('start', ''), latest.get('end', ''))} "
        "has already expired. Would you like me to book a new one?"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.before_request
def _log_request():
    g.request_started = time.time()
    if request.path.startswith("/api/"):
        logger.info("%s %s from %s", request.method, request.path, _client_ip())


@app.after_request
def _log_response(response):
    started = getattr(g, "request_started", None)
    if started and request.path.startswith("/api/"):
        ms = (time.time() - started) * 1000
        logger.info("%s %s -> %s (%.0f ms)", request.method, request.path, response.status_code, ms)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    origin = request.headers.get("Origin", "")
    if _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, X-Session-Id"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response


@app.errorhandler(400)
def _bad_request(err):
    return jsonify({"error": "Bad request."}), 400


@app.errorhandler(404)
def _not_found(err):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(405)
def _method_not_allowed(err):
    return jsonify({"error": "Method not allowed."}), 405


@app.errorhandler(500)
def _server_error(err):
    logger.exception("Unhandled server error: %s", err)
    return jsonify({"error": "Internal server error."}), 500


@app.route("/", methods=["GET"])
def demo_home():
    """Local widget preview (not used on WordPress)."""
    return send_from_directory(DEMO_DIR, "index.html")


@app.route("/api/widget-config", methods=["GET"])
def widget_config():
    """Public demo helper. Does not expose the API secret."""
    return jsonify(
        {
            "api_url": "",
            "bot_name": config.BOT_NAME,
            "business": config.BUSINESS_NAME,
        }
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "business": config.BUSINESS_NAME,
            "bot": config.BOT_NAME,
            "llm_configured": config.llm_ready(),
            "google_configured": config.google_ready(),
            "knowledge_chunks": rag.collection_count(),
            "timezone": config.TIMEZONE_NAME,
            "slot_minutes": config.SLOT_DURATION_MINUTES,
        }
    )


@app.route("/api/chat", methods=["POST"])
@require_api_key
@rate_limit(config.CHAT_RATE_LIMIT)
def chat():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    session_id = str(payload.get("session_id") or request.headers.get("X-Session-Id") or "")

    if not message:
        return jsonify({"error": "Message is required."}), 400
    if len(message) > config.MAX_MESSAGE_LENGTH:
        return jsonify({"error": f"Message exceeds {config.MAX_MESSAGE_LENGTH} characters."}), 400

    session_id, history = _get_session(session_id)

    # Appointment status lookup runs deterministically (no LLM guesswork):
    # it asks for the visitor's email, looks up the booking, then reports
    # whether the slot is still upcoming or has already expired.
    mode = _get_session_mode(session_id)
    # Safety net: if the bot just asked for the booking email and the visitor
    # replies with only their address, treat it as the lookup even when the
    # original wording was not recognised as a lookup.
    last_bot = _last_assistant_message(history)
    awaiting_email = mode == "lookup" or (
        "email address you used" in last_bot or "check that for you" in last_bot
    )
    if _is_appointment_lookup(message) or mode == "lookup" or (_is_email_only(message) and awaiting_email):
        if _CANCEL_RE.search(message) and not _extract_email(message):
            _set_session_mode(session_id, None)
            reply = "No problem. Is there anything else I can help you with?"
            buttons = [{"label": "Book Appointment", "value": "I would like to book an appointment"}]
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": reply})
            _save_session(session_id, history)
            return jsonify(
                {"reply": reply, "session_id": session_id, "buttons": buttons, "sources_used": 0}
            )

        email = _extract_email(message)
        if not email:
            _set_session_mode(session_id, "lookup")
            reply = (
                "Sure, I can check that for you. "
                "Please enter the email address you used when booking."
            )
            buttons = [
                {"label": "Book Appointment", "value": "I would like to book an appointment"},
                {"label": "Cancel", "value": "cancel"},
            ]
        else:
            _set_session_mode(session_id, None)
            reply = _appointment_status_reply(email)
            buttons = [{"label": "Book Appointment", "value": "I would like to book an appointment"}]

        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        _save_session(session_id, history)
        return jsonify(
            {"reply": reply, "session_id": session_id, "buttons": buttons, "sources_used": 0}
        )

    # If the user just picked a slot, acknowledge and ask for remaining details
    # (or confirm if we already have them). The actual booking is POST /api/book.
    slot_iso = _looks_like_slot_choice(message)
    extra_buttons: List[Dict[str, str]] = []

    hits = rag.retrieve(message)
    context = rag.format_context(hits)
    if not context:
        if _is_in_domain(message):
            context = (
                "(No matching knowledge-base excerpts were found. The question is "
                "still in this firm's domain. Give a helpful general answer from "
                "professional knowledge. Do not invent this firm's prices, plan "
                "features, hours, or policies. Do not say you lack the information. "
                "Offer a consultation only if they need a firm-specific quote.)"
            )
        else:
            context = (
                "(No matching knowledge-base excerpts were found, and the question "
                "appears outside this firm's domain. Politely say you focus on tax "
                "and business services, then offer to book an appointment or answer "
                "a related question. Do not answer the off-topic question.)"
            )

    system = SYSTEM_PROMPT.format(
        bot_name=config.BOT_NAME,
        business=config.BUSINESS_NAME,
        context=context,
        booking_state=_extract_booking_state(history, _is_session_booked(session_id)),
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    if not config.llm_ready():
        if _is_session_booked(session_id):
            reply = (
                "You're all set. Is there anything else I can help you with?"
            )
            extra_buttons = []
        else:
            reply = (
                f"Thanks for reaching out to {config.BUSINESS_NAME}. "
                "The AI engine is not configured yet, but I can still help you book "
                "an appointment. Would you like to see available times?"
            )
            extra_buttons = [{"label": "Book Appointment", "value": "I would like to book an appointment"}]
        logger.warning("OPENROUTER_API_KEY missing; returning fallback reply.")
    else:
        try:
            client = _openai_client()
            completion = client.chat.completions.create(
                model=config.OPENROUTER_MODEL,
                messages=messages,
                temperature=config.LLM_TEMPERATURE,
                max_tokens=config.LLM_MAX_TOKENS,
            )
            reply = (completion.choices[0].message.content or "").strip()
            if not reply:
                reply = "I am sorry, I could not generate a reply. Please try again."
        except Exception as exc:
            logger.exception("OpenRouter call failed: %s", exc)
            return jsonify({"error": "The assistant is temporarily unavailable. Please try again."}), 502

    reply, buttons = _extract_buttons(reply)

    # Safety net: if the model wrongly claims it cannot access appointments,
    # switch to the deterministic lookup so the visitor is never sent away.
    if _APPOINTMENT_WORD_RE.search(message) and _looks_like_refusal(reply):
        email = _extract_email(message)
        if email:
            _set_session_mode(session_id, None)
            reply = _appointment_status_reply(email)
            buttons = [{"label": "Book Appointment", "value": "I would like to book an appointment"}]
        else:
            _set_session_mode(session_id, "lookup")
            reply = (
                "Sure, I can check that for you. "
                "Please enter the email address you used when booking."
            )
            buttons = [
                {"label": "Book Appointment", "value": "I would like to book an appointment"},
                {"label": "Cancel", "value": "cancel"},
            ]
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        _save_session(session_id, history)
        return jsonify(
            {"reply": reply, "session_id": session_id, "buttons": buttons, "sources_used": 0}
        )

    if slot_iso:
        # The visitor picked a time. Do not show the slot list again; the widget
        # opens the booking form so they can finish their details.
        extra_buttons = []
        buttons = []
        reply = "Great choice. Please confirm your details to finish booking."
    elif _intent_wants_slots(message, history, booked=_is_session_booked(session_id)):
        slot_btns = _slot_buttons()
        if slot_btns and not any(b.get("start") for b in buttons):
            extra_buttons = slot_btns
            if "available" not in reply.lower() and "slot" not in reply.lower():
                reply = (
                    reply.rstrip()
                    + "\n\nHere are the next available times. Tap a slot to select it."
                )

    if extra_buttons:
        buttons = extra_buttons

    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    _save_session(session_id, history)

    return jsonify(
        {
            "reply": reply,
            "session_id": session_id,
            "buttons": buttons,
            "sources_used": len(hits),
        }
    )


@app.route("/api/availability", methods=["GET"])
@require_api_key
@rate_limit(config.CHAT_RATE_LIMIT)
def availability():
    try:
        slots = calendar_service.get_available_slots()
    except Exception as exc:
        logger.exception("Availability lookup failed: %s", exc)
        return jsonify({"error": "Could not load availability."}), 502
    return jsonify(
        {
            "timezone": config.TIMEZONE_NAME,
            "slot_minutes": config.SLOT_DURATION_MINUTES,
            "days": config.AVAILABILITY_DAYS,
            "slots": slots,
        }
    )


@app.route("/api/book", methods=["POST"])
@require_api_key
@rate_limit(config.BOOK_RATE_LIMIT)
def book():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip()
    phone = str(payload.get("phone", "")).strip()
    reason = str(payload.get("reason", "")).strip()
    start_iso = str(payload.get("start", "")).strip()
    end_iso = str(payload.get("end", "")).strip()
    session_id = str(payload.get("session_id") or request.headers.get("X-Session-Id") or "")

    errors = []
    if len(name) < 2:
        errors.append("Please provide your full name.")
    if not EMAIL_RE.match(email):
        errors.append("Please provide a valid email address.")
    if not start_iso:
        errors.append("Please choose a time slot.")
    if phone and (len(phone) < 7 or len(phone) > 32):
        errors.append("Phone number looks invalid.")
    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    if not end_iso:
        try:
            start_dt = datetime.fromisoformat(start_iso)
            end_iso = (start_dt + timedelta(minutes=config.SLOT_DURATION_MINUTES)).isoformat()
        except ValueError:
            return jsonify({"error": "Invalid start time."}), 400

    try:
        event = calendar_service.create_event(
            start_iso=start_iso,
            end_iso=end_iso,
            name=name,
            email=email,
            phone=phone,
            reason=reason,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.exception("Booking failed: %s", exc)
        return jsonify({"error": "Could not complete the booking. Please try again."}), 502

    mail = gmail_service.send_booking_emails(
        name=name,
        email=email,
        phone=phone,
        reason=reason,
        start_iso=event["start"],
        end_iso=event["end"],
    )

    confirmation = (
        f"You are booked, {name}. "
        f"A confirmation email is on its way to {email}. "
        "We look forward to speaking with you."
    )
    if event.get("fallback") or not mail.get("user_sent"):
        confirmation += (
            " (Email delivery is not fully configured yet; the appointment is still saved.)"
        )

    # Remember that this session already booked so the slot list stays closed
    # on the next "ok"/"thanks" reply.
    if session_id:
        _set_session_booked(session_id, True)
        _set_session_mode(session_id, None)

    return jsonify(
        {
            "ok": True,
            "message": confirmation,
            "event_id": event["event_id"],
            "start": event["start"],
            "end": event["end"],
            "emails": mail,
        }
    )


@app.route("/api/session/reset", methods=["POST"])
@require_api_key
@rate_limit(config.CHAT_RATE_LIMIT)
def reset_session():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id") or request.headers.get("X-Session-Id") or "")
    if session_id:
        with _sessions_lock:
            _sessions.pop(session_id, None)
    new_id = str(uuid.uuid4())
    return jsonify({"ok": True, "session_id": new_id})


def main() -> None:
    logger.info(
        "Starting %s chatbot on %s:%s (tz=%s, llm=%s, google=%s, kb=%d)",
        config.BUSINESS_NAME,
        config.HOST,
        config.PORT,
        config.TIMEZONE_NAME,
        config.llm_ready(),
        config.google_ready(),
        rag.collection_count(),
    )
    # Waitress is the production WSGI server for Windows (and works on Linux too).
    from waitress import serve

    serve(app, host=config.HOST, port=config.PORT, threads=8, ident="chatbot")


if __name__ == "__main__":
    main()
