"""
Nihongo.AI — Configuration Module

Centralizes all configuration constants, environment variable loading,
and application-wide settings.
"""

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("nihongo_ai")

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Admin chat IDs (comma-separated string → set of ints)
_raw_admin_ids = os.getenv("ADMIN_CHAT_IDS", "")
ADMIN_CHAT_IDS: set[int] = set()
if _raw_admin_ids:
    for part in _raw_admin_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_CHAT_IDS.add(int(part))

# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
GENERATION_TIMEOUT: int = 30  # increased from 15

# ---------------------------------------------------------------------------
# Timezone & Scheduling
# ---------------------------------------------------------------------------
TIMEZONE = ZoneInfo("Asia/Singapore")
QUIZ_HOUR = 9
REMINDER_HOURS = [12, 18, 21]
WEEKLY_SUMMARY_DAY = "fri"
FULL_QUIZ_RETRY_DELAY_MINUTES = 10

# ---------------------------------------------------------------------------
# User Activity
# ---------------------------------------------------------------------------
ACTIVE_DAYS_THRESHOLD = 10

# ---------------------------------------------------------------------------
# Passage / Quiz
# ---------------------------------------------------------------------------
PASSAGE_MIN_CHARS = 250
PASSAGE_MAX_CHARS = 300

FALLBACK_PASSAGE_MIN_CHARS = 200
FALLBACK_PASSAGE_MAX_CHARS = 250

BONUS_PASSAGE_MIN_CHARS = 150
BONUS_PASSAGE_MAX_CHARS = 200

MAX_DAILY_QUIZZES = 3

# ---------------------------------------------------------------------------
# Difficulty adaptation
# ---------------------------------------------------------------------------
DIFFICULTY_WINDOW = 10
HIGH_ACCURACY_THRESHOLD = 0.85
LOW_ACCURACY_THRESHOLD = 0.50

# ---------------------------------------------------------------------------
# Topic rotation
# ---------------------------------------------------------------------------
MAX_TOPIC_REPEAT_IN_14_DAYS = 3

# ---------------------------------------------------------------------------
# Anti-spam
# ---------------------------------------------------------------------------
COMMAND_COOLDOWN_SECONDS = 1.0

# ---------------------------------------------------------------------------
# Retry / Backoff
# ---------------------------------------------------------------------------
RETRY_BACKOFF_MINUTES = [1, 2, 4, 8, 16, 32, 60]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DB_PATH = DATA_DIR / "nihongo_ai.db"

# ---------------------------------------------------------------------------
# Reminder messages
# ---------------------------------------------------------------------------
REMINDER_MESSAGES = {
    12: "⏰ Quick reminder: Today's Nihongo.AI reading is waiting for you! Check it out now!",
    18: "📘 Noticed you haven't completed today's Nihongo.AI reading! There's still time!",
    21: "🌙 Last reminder today to complete your Nihongo.AI reading — keep your streak alive!",
}

# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------
WELCOME_MESSAGE = (
    "👋 Welcome to Nihongo.AI! ようこそ 🌸\n\n"
    "Every day at 9:00am SGT, you'll get a short Japanese passage "
    "(JLPT N5–N4) + a reading question.\n\n"
    "Tap 1 / 2 / 3 / 4 to answer! I'll tell you ✅ correct or ❌ wrong "
    "and explain why — in Japanese, plus a 1-line English explanation "
    "so you can understand.\n\n"
    "If you haven't answered, I'll send gentle reminders to keep you on track.\n\n"
    "Let's go — がんばろう📘✨"
)
