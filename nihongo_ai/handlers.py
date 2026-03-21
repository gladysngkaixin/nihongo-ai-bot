"""
Nihongo.AI — Telegram Handlers Module

All /command handlers, callback query handlers, and text message handlers.
"""

from __future__ import annotations
import asyncio
import time
from datetime import datetime, timedelta

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
from .quiz_generator import format_quiz_message_split

_last_command: dict[int, float] = {}


def _is_spam(chat_id: int) -> bool:
    now = time.time()
    last = _last_command.get(chat_id, 0)
    if now - last < COMMAND_COOLDOWN_SECONDS:
        return True
    _last_command[chat_id] = now
    return False


def _today_str() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def _build_answer_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("1", callback_data="answer_1"),
        InlineKeyboardButton("2", callback_data="answer_2"),
        InlineKeyboardButton("3", callback_data="answer_3"),
        InlineKeyboardButton("4", callback_data="answer_4"),
    ]
    return InlineKeyboardMarkup([buttons])


def _build_bonus_offer_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("Yes", callback_data="bonus_yes"),
        InlineKeyboardButton("No", callback_data="bonus_no"),
    ]
    return InlineKeyboardMarkup([buttons])


async def _send_bonus_quiz_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, bonus: BonusQuiz) -> None:
    try:
        # Bonus passages are shorter but we split anyway for consistency
        # and to guarantee the keyboard always appears
        await context.bot.send_message(chat_id=chat_id, text=bonus.full_message)
        await context.bot.send_message(
            chat_id=chat_id,
            text="👉 答えを選んでね：（1 / 2 / 3 / 4 ボタン）",
            reply_markup=_build_answer_keyboard(),
        )
        logger.info("Bonus quiz sent: bonus_id=%s chat_id=%s", bonus.bonus_id, chat_id)
    except Exception as e:
        logger.error("Failed to send bonus quiz to chat_id=%s: %s", chat_id, e)


async def _offer_bonus_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, quizzes_done: int) -> None:
    reply_func = _get_reply_func(update)
    if quizzes_done == 1:
        msg = (
            "📌 You've already answered today's quiz! "
            "Do you want to do another quiz? Happy to generate a shorter one for you!"
        )
    else:
        msg = (
            "📌 Wow, you've completed 2 quizzes today! "
            "Do you want to do another one? Happy to generate a 3rd quiz for you!"
        )
    await reply_func(msg, reply_markup=_build_bonus_offer_keyboard())


def _adapt_difficulty(chat_id: int) -> None:
    """
    Adjust user difficulty based on recent accuracy.

    BUG FIX #3: Previously used dict subscript a["is_correct"] on Answer
    dataclass objects, which raises TypeError on every call after 10 answers.
    Fixed to use attribute access a.is_correct.
    """
    recent = db.get_user_answers_recent(chat_id, limit=DIFFICULTY_WINDOW)
    if len(recent) < DIFFICULTY_WINDOW:
        return

    # BUG FIX #3: was a["is_correct"] — Answer is a dataclass, not a dict
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


