#!/usr/bin/env python3
"""
Nihongo.AI — Top-level runner script.
Usage:  python run.py
"""
import asyncio
from nihongo_ai.bot import main

if __name__ == "__main__":
    asyncio.run(main())
