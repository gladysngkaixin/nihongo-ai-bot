"""
Allow running with: python -m nihongo_ai

X1 FIX: Previously called main() directly without asyncio.run().
Since main() is a coroutine, calling it bare just creates a coroutine
object and exits — the bot never actually started when invoked this way.
Wrapped in asyncio.run() to match how run.py correctly starts the bot.
"""
import asyncio
from .bot import main

asyncio.run(main())
