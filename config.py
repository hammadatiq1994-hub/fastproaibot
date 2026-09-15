"""
Central configuration for the AI chatbot backend.

All runtime settings are loaded from environment variables / .env so the
same codebase can run on Windows (dev) and Linux (VPS) without changes.
Paths are built with pathlib and never assume a POSIX-only layout.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Project root = directory that contains this file (platform-independent).
BASE_DIR = Path(__file__).resolve().parent

# Load .env from the project root. override=False keeps real env vars winning.
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """Read a string env var, stripping whitespace."""
    value = os.getenv(name, default)
    return (value or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> List[str]:
    raw = _env(name, default)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
BUSINESS_NAME = _env("BUSINESS_NAME", "Your Business Name")
BOT_NAME = _env("BOT_NAME", "Alex")
ADMIN_EMAIL = _env("ADMIN_EMAIL", "")
WEBSITE_DOMAIN = _env("WEBSITE_DOMAIN", "http://localhost:5000")
ALLOWED_ORIGINS = _env_list(
    "ALLOWED_ORIGINS",
    "http://localhost,http://127.0.0.1,http://localhost:5000",
)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
HOST = _env("HOST", "0.0.0.0")
PORT = _env_int("PORT", 5000)
API_SECRET_KEY = _env("API_SECRET_KEY", "change-me-to-a-long-random-secret")
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible)
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Strong, affordable default. Override in .env, e.g. anthropic/claude-3.5-sonnet
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "openai/gpt-4o-mini")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 700)

# ---------------------------------------------------------------------------
# Knowledge base / RAG
# ---------------------------------------------------------------------------
KNOWLEDGE_DOCS_DIR = BASE_DIR / _env("KNOWLEDGE_DOCS_DIR", "knowledge_docs")
CHROMA_DIR = BASE_DIR / _env("CHROMA_DIR", "chroma_db")
CHROMA_COLLECTION = _env("CHROMA_COLLECTION", "website_kb")
RAG_TOP_K = _env_int("RAG_TOP_K", 5)
RAG_MIN_SCORE = float(_env("RAG_MIN_SCORE", "0.25"))

# ---------------------------------------------------------------------------
# Appointments / working hours (timezone-aware)
# ---------------------------------------------------------------------------
TIMEZONE_NAME = _env("TIMEZONE", "America/New_York")
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TIMEZONE = ZoneInfo("UTC")
    TIMEZONE_NAME = "UTC"

# Business days as abbreviated English names: Mon,Tue,Wed,Thu,Fri
_DAY_NAME_TO_WEEKDAY = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_raw_days = [d.lower()[:3] for d in _env_list("BUSINESS_DAYS", "Mon,Tue,Wed,Thu,Fri")]
BUSINESS_WEEKDAYS: List[int] = [
    _DAY_NAME_TO_WEEKDAY[d] for d in _raw_days if d in _DAY_NAME_TO_WEEKDAY
]
if not BUSINESS_WEEKDAYS:
    BUSINESS_WEEKDAYS = [0, 1, 2, 3, 4]

WORKING_HOURS_START = _env("WORKING_HOURS_START", "09:00")
WORKING_HOURS_END = _env("WORKING_HOURS_END", "17:00")
LUNCH_BREAK_START = _env("LUNCH_BREAK_START", "")  # empty = no lunch block
LUNCH_BREAK_END = _env("LUNCH_BREAK_END", "")
SLOT_DURATION_MINUTES = _env_int("SLOT_DURATION_MINUTES", 30)
AVAILABILITY_DAYS = _env_int("AVAILABILITY_DAYS", 7)
CALENDAR_ID = _env("CALENDAR_ID", "primary")

# ---------------------------------------------------------------------------
# Google OAuth (Calendar + Gmail). Files live under credentials/
# ---------------------------------------------------------------------------
CREDENTIALS_DIR = BASE_DIR / "credentials"
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
GOOGLE_CLIENT_SECRETS = CREDENTIALS_DIR / _env(
    "GOOGLE_CLIENT_SECRETS", "credentials.json"
)
GOOGLE_TOKEN_FILE = CREDENTIALS_DIR / _env("GOOGLE_TOKEN_FILE", "token.json")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

# ---------------------------------------------------------------------------
# Rate limits / robustness
# ---------------------------------------------------------------------------
CHAT_RATE_LIMIT = _env("CHAT_RATE_LIMIT", "30 per minute")
BOOK_RATE_LIMIT = _env("BOOK_RATE_LIMIT", "10 per minute")
MAX_MESSAGE_LENGTH = _env_int("MAX_MESSAGE_LENGTH", 2000)
MAX_HISTORY_MESSAGES = _env_int("MAX_HISTORY_MESSAGES", 20)
SESSION_TTL_HOURS = _env_int("SESSION_TTL_HOURS", 24)
BOOKINGS_FILE = BASE_DIR / "bookings.json"


def parse_hhmm(value: str) -> Tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute). Raises ValueError on bad input."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{value}', expected HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time '{value}'")
    return hour, minute


def setup_logging() -> logging.Logger:
    """Configure root logging once: console + rotating file."""
    logger = logging.getLogger("chatbot")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            LOG_DIR / "app.log",
            maxBytes=2_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Could not open log file; continuing with console only.")

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return logger


def google_ready() -> bool:
    """True when OAuth client secrets + a saved token exist."""
    return GOOGLE_CLIENT_SECRETS.exists() and GOOGLE_TOKEN_FILE.exists()


def llm_ready() -> bool:
    """True when an OpenRouter key is configured."""
    return bool(OPENROUTER_API_KEY)
