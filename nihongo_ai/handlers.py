"""
Nihongo.AI — Telegram Handlers Module

All /command handlers, callback query handlers, and text message handlers.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from .config import (
    ADMIN_CHAT_IDS,
    TIMEZONE,
    COMMAND_COOLDOWN_SECONDS,
    DIFFICULTY_WINDOW,
    HIGH_ACCURACY_THRESHOLD,
    LOW_ACCURACY_THRESHOLD,
    WELCOME_MESSAGE,
    logger,
)
from .models import Quiz
from . import database as db
from . import quiz_generator as qg

# ---------------------------------------------------------------------------
# Anti-spam: per-user last command timestamp
# ---------------------------------------------------------------------------
_last_command: dict[int, float] = {}


def _is_spam(chat_id: int) -> bool:
    """Return True if the user sent a command within the cooldown window."""
    now = time.time()
    last = _last_command.get(chat_id, 0)
    if now - last < COMMAND_COOLDOWN_SECONDS:
        return True
    _last_command[chat_id] = now
    return False


# ---------------------------------------------------------------------------
# Helper: get today's date string
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def _build_answer_keyboard() -> InlineKeyboardMarkup:
    """Build the 1/2/3/4 inline keyboard."""
    buttons = [
        InlineKeyboardButton("1", callback_data="answer_1"),
        InlineKeyboardButton("2", callback_data="answer_2"),
        InlineKeyboardButton("3", callback_data="answer_3"),
        InlineKeyboardButton("4", callback_data="answer_4"),
    ]
    return InlineKeyboardMarkup([buttons])


# ---------------------------------------------------------------------------
# Difficulty adaptation
# ---------------------------------------------------------------------------

def _adapt_difficulty(chat_id: int) -> None:
    """Adjust user difficulty based on recent accuracy."""
    recent = db.get_user_answers_recent(chat_id, limit=DIFFICULTY_WINDOW)
    if len(recent) < DIFFICULTY_WINDOW:
        return  # Not enough data

    correct = sum(1 for a in recent if a.is_correct)
    accuracy = correct / len(recent)

    if accuracy > HIGH_ACCURACY_THRESHOLD:
        db.update_user_difficulty(chat_id, "n4")
        logger.info("User %s difficulty → n4 (accuracy=%.0f%%)", chat_id, accuracy * 100)
    elif accuracy < LOW_ACCURACY_THRESHOLD:
        db.update_user_difficulty(chat_id, "n5")
        logger.info("User %s difficulty → n5 (accuracy=%.0f%%)", chat_id, accuracy * 100)
    else:
        db.update_user_difficulty(chat_id, "mixed")


# ---------------------------------------------------------------------------
# Streak management
# ---------------------------------------------------------------------------

def _update_streak(chat_id: int, quiz_date: str) -> None:
    """Update the user's streak after answering."""
    user = db.get_user(chat_id)
    if not user:
        return

    # Check if they answered yesterday
    yesterday = (datetime.strptime(quiz_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_answer = db.get_answer(chat_id, yesterday)

    if yesterday_answer:
        new_streak = user.streak + 1
    else:
        # Check if this is the first quiz or streak was already 0
        new_streak = 1

    db.update_streak(chat_id, new_streak)


# ---------------------------------------------------------------------------
# Send quiz to a single user
# ---------------------------------------------------------------------------

async def send_quiz_to_user(context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, quiz: Quiz) -> bool:
    """Send a quiz message to a user. Returns True on success."""
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=quiz.full_message,
            reply_markup=_build_answer_keyboard(),
        )
        logger.info("Quiz sent to chat_id=%s date=%s", chat_id, quiz.date)
        return True
    except Exception as e:
        logger.error("Failed to send quiz to chat_id=%s: %s", chat_id, e)
        return False


# ---------------------------------------------------------------------------
# /start command
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — welcome message + send today's quiz immediately."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    user = db.get_or_create_user(chat_id)
    db.update_last_interaction(chat_id)

    # Send welcome
    await update.message.reply_text(WELCOME_MESSAGE)
    logger.info("/start from chat_id=%s", chat_id)

    # Send today's quiz immediately
    today = _today_str()
    quiz = db.get_today_quiz(today)

    if quiz is None:
        # Generate quiz for today
        quiz = qg.generate_quiz_with_fallback(today)
        db.save_today_quiz(quiz)

    # Check if already answered
    existing = db.get_answer(chat_id, today)
    if existing:
        await update.message.reply_text(
            "📌 You've already answered today's quiz! Come back tomorrow for a new one. 📘✨"
        )
        return

    await send_quiz_to_user(context, chat_id, quiz)


# ---------------------------------------------------------------------------
# /today command
# ---------------------------------------------------------------------------

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /today — resend today's quiz."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    today = _today_str()
    quiz = db.get_today_quiz(today)

    if quiz is None:
        quiz = qg.generate_quiz_with_fallback(today)
        db.save_today_quiz(quiz)

    # Check if already answered
    existing = db.get_answer(chat_id, today)
    if existing:
        await update.message.reply_text(
            "📌 You've already answered today's quiz! Come back tomorrow for a new one. 📘✨"
        )
        return

    await send_quiz_to_user(context, chat_id, quiz)
    logger.info("/today from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /stats command
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats — show user statistics."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    user = db.get_or_create_user(chat_id)

    if user.total_answered == 0:
        await update.message.reply_text(
            "📊 You haven't answered any quizzes yet!\n"
            "Type /today to get started. 📘✨"
        )
        return

    accuracy = (user.total_correct / user.total_answered * 100) if user.total_answered > 0 else 0

    msg = (
        f"📊 Your Nihongo.AI Stats\n\n"
        f"✅ Total correct: {user.total_correct}\n"
        f"📝 Total answered: {user.total_answered}\n"
        f"🎯 Accuracy: {accuracy:.0f}%\n"
        f"🔥 Current streak: {user.streak} day(s)\n"
        f"📚 Difficulty level: {user.difficulty.upper()}\n"
    )
    await update.message.reply_text(msg)
    logger.info("/stats from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /level command
# ---------------------------------------------------------------------------

async def level_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /level — show current difficulty level."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    user = db.get_or_create_user(chat_id)

    level_desc = {
        "n5": "N5 (Beginner) — Mostly simple grammar and vocabulary",
        "n4": "N4 (Elementary) — Slightly more complex grammar",
        "mixed": "N5–N4 Mixed — A balanced mix of beginner content",
    }

    msg = (
        f"📚 Your Current Level\n\n"
        f"🎯 {level_desc.get(user.difficulty, 'N5–N4 Mixed')}\n\n"
        f"Your level adjusts automatically based on your recent accuracy.\n"
        f"Keep practicing! 📘✨"
    )
    await update.message.reply_text(msg)
    logger.info("/level from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /pause command
# ---------------------------------------------------------------------------

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause — stop receiving daily quizzes."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    db.set_user_paused(chat_id, True)

    await update.message.reply_text(
        "⏸️ Paused! You won't receive daily quizzes until you type /resume.\n"
        "Take your time — we'll be here when you're ready! 🌸"
    )
    logger.info("/pause from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /resume command
# ---------------------------------------------------------------------------

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — resume daily quizzes."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    db.set_user_paused(chat_id, False)

    await update.message.reply_text(
        "▶️ Resumed! You'll receive daily quizzes again starting tomorrow at 9:00am SGT.\n"
        "Type /today to get today's quiz now! 📘✨"
    )
    logger.info("/resume from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /reminders command
# ---------------------------------------------------------------------------

async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reminders on|off — toggle reminders."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)

    text = (update.message.text or "").strip().lower()

    if text == "/reminders on":
        db.set_reminders_enabled(chat_id, True)
        await update.message.reply_text(
            "🔔 Reminders turned ON! I'll nudge you if you forget to answer. 📘✨"
        )
        logger.info("/reminders on from chat_id=%s", chat_id)
    elif text == "/reminders off":
        db.set_reminders_enabled(chat_id, False)
        await update.message.reply_text(
            "🔕 Reminders turned OFF. You can turn them back on anytime with /reminders on"
        )
        logger.info("/reminders off from chat_id=%s", chat_id)
    else:
        await update.message.reply_text(
            "Usage:\n/reminders on — Enable reminders\n/reminders off — Disable reminders"
        )


# ---------------------------------------------------------------------------
# /help command
# ---------------------------------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — show available commands."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)

    msg = (
        "📖 Nihongo.AI — Commands\n\n"
        "/start — Start the bot & get today's quiz\n"
        "/today — See today's quiz again\n"
        "/stats — View your statistics\n"
        "/level — Check your current difficulty level\n"
        "/pause — Pause daily quizzes\n"
        "/resume — Resume daily quizzes\n"
        "/reminders on — Turn on reminders\n"
        "/reminders off — Turn off reminders\n"
        "/help — Show this help message\n"
        "/delete_my_data — Delete all your data\n\n"
        "Every day at 9:00am SGT, you'll get a new reading passage.\n"
        "Answer by tapping 1/2/3/4 or typing the number! 📘✨"
    )
    await update.message.reply_text(msg)
    logger.info("/help from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /delete_my_data command
# ---------------------------------------------------------------------------

async def delete_my_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete_my_data — delete all user data."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.delete_user(chat_id)

    await update.message.reply_text(
        "🗑️ All your data has been deleted.\n"
        "If you want to start again, just type /start.\n"
        "We hope to see you again! 🌸"
    )
    logger.info("/delete_my_data from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /reset_today (admin only)
# ---------------------------------------------------------------------------

async def reset_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reset_today — admin-only: regenerate today's quiz."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    if chat_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Sorry — this command is not available.")
        return

    if _is_spam(chat_id):
        return

    today = _today_str()

    await update.message.reply_text("🔄 Regenerating today's quiz...")
    logger.info("/reset_today by admin chat_id=%s", chat_id)

    # Delete old quiz + answers
    db.delete_quiz_for_date(today)

    # Generate new
    quiz = qg.generate_quiz_with_fallback(today)
    db.save_today_quiz(quiz)

    # Send to admin
    await send_quiz_to_user(context, chat_id, quiz)

    # Send to all active users (except admin)
    active_users = db.get_active_users()
    sent_count = 0
    for user in active_users:
        if user.chat_id == chat_id:
            continue
        success = await send_quiz_to_user(context, user.chat_id, quiz)
        if success:
            sent_count += 1

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Quiz regenerated and sent to {sent_count + 1} user(s) (including you).",
    )
    logger.info("reset_today complete: sent to %s users", sent_count + 1)


# ---------------------------------------------------------------------------
# Callback query handler (button presses: answer_1, answer_2, etc.)
# ---------------------------------------------------------------------------

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses for quiz answers."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()  # Acknowledge the callback

    chat_id = query.message.chat_id
    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    # Parse answer
    if not query.data.startswith("answer_"):
        return
    try:
        chosen = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        return

    if chosen < 1 or chosen > 4:
        return

    await _process_answer(update, context, chat_id, chosen)


# ---------------------------------------------------------------------------
# Text message handler (typed 1/2/3/4)
# ---------------------------------------------------------------------------

async def text_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle typed answers: 1, 2, 3, or 4."""
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Only handle single digit 1-4
    if text not in ("1", "2", "3", "4"):
        return

    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    chosen = int(text)
    await _process_answer(update, context, chat_id, chosen)


# ---------------------------------------------------------------------------
# Core answer processing
# ---------------------------------------------------------------------------

async def _process_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          chat_id: int, chosen: int) -> None:
    """Process a user's answer to a quiz."""
    today = _today_str()
    now = datetime.now(TIMEZONE)

    # Get today's quiz
    today_quiz = db.get_today_quiz(today)

    # Check if user already answered today
    existing_today = db.get_answer(chat_id, today)

    if today_quiz and existing_today:
        # Already answered today — ignore (answer locking)
        logger.info("Duplicate answer ignored: chat_id=%s date=%s", chat_id, today)
        return

    # If today's quiz exists and not answered → answer today's quiz
    if today_quiz and not existing_today:
        await _record_and_respond(update, context, chat_id, today_quiz, chosen, today)
        return

    # No quiz for today yet — check if there's a previous day's quiz they can answer
    # (late answer: after midnight but before next quiz arrives)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_quiz = db.get_today_quiz(yesterday)

    if yesterday_quiz:
        existing_yesterday = db.get_answer(chat_id, yesterday)
        if not existing_yesterday:
            # Late answer for yesterday's quiz — counts for previous day
            await _record_and_respond(update, context, chat_id, yesterday_quiz, chosen, yesterday)
            return
        else:
            # Already answered yesterday too — old quiz message
            await _send_old_quiz_message(update, context, chat_id)
            return

    # No quiz found at all
    reply_func = _get_reply_func(update)
    await reply_func("📭 No quiz available right now. Please wait for the next one at 9:00am SGT!")


async def _record_and_respond(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              chat_id: int, quiz: Quiz, chosen: int,
                              quiz_date: str) -> None:
    """Record the answer and send explanation."""
    is_correct = chosen == quiz.correct_option

    # Try to record (returns False if duplicate)
    recorded = db.mark_answer(chat_id, quiz_date, chosen, is_correct)
    if not recorded:
        # Duplicate — ignore
        return

    # Update stats
    db.increment_user_stats(chat_id, is_correct)
    _update_streak(chat_id, quiz_date)
    _adapt_difficulty(chat_id)

    # Send explanation
    explanation = qg.format_explanation(quiz, chosen)
    reply_func = _get_reply_func(update)
    await reply_func(explanation)

    logger.info("Answer evaluated: chat_id=%s date=%s chosen=%s correct=%s",
                chat_id, quiz_date, chosen, is_correct)


async def _send_old_quiz_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 chat_id: int) -> None:
    """Send the 'old quiz' message when user tries to answer a past quiz."""
    now = datetime.now(TIMEZONE)
    day_names = ["月", "火", "水", "木", "金", "土", "日"]
    day_name = day_names[now.weekday()]

    msg = (
        "⏳ That quiz is from a previous day, and it's already closed.\n\n"
        f"📅 Today is {now.year}年{now.month}月{now.day}日（{day_name}曜日）\n\n"
        "📌 Please focus on today's passage instead — type /today to see it again! 📘✨"
    )
    reply_func = _get_reply_func(update)
    await reply_func(msg)


def _get_reply_func(update: Update):
    """Get the appropriate reply function based on update type."""
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message.reply_text
    elif update.message:
        return update.message.reply_text
    else:
        # Fallback — shouldn't happen
        async def noop(text):
            pass
        return noop
