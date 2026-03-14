"""
Nihongo.AI — Main Bot Entry Point

Initialises the Telegram bot, registers handlers, starts the scheduler,
and begins polling.
"""

from __future__ import annotations

import asyncio
import sys

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from .config import TELEGRAM_BOT_TOKEN, logger
from . import database as db
from . import handlers
from .scheduler import setup_scheduler


async def main() -> None:
    """Build, configure, and run the bot."""

    # ------------------------------------------------------------------
    # Validate configuration
    # ------------------------------------------------------------------
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Initialise database
    # ------------------------------------------------------------------
    db.init_db()
    logger.info("Database ready.")

    # ------------------------------------------------------------------
    # Build Telegram application
    # ------------------------------------------------------------------
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # ------------------------------------------------------------------
    # Register command handlers
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Register callback query handler (inline button presses)
    # ------------------------------------------------------------------
    app.add_handler(CallbackQueryHandler(handlers.answer_callback, pattern=r"^answer_\d$"))

    # ------------------------------------------------------------------
    # Register text message handler (typed 1/2/3/4)
    # ------------------------------------------------------------------
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handlers.text_answer_handler,
        )
    )

    # ------------------------------------------------------------------
    # Setup and start scheduler
    # ------------------------------------------------------------------
    scheduler = setup_scheduler(app)
    scheduler.start()
    logger.info("Scheduler started.")

    # ------------------------------------------------------------------
    # Start polling
    # ------------------------------------------------------------------
    logger.info("Nihongo.AI bot is starting... 📘✨")

await app.initialize()
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

