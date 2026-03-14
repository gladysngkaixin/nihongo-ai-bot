"""
Nihongo.AI — Data Models

Plain dataclasses used throughout the application.
These are transport objects — the database module handles persistence.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class User:
    """Represents a bot user."""
    chat_id: int
    joined_at: str = ""                 # ISO datetime
    last_interaction: str = ""          # ISO datetime
    streak: int = 0
    paused: bool = False
    reminders_enabled: bool = True
    difficulty: str = "mixed"           # "n5", "n4", "mixed"
    total_correct: int = 0
    total_answered: int = 0


@dataclass
class Quiz:
    """A daily quiz sent to users."""
    quiz_id: str = ""                   # YYYY-MM-DD (date string as ID)
    date: str = ""                      # YYYY-MM-DD
    passage: str = ""
    question: str = ""
    option1: str = ""
    option2: str = ""
    option3: str = ""
    option4: str = ""
    correct_option: int = 1             # 1-4
    explanation_ja: str = ""
    explanation_en: str = ""
    topic_label: str = ""
    topic_label_en: str = ""
    jlpt_level: str = "N5-N4"
    is_fallback: bool = False
    created_at: str = ""                # ISO datetime
    full_message: str = ""              # Pre-formatted Telegram message


@dataclass
class Answer:
    """A user's answer to a quiz."""
    chat_id: int = 0
    quiz_date: str = ""                 # YYYY-MM-DD
    chosen_option: int = 0              # 1-4
    is_correct: bool = False
    answered_at: str = ""               # ISO datetime
    question_type: str = ""             # main_idea, detail, inference, vocab, pronoun


@dataclass
class WeeklyStats:
    """Aggregated weekly statistics for a user."""
    total_quizzes: int = 0
    correct_answers: int = 0
    accuracy: float = 0.0
    current_streak: int = 0
    common_mistakes: list[str] = field(default_factory=list)
    focus_points: list[str] = field(default_factory=list)