def _update_streak(chat_id: int, quiz_date: str) -> None:
    """
    Update streak after a quiz answer.

    W1 FIX: The previous S1 fix introduced a regression. The condition
    'yesterday_answer OR quiz_date == today' caused quiz_date == today to
    always be True for normal answers, meaning a user who missed 5 days
    and then answered still got streak+1 instead of reset to 1.

    Correct logic, handling both cases cleanly:
    - Normal answer (quiz_date == today): check if yesterday was answered.
      If yes → streak+1. If no → reset to 1.
    - Late answer (quiz_date == yesterday): check if the day before
      quiz_date was answered. If yes → streak+1. If no → reset to 1.
    This correctly resets streaks after missed days in both cases.
    """
    user = db.get_user(chat_id)
    if not user:
        return

    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    if quiz_date == today:
        # Normal answer — chain depends on whether yesterday was answered
        yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
        prior_answer = db.get_answer(chat_id, yesterday)
    else:
        # Late answer for a previous date — chain depends on the day before that date
        day_before = (
            datetime.strptime(quiz_date, "%Y-%m-%d") - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        prior_answer = db.get_answer(chat_id, day_before)

    if prior_answer:
        new_streak = user.streak + 1
    else:
        new_streak = 1

    db.update_streak(chat_id, new_streak)


async def send_quiz_to_user(context, chat_id: int, quiz: Quiz) -> bool:
    try:
        passage_msg, question_msg = format_quiz_message_split(quiz, quiz.date)
        # Send passage first (no keyboard — avoids Telegram's 4096-char limit
        # which silently drops inline keyboards on long messages)
        await context.bot.send_message(chat_id=chat_id, text=passage_msg)
        # Send question + options + keyboard as a short second message
        await context.bot.send_message(
            chat_id=chat_id,
            text=question_msg,
            reply_markup=_build_answer_keyboard(),
        )
        logger.info("Quiz sent to chat_id=%s date=%s", chat_id, quiz.date)
        return True
    except Exception as e:
        logger.error("Failed to send quiz to chat_id=%s: %s", chat_id, e)
        return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.get_or_create_user(chat_id)
    db.update_last_interaction(chat_id)

    await update.message.reply_text(WELCOME_MESSAGE)
    logger.info("/start from chat_id=%s", chat_id)

    today = _today_str()
    quiz = db.get_today_quiz(today)
    if quiz is None:
        # ISSUE FIX #11: With 90s timeout × up to 5 attempts, worst-case generation
        # is ~7.5 minutes. Send an immediate holding message so the user knows
        # the bot is working and hasn't frozen.
        await update.message.reply_text("⏳ Generating today's quiz, please wait a moment...")
        quiz = await asyncio.to_thread(qg.generate_quiz_with_fallback, today)
        db.save_today_quiz(quiz)

    existing = db.get_answer(chat_id, today)
    if existing:
        total_done = db.count_quizzes_today(chat_id, today)
        if total_done >= MAX_DAILY_QUIZZES:
            await update.message.reply_text(
                "📌 Good job, you've done 3 quizzes today! "
                "Nihongo AI will now take a break, and you should as well. "
                "See you tomorrow again! 📘✨"
            )
        else:
            active_bonus = db.get_active_bonus_quiz(chat_id, today)
            # H4 FIX: guard against stale bonus from a previous day (e.g. after pause+resume)
            if active_bonus and active_bonus.date == today:
                await _send_bonus_quiz_to_user(update, context, chat_id, active_bonus)
            else:
                await _offer_bonus_quiz(update, context, chat_id, total_done)
        return

    await send_quiz_to_user(context, chat_id, quiz)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    today = _today_str()
    total_done = db.count_quizzes_today(chat_id, today)

    if total_done >= MAX_DAILY_QUIZZES:
        await update.message.reply_text(
            "📌 Good job, you've done 3 quizzes today! "
            "Nihongo AI will now take a break, and you should as well. "
            "See you tomorrow again! 📘✨"
        )
        logger.info("/today from chat_id=%s — max quizzes reached", chat_id)
        return

    existing_main = db.get_answer(chat_id, today)
    if existing_main:
        active_bonus = db.get_active_bonus_quiz(chat_id, today)
        # W3 FIX: get_active_bonus_quiz already filters by date=today, so this
        # is consistent. But adding explicit .date == today guard as safety net
        # against stale bonus quizzes surfacing after pause+resume across days.
        if active_bonus and active_bonus.date == today:
            await _send_bonus_quiz_to_user(update, context, chat_id, active_bonus)
            logger.info("/today resent active bonus quiz: bonus_id=%s chat_id=%s",
                        active_bonus.bonus_id, chat_id)
        else:
            await _offer_bonus_quiz(update, context, chat_id, total_done)
            logger.info("/today from chat_id=%s — offering bonus quiz", chat_id)
        return

    quiz = db.get_today_quiz(today)
    if quiz is None:
        # ISSUE FIX #11: send holding message before potentially slow generation
        await update.message.reply_text("⏳ Generating today's quiz, please wait a moment...")
        quiz = await asyncio.to_thread(qg.generate_quiz_with_fallback, today)
        db.save_today_quiz(quiz)

    await send_quiz_to_user(context, chat_id, quiz)
    logger.info("/today from chat_id=%s", chat_id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def level_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    user = db.get_or_create_user(chat_id)
    # H8 FIX: check current state so typing /pause twice gives a sensible response
    if user.paused:
        await update.message.reply_text(
            "⏸️ You're already paused. Type /resume whenever you're ready! 🌸"
        )
        return
    db.set_user_paused(chat_id, True)
    await update.message.reply_text(
        "⏸️ Paused! You won't receive daily quizzes until you type /resume.\n"
        "Take your time — we'll be here when you're ready! 🌸"
    )
    logger.info("/pause from chat_id=%s", chat_id)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.update_last_interaction(chat_id)
    user = db.get_or_create_user(chat_id)
    # H8 FIX: check current state
    if not user.paused:
        await update.message.reply_text(
            "▶️ You're not paused — quizzes are already active! Type /today to get today's quiz. 📘✨"
        )
        return
    db.set_user_paused(chat_id, False)
    await update.message.reply_text(
        "▶️ Resumed! You'll receive daily quizzes again starting tomorrow at 9:00am SGT.\n"
        "Type /today to get today's quiz now! 📘✨"
    )
    logger.info("/resume from chat_id=%s", chat_id)


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def delete_my_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    if _is_spam(chat_id):
        return

    db.delete_user(chat_id)

    # A5 FIX: clear the in-memory spam entry so /start works immediately after
    # deletion without hitting the 1-second cooldown. Also prevents the dict
    # from holding entries for users who have deleted their account.
    _last_command.pop(chat_id, None)

    await update.message.reply_text(
        "🗑️ All your data has been deleted.\n"
        "If you want to start again, just type /start.\n"
        "We hope to see you again! 🌸"
    )
    logger.info("/delete_my_data from chat_id=%s", chat_id)


async def reset_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: generate a fresh quiz preview sent only to the admin.

    Does NOT touch the globally stored quiz, other users' answers, streaks,
    or bonus quizzes. Preview only — no answer buttons.
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

    # X3 FIX: previously called generate_quiz_with_fallback() which, when a
    # fallback is returned, internally schedules _schedule_fallback_retry().
    # That retry job fires 10 minutes later and replaces the real stored quiz,
    # then sends it to all unanswered users — a real user-facing action triggered
    # by what was supposed to be a preview-only admin command.
    #
    # Fix: call generate_quiz() directly (single attempt, no retry scheduling).
    # If that fails, fall back to _hardcoded_fallback() locally — no scheduler
    # side-effects in either case.
    quiz = await asyncio.to_thread(qg.generate_quiz, today)
    if quiz is None:
        quiz = qg._hardcoded_fallback(today)

    # Send to admin only, no answer buttons (preview only).
    # Split into two messages so the long furigana passage doesn't get truncated.
    passage_msg, question_msg = format_quiz_message_split(quiz, quiz.date)
    await context.bot.send_message(chat_id=chat_id, text=passage_msg)
    await context.bot.send_message(chat_id=chat_id, text=question_msg)
    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ Fresh quiz preview generated and sent only to you. This is for review only.",
    )
    logger.info("/reset_today complete: preview sent to admin chat_id=%s only", chat_id)


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.message:
        return

    # W6 FIX: only process answer button presses in private chats,
    # consistent with the text_answer_handler guard. Prevents phantom
    # answer records if a quiz message is forwarded to a group.
    if query.message.chat.type != "private":
        await query.answer()
        return

    await query.answer()

    chat_id = query.message.chat_id
    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    if not query.data.startswith("answer_"):
        return

    try:
        chosen = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        return

    if chosen < 1 or chosen > 4:
        return

    await _process_answer(update, context, chat_id, chosen)


async def text_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_chat:
        return

    # A4 FIX: ignore typed answers in group/supergroup/channel chats.
    # Without this, typing '1'-'4' in any group the bot is in triggers a
    # confusing "No quiz available" response.
    if update.effective_chat.type != "private":
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text not in ("1", "2", "3", "4"):
        return

    db.update_last_interaction(chat_id)
    db.get_or_create_user(chat_id)

    chosen = int(text)
    await _process_answer(update, context, chat_id, chosen)


async def _process_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          chat_id: int, chosen: int) -> None:
    today = _today_str()
    now = datetime.now(TIMEZONE)

    today_quiz = db.get_today_quiz(today)
    existing_today = db.get_answer(chat_id, today)

    if today_quiz and existing_today:
        active_bonus = db.get_active_bonus_quiz(chat_id, today)
        if active_bonus:
            await _process_bonus_answer(update, context, chat_id, chosen)
        else:
            completed_bonuses = db.get_bonus_quizzes_for_day(chat_id, today)
            if any(b.is_answered for b in completed_bonuses):
                reply_func = _get_reply_func(update)
                await reply_func("📌 This quiz has already been completed.")
                logger.info("Stale bonus button press ignored: chat_id=%s date=%s",
                            chat_id, today)
            else:
                logger.info("Duplicate answer ignored: chat_id=%s date=%s", chat_id, today)
        return

    if today_quiz and not existing_today:
        await _record_and_respond(update, context, chat_id, today_quiz, chosen, today)
        return

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_quiz = db.get_today_quiz(yesterday)

    if yesterday_quiz:
        existing_yesterday = db.get_answer(chat_id, yesterday)
        if not existing_yesterday:
            await _record_and_respond(update, context, chat_id, yesterday_quiz, chosen, yesterday)
            return
        else:
            await _send_old_quiz_message(update, context, chat_id)
            return

    reply_func = _get_reply_func(update)
    await reply_func("📭 No quiz available right now. Please wait for the next one at 9:00am SGT!")


