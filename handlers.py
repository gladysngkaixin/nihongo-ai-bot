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
    MAX_DAILY_QUIZZES,
    WELCOME_MESSAGE,
    logger,
)
from .models import Quiz, BonusQuiz
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


def _build_bonus_offer_keyboard() -> InlineKeyboardMarkup:
    """Build the Yes/No inline keyboard for bonus quiz offer."""
    buttons = [
        InlineKeyboardButton("Yes", callback_data="bonus_yes"),
        InlineKeyboardButton("No", callback_data="bonus_no"),
    ]
    return InlineKeyboardMarkup([buttons])


async def _send_bonus_quiz_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, bonus: BonusQuiz) -> None:
    """Send a bonus quiz message to the user."""
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=bonus.full_message,
            reply_markup=_build_answer_keyboard(),
        )
        logger.info("Bonus quiz sent: bonus_id=%s chat_id=%s", bonus.bonus_id, chat_id)
    except Exception as e:
        logger.error("Failed to send bonus quiz to chat_id=%s: %s", chat_id, e)


async def _offer_bonus_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, quizzes_done: int) -> None:
    """Send the bonus quiz offer message based on how many quizzes are done today."""
    reply_func = _get_reply_func(update)
    if quizzes_done == 1:
        msg = (
            "📌 You've already answered today's quiz! "
            "Do you want to do another quiz? Happy to generate a shorter one for you!"
        )
    else:  # quizzes_done == 2
        msg = (
            "📌 Wow, you've completed 2 quizzes today! "
            "Do you want to do another one? Happy to generate a 3rd quiz for you!"
        )
    await reply_func(msg, reply_markup=_build_bonus_offer_keyboard())


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
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.get_or_create_user(chat_id)
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
        # Daily quiz done — check bonus quiz state
        total_done = db.count_quizzes_today(chat_id, today)
        if total_done >= MAX_DAILY_QUIZZES:
            await update.message.reply_text(
                "📌 Good job, you've done 3 quizzes today! "
                "Nihongo AI will now take a break, and you should as well. "
                "See you tomorrow again! 📘✨"
            )
        else:
            active_bonus = db.get_active_bonus_quiz(chat_id, today)
            if active_bonus:
                await _send_bonus_quiz_to_user(update, context, chat_id, active_bonus)
            else:
                await _offer_bonus_quiz(update, context, chat_id, total_done)
        return

    await send_quiz_to_user(context, chat_id, quiz)


