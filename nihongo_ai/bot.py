"""
Nihongo.AI — Main Bot Entry Point

Initialises the Telegram bot, registers handlers, starts the scheduler,
and begins polling.
"""

from __future__ import annotations

import asyncio
import sys

from telegram.error import TimedOut, NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from .config import TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, logger
from . import database as db
from . import handlers
from .scheduler import setup_scheduler


# C1 FIX: Railway frequently has a transient network blip on first boot.
# The previous nihongo_ai/bot.py called app.initialize() once with no retry,
# meaning a single TimedOut on startup crashed the entire process permanently.
# These constants mirror the root-level bot.py which already had retry logic.
STARTUP_RETRIES = 5
STARTUP_RETRY_DELAY_SECONDS = 5
TELEGRAM_CONNECT_TIMEOUT = 30.0
TELEGRAM_READ_TIMEOUT = 30.0
TELEGRAM_WRITE_TIMEOUT = 30.0
TELEGRAM_POOL_TIMEOUT = 30.0


async def _initialize_with_retry(app) -> None:
    """Initialize Telegram app with retries to survive transient network timeouts."""
    last_error: Exception | None = None

    for attempt in range(1, STARTUP_RETRIES + 1):
        try:
            logger.info(
                "Initializing Telegram app (attempt %s/%s)...",
                attempt,
                STARTUP_RETRIES,
            )
            await app.initialize()
            logger.info("Telegram app initialized successfully.")
            return
        except (TimedOut, NetworkError, OSError) as e:
            last_error = e
            logger.warning(
                "Telegram initialization failed on attempt %s/%s: %s",
                attempt,
                STARTUP_RETRIES,
                e,
            )
            if attempt < STARTUP_RETRIES:
                logger.info(
                    "Retrying Telegram initialization in %s seconds...",
                    STARTUP_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(STARTUP_RETRY_DELAY_SECONDS)

    assert last_error is not None
    raise last_error


async def main() -> None:
    """Build, configure, and run the bot."""

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    # D2 FIX: validate ANTHROPIC_API_KEY at startup. Previously the bot started
    # successfully with a missing or expired key, then silently sent hardcoded
    # fallback quizzes every day with no error message — exactly the failure
    # mode experienced in production. Failing fast here makes the problem
    # immediately visible in Railway logs.
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY is not set. Exiting.")
        sys.exit(1)

    db.init_db()
    logger.info("Database ready.")

    # Use explicit timeouts on the HTTP client so slow Railway networks
    # don't cause indefinite hangs during polling.
    request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.start_command))
    app.add_handler(CommandHandler("today", handlers.today_command))
    app.add_handler(CommandHandler("stats", handlers.stats_command))
    app.add_handler(CommandHandler("level", handlers.level_command))
    app.add_handler(CommandHandler("pause", handlers.pause_command))
    app.add_handler(CommandHandler("resume", handlers.resume_command))
    app.add_handler(CommandHandler("reminders", handlers.reminders_command))
    app.add_handler(CommandHandler("help", handlers.help_command))
    app.add_handler(CommandHandler("delete_my_data", handlers.delete_my_data_command))
    app.add_handler(CommandHandler("reset_today", handlers.reset_today_command))

    app.add_handler(CallbackQueryHandler(handlers.answer_callback, pattern=r"^answer_\d$"))
    app.add_handler(CallbackQueryHandler(handlers.bonus_callback, pattern=r"^bonus_(yes|no)$"))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handlers.text_answer_handler,
        )
    )

    scheduler = setup_scheduler(app)
    scheduler.start()
    logger.info("Scheduler started.")

    logger.info("Nihongo.AI bot is starting... 📘✨")

    await _initialize_with_retry(app)
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
