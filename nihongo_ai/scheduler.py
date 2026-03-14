"""
Nihongo.AI — Scheduler Module

Uses APScheduler to manage:
  - Daily quiz generation & delivery (9:00am SGT)
  - Reminders (12pm, 6pm, 9pm SGT)
  - Weekly summary (Friday evening)
  - Retry logic with exponential backoff
  - Fallback retry (10 min after fallback quiz)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from .config import (
    TIMEZONE,
    QUIZ_HOUR,
    REMINDER_HOURS,
    REMINDER_MESSAGES,
    WEEKLY_SUMMARY_DAY,
    RETRY_BACKOFF_MINUTES,
    logger,
)
from .models import Quiz, WeeklyStats
from . import database as db
from . import quiz_generator as qg
from .handlers import send_quiz_to_user

# ---------------------------------------------------------------------------
# Module-level reference to the Telegram application
# ---------------------------------------------------------------------------
_app: Optional[Application] = None
_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the singleton scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    return _scheduler


def setup_scheduler(app: Application) -> AsyncIOScheduler:
    """Configure and return the scheduler with all jobs."""
    global _app
    _app = app

    scheduler = get_scheduler()

    # Daily quiz at 9:00am SGT
    scheduler.add_job(
        daily_quiz_job,
        CronTrigger(hour=QUIZ_HOUR, minute=0, timezone=TIMEZONE),
        id="daily_quiz",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Reminders at 12pm, 6pm, 9pm SGT
    for hour in REMINDER_HOURS:
        scheduler.add_job(
            reminder_job,
            CronTrigger(hour=hour, minute=0, timezone=TIMEZONE),
            id=f"reminder_{hour}",
            replace_existing=True,
            args=[hour],
            misfire_grace_time=3600,
        )

    # Weekly summary — Friday at 8:00pm SGT
    scheduler.add_job(
        weekly_summary_job,
        CronTrigger(day_of_week=WEEKLY_SUMMARY_DAY, hour=20, minute=0, timezone=TIMEZONE),
        id="weekly_summary",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info("Scheduler configured with daily quiz, reminders, and weekly summary jobs")
    return scheduler


# ---------------------------------------------------------------------------
# Daily Quiz Job
# ---------------------------------------------------------------------------

async def daily_quiz_job() -> None:
    """Generate and send today's quiz to all active users."""
    if _app is None:
        logger.error("App not initialized for daily_quiz_job")
        return

    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    logger.info("Daily quiz job started for %s", today)

    # Check if quiz already exists (e.g., from /reset_today)
    quiz = db.get_today_quiz(today)
    if quiz is None:
        quiz = qg.generate_quiz_with_fallback(today)
        db.save_today_quiz(quiz)
        logger.info("Quiz generated for %s", today)

    # Schedule fallback retry if this was a fallback quiz
    if quiz.is_fallback:
        _schedule_fallback_retry(today)

    # Send to all active users
    active_users = db.get_active_users()
    logger.info("Sending quiz to %d active users", len(active_users))

    success_count = 0
    fail_list: list[int] = []

    for user in active_users:
        success = await send_quiz_to_user(_app, user.chat_id, quiz)
        if success:
            success_count += 1
        else:
            fail_list.append(user.chat_id)

    logger.info("Quiz sent: %d success, %d failed", success_count, len(fail_list))

    # Schedule retries for failed sends
    if fail_list:
        _schedule_send_retries(fail_list, quiz, attempt=0)


# ---------------------------------------------------------------------------
# Retry logic with exponential backoff
# ---------------------------------------------------------------------------

def _schedule_send_retries(chat_ids: list[int], quiz: Quiz, attempt: int) -> None:
    """Schedule retry sends with exponential backoff."""
    if attempt >= len(RETRY_BACKOFF_MINUTES):
        logger.error("Max retries reached for %d users", len(chat_ids))
        return

    delay_minutes = RETRY_BACKOFF_MINUTES[attempt]
    run_time = datetime.now(TIMEZONE) + timedelta(minutes=delay_minutes)

    scheduler = get_scheduler()
    job_id = f"retry_send_{quiz.date}_{attempt}"

    scheduler.add_job(
        _retry_send_job,
        "date",
        run_date=run_time,
        id=job_id,
        replace_existing=True,
        args=[chat_ids, quiz, attempt],
    )
    logger.info("Scheduled retry #%d in %d min for %d users",
                attempt + 1, delay_minutes, len(chat_ids))


async def _retry_send_job(chat_ids: list[int], quiz: Quiz, attempt: int) -> None:
    """Retry sending quiz to failed users."""
    if _app is None:
        return

    still_failed: list[int] = []
    for chat_id in chat_ids:
        success = await send_quiz_to_user(_app, chat_id, quiz)
        if not success:
            still_failed.append(chat_id)

    if still_failed:
        _schedule_send_retries(still_failed, quiz, attempt + 1)
    else:
        logger.info("All retries succeeded for attempt #%d", attempt + 1)


# ---------------------------------------------------------------------------
# Fallback retry (10 min after fallback quiz)
# ---------------------------------------------------------------------------

def _schedule_fallback_retry(date_str: str) -> None:
    """If a fallback quiz was sent, try generating the full version in 10 min."""
    run_time = datetime.now(TIMEZONE) + timedelta(minutes=10)
    scheduler = get_scheduler()

    scheduler.add_job(
        _fallback_retry_job,
        "date",
        run_date=run_time,
        id=f"fallback_retry_{date_str}",
        replace_existing=True,
        args=[date_str],
    )
    logger.info("Scheduled fallback retry in 10 min for %s", date_str)