# ---------------------------------------------------------------------------
# /today command
# ---------------------------------------------------------------------------

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /today — resend today's quiz or show bonus offer."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    today = _today_str()

    # Check total quizzes done today
    total_done = db.count_quizzes_today(chat_id, today)

    # Scenario: three quizzes completed — show max reached message
    if total_done >= MAX_DAILY_QUIZZES:
        await update.message.reply_text(
            "📌 Good job, you've done 3 quizzes today! "
            "Nihongo AI will now take a break, and you should as well. "
            "See you tomorrow again! 📘✨"
        )
        logger.info("/today from chat_id=%s — max quizzes reached", chat_id)
        return

    # Scenario: daily quiz completed, bonus available
    existing_main = db.get_answer(chat_id, today)
    if existing_main:
        active_bonus = db.get_active_bonus_quiz(chat_id, today)
        if active_bonus:
            # Resend the existing unanswered bonus quiz
            await _send_bonus_quiz_to_user(update, context, chat_id, active_bonus)
            logger.info("/today resent active bonus quiz: bonus_id=%s chat_id=%s",
                        active_bonus.bonus_id, chat_id)
        else:
            # Offer a new bonus quiz
            await _offer_bonus_quiz(update, context, chat_id, total_done)
            logger.info("/today from chat_id=%s — offering bonus quiz", chat_id)
        return

    # Scenario: daily quiz not answered — show/resend today's daily quiz
    quiz = db.get_today_quiz(today)
    if quiz is None:
        quiz = qg.generate_quiz_with_fallback(today)
        db.save_today_quiz(quiz)

    await send_quiz_to_user(context, chat_id, quiz)
    logger.info("/today from chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# /stats command
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats — show user statistics."""
    if not update.effective_chat or not update.message:
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
    if not update.effective_chat or not update.message:
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
    if not update.effective_chat or not update.message:
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
    if not update.effective_chat or not update.message:
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
    if not update.effective_chat or not update.message:
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
    if not update.effective_chat or not update.message:
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
    """Handle /reset_today — admin-only: generate a fresh quiz preview for the admin.

    This command generates a brand-new quiz preview and sends it only to the admin.
    It does NOT modify the globally stored daily quiz, does NOT delete any
    answer records, and does NOT affect any other user's state or bonus quizzes.
    The preview is review-only and is not answerable.
    """
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if chat_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Sorry — this command is not available.")
        return

    if _is_spam(chat_id):
        return

    today = _today_str()

    await update.message.reply_text("⏳ Generating a fresh quiz preview for you...")
    logger.info("/reset_today preview requested by admin chat_id=%s", chat_id)

    # Generate a fresh quiz without touching the globally stored daily quiz.
    # db.delete_quiz_for_date and db.save_today_quiz are intentionally NOT called
    # so that other users' quiz state and answer records remain completely unaffected.
    quiz = qg.generate_quiz_with_fallback(today)

    # Send only to the admin — as a preview only, with NO answer buttons.
    await context.bot.send_message(
        chat_id=chat_id,
        text=quiz.full_message,
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ A fresh quiz preview has been generated and sent only to you. This preview is for review only.",
    )
    logger.info("/reset_today complete: preview sent to admin chat_id=%s only", chat_id)


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
        # Main daily quiz already answered.
        # Check if there is an active (unanswered) bonus quiz for today.
        active_bonus = db.get_active_bonus_quiz(chat_id, today)
        if active_bonus:
            await _process_bonus_answer(update, context, chat_id, chosen)
        else:
            # No active bonus quiz — check whether any completed bonus quiz exists.
            # If so, the user is tapping buttons from an already-completed quiz.
            completed_bonuses = db.get_bonus_quizzes_for_day(chat_id, today)
            if any(b.is_answered for b in completed_bonuses):
                reply_func = _get_reply_func(update)
                await reply_func("📌 This quiz has already been completed.")
                logger.info("Stale bonus button press ignored: chat_id=%s date=%s",
                            chat_id, today)
            else:
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

    # Offer a bonus quiz only when the quiz answered is today's main daily quiz.
    # Late answers to yesterday's quiz must never trigger the bonus quiz flow.
    today = _today_str()
    if quiz_date == today:
        total_done = db.count_quizzes_today(chat_id, quiz_date)
        if total_done < MAX_DAILY_QUIZZES:
            await _offer_bonus_quiz(update, context, chat_id, total_done)


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
        async def noop(text, **kwargs):
            pass
        return noop


# ---------------------------------------------------------------------------
# Bonus quiz callback handler (bonus_yes / bonus_no)
# ---------------------------------------------------------------------------

async def bonus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bonus_yes / bonus_no inline button presses."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    chat_id = query.message.chat_id
    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    today = _today_str()

    if query.data == "bonus_no":
        await query.message.reply_text(
            "📌 That's alright as you have finished today's quiz! "
            "See you tomorrow for a new one. 📘✨"
        )
        logger.info("bonus_no: chat_id=%s", chat_id)
        return

    if query.data != "bonus_yes":
        return

    # bonus_yes — check if max quizzes already reached
    total_done = db.count_quizzes_today(chat_id, today)
    if total_done >= MAX_DAILY_QUIZZES:
        await query.message.reply_text(
            "📌 Good job, you've done 3 quizzes today! "
            "Nihongo AI will now take a break, and you should as well. "
            "See you tomorrow again! 📘✨"
        )
        logger.info("bonus_yes blocked — max quizzes: chat_id=%s", chat_id)
        return

    # Check if there's already an active unanswered bonus quiz
    active_bonus = db.get_active_bonus_quiz(chat_id, today)
    if active_bonus:
        # Resend existing unanswered bonus quiz instead of generating a new one
        await context.bot.send_message(
            chat_id=chat_id,
            text=active_bonus.full_message,
            reply_markup=_build_answer_keyboard(),
        )
        logger.info("bonus_yes resent existing bonus: bonus_id=%s chat_id=%s",
                    active_bonus.bonus_id, chat_id)
        return

    # Determine quiz_type and sequence for the new bonus quiz
    bonus_quizzes_today = db.get_bonus_quizzes_for_day(chat_id, today)
    answered_bonus_count = sum(1 for b in bonus_quizzes_today if b.is_answered)

    if answered_bonus_count == 0:
        quiz_type = "bonus_1"
        quiz_sequence_for_day = 2
    elif answered_bonus_count == 1:
        quiz_type = "bonus_2"
        quiz_sequence_for_day = 3
    else:
        # Should not reach here due to MAX_DAILY_QUIZZES check above
        await query.message.reply_text(
            "📌 Good job, you've done 3 quizzes today! "
            "Nihongo AI will now take a break, and you should as well. "
            "See you tomorrow again! 📘✨"
        )
        return

    # Generate the bonus quiz
    await query.message.reply_text("⏳ Generating your bonus quiz... please wait!")
    bonus = qg.generate_bonus_quiz(today, chat_id, quiz_type, quiz_sequence_for_day)

    if bonus is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🙇 Sorry — I couldn't generate a bonus quiz right now. Please try again later!",
        )
        logger.error("Bonus quiz generation failed for chat_id=%s", chat_id)
        return

    db.save_bonus_quiz(bonus)

    await context.bot.send_message(
        chat_id=chat_id,
        text=bonus.full_message,
        reply_markup=_build_answer_keyboard(),
    )
    logger.info("bonus_quiz_generated and sent: bonus_id=%s chat_id=%s",
                bonus.bonus_id, chat_id)


# ---------------------------------------------------------------------------
# Bonus answer processing
# ---------------------------------------------------------------------------

async def _process_bonus_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                chat_id: int, chosen: int) -> None:
    """Process a user's answer to an active bonus quiz."""
    today = _today_str()
    active_bonus = db.get_active_bonus_quiz(chat_id, today)

    if active_bonus is None:
        return  # No active bonus quiz to answer

    is_correct = chosen == active_bonus.correct_option
    recorded = db.mark_bonus_answer(active_bonus.bonus_id, chat_id, chosen, is_correct)
    if not recorded:
        return  # Already answered

    # Send explanation (bonus quizzes do NOT affect streak/stats/difficulty)
    explanation = qg.format_bonus_explanation(active_bonus, chosen)
    reply_func = _get_reply_func(update)
    await reply_func(explanation)

    logger.info("bonus_quiz_answered: bonus_id=%s chat_id=%s chosen=%s correct=%s",
                active_bonus.bonus_id, chat_id, chosen, is_correct)

    # Check if user can do more quizzes
    total_done = db.count_quizzes_today(chat_id, today)
    if total_done >= MAX_DAILY_QUIZZES:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "📌 Good job, you've done 3 quizzes today! "
                "Nihongo AI will now take a break, and you should as well. "
                "See you tomorrow again! 📘✨"
            ),
        )
    else:
        # Offer another bonus quiz
        await _offer_bonus_quiz(update, context, chat_id, total_done)