async def _record_and_respond(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              chat_id: int, quiz: Quiz, chosen: int,
                              quiz_date: str) -> None:
    is_correct = chosen == quiz.correct_option

    recorded = db.mark_answer(chat_id, quiz_date, chosen, is_correct, quiz.question_type)
    if not recorded:
        return

    db.increment_user_stats(chat_id, is_correct)
    _update_streak(chat_id, quiz_date)
    _adapt_difficulty(chat_id)

    explanation = qg.format_explanation(quiz, chosen)
    reply_func = _get_reply_func(update)
    await reply_func(explanation)

    logger.info("Answer evaluated: chat_id=%s date=%s chosen=%s correct=%s",
                chat_id, quiz_date, chosen, is_correct)

    today = _today_str()
    if quiz_date == today:
        total_done = db.count_quizzes_today(chat_id, quiz_date)
        if total_done < MAX_DAILY_QUIZZES:
            await _offer_bonus_quiz(update, context, chat_id, total_done)


async def _send_old_quiz_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 chat_id: int) -> None:
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
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message.reply_text
    if update.message:
        return update.message.reply_text

    async def noop(text, **kwargs):
        return None

    return noop


async def bonus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.message:
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

    # Check for an existing unanswered bonus quiz before anything else
    active_bonus = db.get_active_bonus_quiz(chat_id, today)
    # H5 FIX: explicit date guard for consistency with today_command and start_command
    if active_bonus and active_bonus.date == today:
        await context.bot.send_message(chat_id=chat_id, text=active_bonus.full_message)
        await context.bot.send_message(
            chat_id=chat_id,
            text="👉 答えを選んでね：（1 / 2 / 3 / 4 ボタン）",
            reply_markup=_build_answer_keyboard(),
        )
        logger.info("bonus_yes resent existing bonus: bonus_id=%s chat_id=%s",
                    active_bonus.bonus_id, chat_id)
        return

    total_done = db.count_quizzes_today(chat_id, today)
    if total_done >= MAX_DAILY_QUIZZES:
        await query.message.reply_text(
            "📌 Good job, you've done 3 quizzes today! "
            "Nihongo AI will now take a break, and you should as well. "
            "See you tomorrow again! 📘✨"
        )
        logger.info("bonus_yes blocked — max quizzes: chat_id=%s", chat_id)
        return

    bonus_quizzes_today = db.get_bonus_quizzes_for_day(chat_id, today)
    answered_bonus_count = sum(1 for b in bonus_quizzes_today if b.is_answered)

    if answered_bonus_count == 0:
        quiz_type = "bonus_1"
        quiz_sequence_for_day = 2
    elif answered_bonus_count == 1:
        quiz_type = "bonus_2"
        quiz_sequence_for_day = 3
    else:
        await query.message.reply_text(
            "📌 Good job, you've done 3 quizzes today! "
            "Nihongo AI will now take a break, and you should as well. "
            "See you tomorrow again! 📘✨"
        )
        return

    # ISSUE FIX #8: collect all topic labels used today (daily + any bonus already
    # generated) so the new bonus quiz picks a different topic each time.
    used_topics: list[str] = []
    daily_quiz = db.get_today_quiz(today)
    if daily_quiz and daily_quiz.topic_label_en:
        used_topics.append(daily_quiz.topic_label_en)
    for b in bonus_quizzes_today:
        if b.topic_label_en:
            used_topics.append(b.topic_label_en)

    await query.message.reply_text("⏳ Generating your bonus quiz... please wait!")
    bonus = await asyncio.to_thread(
        qg.generate_bonus_quiz,
        today,
        chat_id,
        quiz_type,
        quiz_sequence_for_day,
        used_topics,
    )

    # generate_bonus_quiz always returns a BonusQuiz (never None)
    db.save_bonus_quiz(bonus)

    await context.bot.send_message(chat_id=chat_id, text=bonus.full_message)
    await context.bot.send_message(
        chat_id=chat_id,
        text="👉 答えを選んでね：（1 / 2 / 3 / 4 ボタン）",
        reply_markup=_build_answer_keyboard(),
    )
    logger.info("bonus_quiz_generated and sent: bonus_id=%s chat_id=%s",
                bonus.bonus_id, chat_id)


async def _process_bonus_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                chat_id: int, chosen: int) -> None:
    today = _today_str()
    active_bonus = db.get_active_bonus_quiz(chat_id, today)

    if active_bonus is None:
        return

    is_correct = chosen == active_bonus.correct_option
    recorded = db.mark_bonus_answer(active_bonus.bonus_id, chat_id, chosen, is_correct)
    if not recorded:
        return

    explanation = qg.format_bonus_explanation(active_bonus, chosen)
    reply_func = _get_reply_func(update)
    await reply_func(explanation)

    logger.info("bonus_quiz_answered: bonus_id=%s chat_id=%s chosen=%s correct=%s",
                active_bonus.bonus_id, chat_id, chosen, is_correct)

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
        await _offer_bonus_quiz(update, context, chat_id, total_done)