async def _fallback_retry_job(date_str: str) -> None:
    """Try generating the full quiz after a fallback was sent."""
    if _app is None:
        return

    existing = db.get_today_quiz(date_str)
    if existing and not existing.is_fallback:
        return  # Already replaced

    quiz = qg.generate_quiz(date_str=date_str, is_fallback=False)
    if quiz is None:
        logger.warning("Fallback retry also failed for %s", date_str)
        return

    db.save_today_quiz(quiz)
    logger.info("Full quiz generated on fallback retry for %s", date_str)

    # Send to users who haven't answered yet
    unanswered = db.get_unanswered_users(date_str)
    for chat_id in unanswered:
        try:
            await _app.bot.send_message(
                chat_id=chat_id,
                text="📖 Good news! Here's the full version of today's passage:",
            )
            from .handlers import _build_answer_keyboard
            await _app.bot.send_message(
                chat_id=chat_id,
                text=quiz.full_message,
                reply_markup=_build_answer_keyboard(),
            )
        except Exception as e:
            logger.error("Failed to send fallback retry to %s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Reminder Job
# ---------------------------------------------------------------------------

async def reminder_job(hour: int) -> None:
    """Send reminders to users who haven't answered today's quiz."""
    if _app is None:
        logger.error("App not initialized for reminder_job")
        return

    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    message = REMINDER_MESSAGES.get(hour, "⏰ Don't forget today's Nihongo.AI reading!")

    # Get users who haven't answered
    unanswered = db.get_unanswered_users(today)
    logger.info("Reminder @%d:00 — %d unanswered users", hour, len(unanswered))

    for chat_id in unanswered:
        try:
            await _app.bot.send_message(chat_id=chat_id, text=message)
            logger.info("Reminder sent to chat_id=%s @%d:00", chat_id, hour)
        except Exception as e:
            logger.error("Failed to send reminder to chat_id=%s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Weekly Summary Job (Friday)
# ---------------------------------------------------------------------------

async def weekly_summary_job() -> None:
    """Send weekly summary to all active users on Friday."""
    if _app is None:
        logger.error("App not initialized for weekly_summary_job")
        return

    now = datetime.now(TIMEZONE)
    # Calculate the week range (Monday to Friday)
    # Friday is weekday 4
    end_date = now.strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=4)).strftime("%Y-%m-%d")  # Monday

    active_users = db.get_active_users()
    logger.info("Weekly summary for %d active users (%s to %s)",
                len(active_users), start_date, end_date)

    for user in active_users:
        try:
            answers = db.get_weekly_answers(user.chat_id, start_date, end_date)
            msg = _format_weekly_summary(user.chat_id, answers, user.streak)
            await _app.bot.send_message(chat_id=user.chat_id, text=msg)
            logger.info("Weekly summary sent to chat_id=%s", user.chat_id)
        except Exception as e:
            logger.error("Failed to send weekly summary to chat_id=%s: %s",
                         user.chat_id, e)


def _format_weekly_summary(chat_id: int, answers: list, streak: int) -> str:
    """Format the weekly summary message."""
    if not answers:
        return (
            "📊 Weekly Summary\n\n"
            "Looks like it was a quiet week — no quizzes completed yet. "
            "Type /today to get back into the habit!"
        )

    total = len(answers)
    correct = sum(1 for a in answers if a.is_correct)
    accuracy = (correct / total * 100) if total > 0 else 0

    # Analyze common mistakes
    mistake_types: dict[str, int] = {}
    for a in answers:
        if not a.is_correct and a.question_type:
            mistake_types[a.question_type] = mistake_types.get(a.question_type, 0) + 1

    # Map question types to readable categories
    type_labels = {
        "main_idea": "main idea questions",
        "detail_comprehension": "detail misreading",
        "inference": "inference mistakes",
        "vocabulary_in_context": "vocabulary misunderstanding",
        "pronoun_reference": "pronoun reference questions",
    }

    common_mistakes = []
    for qt, count in sorted(mistake_types.items(), key=lambda x: -x[1])[:3]:
        common_mistakes.append(type_labels.get(qt, qt))

    if not common_mistakes:
        common_mistakes = ["None — great job!"]

    # Focus points
    focus_points = []
    if "vocabulary misunderstanding" in common_mistakes:
        focus_points.append("Review vocabulary from recent passages")
    if "inference mistakes" in common_mistakes:
        focus_points.append("Practice reading between the lines")
    if "detail misreading" in common_mistakes:
        focus_points.append("Read passages more carefully for specific details")
    if not focus_points:
        focus_points.append("Keep up the great work!")

    msg = (
        "🎉 Congrats on another week of Japanese practice with Nihongo.AI! "
        "Here's your weekly summary:\n\n"
        f"✅ Accuracy: {accuracy:.0f}%\n"
        f"🔥 Current streak: {streak} day(s)\n"
        f"🧠 Most common mistakes: {', '.join(common_mistakes)}\n"
        f"🎯 Recommended focus next week: {'; '.join(focus_points)}\n\n"
        "Keep going — small steps every day add up fast! 🇯🇵✨\n"
        "We're here to learn together! 🤝"
    )
    return msg
