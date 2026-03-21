"""
Nihongo.AI — Data Models

Plain dataclasses used throughout the application.
These are transport objects — the database module handles persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # ISSUE FIX #3/#4/#5: question_type was missing from this dataclass.
    # database.py, quiz_generator.py and handlers.py all reference quiz.question_type
    # or construct Quiz(..., question_type=...). Without this field every quiz load,
    # quiz generation, and answer submission crashed with TypeError/AttributeError.
    question_type: str = ""             # main_idea, detail_comprehension, inference,
                                        # vocabulary_in_context, pronoun_reference
    jlpt_level: str = "N5-N4"
    is_fallback: bool = False
    created_at: str = ""                # ISO datetime
    full_message: str = ""              # Pre-formatted Telegram message


@dataclass
class BonusQuiz:
    """A bonus quiz offered after the user completes the main daily quiz.
    Bonus quizzes are shorter (150-200 chars) by design — intentionally less
    taxing than the main daily quiz (250-300 chars).
    Bonus quizzes do not affect streak, weekly summary, or difficulty adaptation.
    """
    bonus_id: str = ""                  # e.g. "2024-01-15_bonus_1"
    date: str = ""                      # YYYY-MM-DD
    quiz_type: str = ""                 # "bonus_1" or "bonus_2"
    quiz_sequence_for_day: int = 0      # 2 for first bonus, 3 for second bonus
    chat_id: int = 0                    # owner user
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
    is_answered: bool = False
    answered_at: str = ""               # ISO datetime
    chosen_option: int = 0
    created_at: str = ""                # ISO datetime
    full_message: str = ""              # Pre-formatted Telegram message


@dataclass
class Answer:
    """A user's answer to a quiz."""
    chat_id: int = 0
    quiz_date: str = ""                 # YYYY-MM-DD
    chosen_option: int = 0             # 1-4
    is_correct: bool = False
    answered_at: str = ""               # ISO datetime
    question_type: str = ""             # main_idea, detail_comprehension, inference,
                                        # vocabulary_in_context, pronoun_reference


@dataclass
class WeeklyStats:
    """Aggregated weekly statistics for a user."""
    total_quizzes: int = 0
    correct_answers: int = 0
    accuracy: float = 0.0
    current_streak: int = 0
    common_mistakes: list[str] = field(default_factory=list)
    focus_points: list[str] = field(default_factory=list)
