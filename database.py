"""
Nihongo.AI — Database Module (SQLite)

Provides the data-layer abstraction so bot logic never touches SQL directly.
All functions use a single SQLite database file for persistence.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

from .config import DB_PATH, TIMEZONE, ACTIVE_DAYS_THRESHOLD, logger
from .models import User, Quiz, BonusQuiz, Answer, WeeklyStats


# ---------------------------------------------------------------------------
# Thread-local connections (SQLite is not thread-safe by default)
# ---------------------------------------------------------------------------
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id         INTEGER PRIMARY KEY,
    joined_at       TEXT NOT NULL,
    last_interaction TEXT NOT NULL,
    streak          INTEGER NOT NULL DEFAULT 0,
    paused          INTEGER NOT NULL DEFAULT 0,
    reminders_enabled INTEGER NOT NULL DEFAULT 1,
    difficulty      TEXT NOT NULL DEFAULT 'mixed',
    total_correct   INTEGER NOT NULL DEFAULT 0,
    total_answered  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_quizzes (
    quiz_id         TEXT PRIMARY KEY,
    date            TEXT NOT NULL,
    passage         TEXT NOT NULL,
    question        TEXT NOT NULL,
    option1         TEXT NOT NULL,
    option2         TEXT NOT NULL,
    option3         TEXT NOT NULL,
    option4         TEXT NOT NULL,
    correct_option  INTEGER NOT NULL,
    explanation_ja  TEXT NOT NULL DEFAULT '',
    explanation_en  TEXT NOT NULL DEFAULT '',
    topic_label     TEXT NOT NULL DEFAULT '',
    topic_label_en  TEXT NOT NULL DEFAULT '',
    jlpt_level      TEXT NOT NULL DEFAULT 'N5-N4',
    is_fallback     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    full_message    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS answers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    quiz_date       TEXT NOT NULL,
    chosen_option   INTEGER NOT NULL,
    is_correct      INTEGER NOT NULL DEFAULT 0,
    answered_at     TEXT NOT NULL,
    question_type   TEXT NOT NULL DEFAULT '',
    UNIQUE(chat_id, quiz_date)
);

CREATE INDEX IF NOT EXISTS idx_answers_chat ON answers(chat_id);
CREATE INDEX IF NOT EXISTS idx_answers_date ON answers(quiz_date);
CREATE INDEX IF NOT EXISTS idx_quizzes_date ON daily_quizzes(date);

CREATE TABLE IF NOT EXISTS bonus_quizzes (
    bonus_id        TEXT NOT NULL,
    date            TEXT NOT NULL,
    quiz_type       TEXT NOT NULL DEFAULT '',
    quiz_sequence_for_day INTEGER NOT NULL DEFAULT 2,
    chat_id         INTEGER NOT NULL,
    passage         TEXT NOT NULL,
    question        TEXT NOT NULL,
    option1         TEXT NOT NULL,
    option2         TEXT NOT NULL,
    option3         TEXT NOT NULL,
    option4         TEXT NOT NULL,
    correct_option  INTEGER NOT NULL,
    explanation_ja  TEXT NOT NULL DEFAULT '',
    explanation_en  TEXT NOT NULL DEFAULT '',
    topic_label     TEXT NOT NULL DEFAULT '',
    topic_label_en  TEXT NOT NULL DEFAULT '',
    is_answered     INTEGER NOT NULL DEFAULT 0,
    answered_at     TEXT NOT NULL DEFAULT '',
    chosen_option   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    full_message    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (bonus_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_bonus_chat_date ON bonus_quizzes(chat_id, date);
"""


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat()


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        chat_id=row["chat_id"],
        joined_at=row["joined_at"],
        last_interaction=row["last_interaction"],
        streak=row["streak"],
        paused=bool(row["paused"]),
        reminders_enabled=bool(row["reminders_enabled"]),
        difficulty=row["difficulty"],
        total_correct=row["total_correct"],
        total_answered=row["total_answered"],
    )


def get_or_create_user(chat_id: int) -> User:
    """Return existing user or create a new one."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        user = _row_to_user(row)
        return user
    now = _now_iso()
    conn.execute(
        "INSERT INTO users (chat_id, joined_at, last_interaction) VALUES (?,?,?)",
        (chat_id, now, now),
    )
    conn.commit()
    return User(chat_id=chat_id, joined_at=now, last_interaction=now)


def get_user(chat_id: int) -> Optional[User]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return _row_to_user(row) if row else None


def update_last_interaction(chat_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET last_interaction=? WHERE chat_id=?",
        (_now_iso(), chat_id),
    )
    conn.commit()


def set_user_paused(chat_id: int, paused: bool) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET paused=? WHERE chat_id=?",
        (int(paused), chat_id),
    )
    conn.commit()


def set_reminders_enabled(chat_id: int, enabled: bool) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET reminders_enabled=? WHERE chat_id=?",
        (int(enabled), chat_id),
    )
    conn.commit()


def update_user_difficulty(chat_id: int, difficulty: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET difficulty=? WHERE chat_id=?",
        (difficulty, chat_id),
    )
    conn.commit()


def update_streak(chat_id: int, streak: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET streak=? WHERE chat_id=?",
        (streak, chat_id),
    )
    conn.commit()


def increment_user_stats(chat_id: int, correct: bool) -> None:
    """Increment total_answered (and total_correct if correct)."""
    conn = _get_conn()
    if correct:
        conn.execute(
            "UPDATE users SET total_answered=total_answered+1, total_correct=total_correct+1 WHERE chat_id=?",
            (chat_id,),
        )
    else:
        conn.execute(
            "UPDATE users SET total_answered=total_answered+1 WHERE chat_id=?",
            (chat_id,),
        )
    conn.commit()


def get_active_users() -> list[User]:
    """Return users who interacted within ACTIVE_DAYS_THRESHOLD and are not paused."""
    conn = _get_conn()
    cutoff = (datetime.now(TIMEZONE) - timedelta(days=ACTIVE_DAYS_THRESHOLD)).isoformat()
    rows = conn.execute(
        "SELECT * FROM users WHERE last_interaction >= ? AND paused = 0",
        (cutoff,),
    ).fetchall()
    return [_row_to_user(r) for r in rows]


def get_all_users() -> list[User]:
    """Return all users."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users").fetchall()
    return [_row_to_user(r) for r in rows]


def delete_user(chat_id: int) -> None:
    """Delete all data for a user (GDPR-style)."""
    conn = _get_conn()
    conn.execute("DELETE FROM answers WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM bonus_quizzes WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    conn.commit()
    logger.info("Deleted all data for chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# Quiz helpers
# ---------------------------------------------------------------------------

def _row_to_quiz(row: sqlite3.Row) -> Quiz:
    return Quiz(
        quiz_id=row["quiz_id"],
        date=row["date"],
        passage=row["passage"],
        question=row["question"],
        option1=row["option1"],
        option2=row["option2"],
        option3=row["option3"],
        option4=row["option4"],
        correct_option=row["correct_option"],
        explanation_ja=row["explanation_ja"],
        explanation_en=row["explanation_en"],
        topic_label=row["topic_label"],
        topic_label_en=row["topic_label_en"],
        jlpt_level=row["jlpt_level"],
        is_fallback=bool(row["is_fallback"]),
        created_at=row["created_at"],
        full_message=row["full_message"],
    )


def save_today_quiz(quiz: Quiz) -> None:
    """Insert or replace today's quiz."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO daily_quizzes
           (quiz_id, date, passage, question, option1, option2, option3, option4,
            correct_option, explanation_ja, explanation_en, topic_label, topic_label_en,
            jlpt_level, is_fallback, created_at, full_message)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            quiz.quiz_id, quiz.date, quiz.passage, quiz.question,
            quiz.option1, quiz.option2, quiz.option3, quiz.option4,
            quiz.correct_option, quiz.explanation_ja, quiz.explanation_en,
            quiz.topic_label, quiz.topic_label_en, quiz.jlpt_level,
            int(quiz.is_fallback), quiz.created_at, quiz.full_message,
        ),
    )
    conn.commit()
    logger.info("Saved quiz for date=%s topic=%s", quiz.date, quiz.topic_label)


def get_today_quiz(date_str: str) -> Optional[Quiz]:
    """Get quiz for a specific date string (YYYY-MM-DD)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM daily_quizzes WHERE date=?", (date_str,)
    ).fetchone()
    return _row_to_quiz(row) if row else None


def get_latest_quiz() -> Optional[Quiz]:
    """Get the most recent quiz."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM daily_quizzes ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return _row_to_quiz(row) if row else None


def delete_quiz_for_date(date_str: str) -> None:
    """Delete quiz and associated answers for a date (used by /reset_today)."""
    conn = _get_conn()
    conn.execute("DELETE FROM daily_quizzes WHERE date=?", (date_str,))
    conn.execute("DELETE FROM answers WHERE quiz_date=?", (date_str,))
    conn.commit()
    logger.info("Deleted quiz and answers for date=%s", date_str)


def get_recent_topics(days: int = 14) -> list[str]:
    """Return topic labels from the last N days."""
    conn = _get_conn()
    cutoff = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT topic_label FROM daily_quizzes WHERE date >= ? ORDER BY date DESC",
        (cutoff,),
    ).fetchall()
    return [r["topic_label"] for r in rows]


# ---------------------------------------------------------------------------
# Answer helpers
# ---------------------------------------------------------------------------

def get_answer(chat_id: int, quiz_date: str) -> Optional[Answer]:
    """Check if user already answered a quiz."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM answers WHERE chat_id=? AND quiz_date=?",
        (chat_id, quiz_date),
    ).fetchone()
    if not row:
        return None
    return Answer(
        chat_id=row["chat_id"],
        quiz_date=row["quiz_date"],
        chosen_option=row["chosen_option"],
        is_correct=bool(row["is_correct"]),
        answered_at=row["answered_at"],
        question_type=row["question_type"],
    )


def mark_answer(chat_id: int, quiz_date: str, chosen: int,
                is_correct: bool, question_type: str = "") -> bool:
    """Record an answer. Returns True if newly inserted, False if duplicate."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO answers (chat_id, quiz_date, chosen_option, is_correct, answered_at, question_type)
               VALUES (?,?,?,?,?,?)""",
            (chat_id, quiz_date, chosen, int(is_correct), _now_iso(), question_type),
        )
        conn.commit()
        logger.info("Answer recorded: chat_id=%s date=%s chosen=%s correct=%s",
                     chat_id, quiz_date, chosen, is_correct)
        return True
    except sqlite3.IntegrityError:
        # Duplicate — user already answered
        return False


def get_user_answers_recent(chat_id: int, limit: int = 10) -> list[Answer]:
    """Get the most recent N answers for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM answers WHERE chat_id=? ORDER BY answered_at DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return [
        Answer(
            chat_id=r["chat_id"],
            quiz_date=r["quiz_date"],
            chosen_option=r["chosen_option"],
            is_correct=bool(r["is_correct"]),
            answered_at=r["answered_at"],
            question_type=r["question_type"],
        )
        for r in rows
    ]


def get_weekly_answers(chat_id: int, start_date: str, end_date: str) -> list[Answer]:
    """Get answers between two dates (inclusive)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM answers WHERE chat_id=? AND quiz_date >= ? AND quiz_date <= ?",
        (chat_id, start_date, end_date),
    ).fetchall()
    return [
        Answer(
            chat_id=r["chat_id"],
            quiz_date=r["quiz_date"],
            chosen_option=r["chosen_option"],
            is_correct=bool(r["is_correct"]),
            answered_at=r["answered_at"],
            question_type=r["question_type"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Bonus Quiz helpers
# ---------------------------------------------------------------------------

def _row_to_bonus_quiz(row: sqlite3.Row) -> BonusQuiz:
    return BonusQuiz(
        bonus_id=row["bonus_id"],
        date=row["date"],
        quiz_type=row["quiz_type"],
        quiz_sequence_for_day=row["quiz_sequence_for_day"],
        chat_id=row["chat_id"],
        passage=row["passage"],
        question=row["question"],
        option1=row["option1"],
        option2=row["option2"],
        option3=row["option3"],
        option4=row["option4"],
        correct_option=row["correct_option"],
        explanation_ja=row["explanation_ja"],
        explanation_en=row["explanation_en"],
        topic_label=row["topic_label"],
        topic_label_en=row["topic_label_en"],
        is_answered=bool(row["is_answered"]),
        answered_at=row["answered_at"],
        chosen_option=row["chosen_option"],
        created_at=row["created_at"],
        full_message=row["full_message"],
    )


def save_bonus_quiz(bonus_quiz: BonusQuiz) -> None:
    """Insert or replace a bonus quiz record."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO bonus_quizzes
           (bonus_id, date, quiz_type, quiz_sequence_for_day, chat_id,
            passage, question, option1, option2, option3, option4,
            correct_option, explanation_ja, explanation_en,
            topic_label, topic_label_en, is_answered, answered_at,
            chosen_option, created_at, full_message)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bonus_quiz.bonus_id, bonus_quiz.date, bonus_quiz.quiz_type,
            bonus_quiz.quiz_sequence_for_day, bonus_quiz.chat_id,
            bonus_quiz.passage, bonus_quiz.question,
            bonus_quiz.option1, bonus_quiz.option2,
            bonus_quiz.option3, bonus_quiz.option4,
            bonus_quiz.correct_option, bonus_quiz.explanation_ja,
            bonus_quiz.explanation_en, bonus_quiz.topic_label,
            bonus_quiz.topic_label_en, int(bonus_quiz.is_answered),
            bonus_quiz.answered_at, bonus_quiz.chosen_option,
            bonus_quiz.created_at, bonus_quiz.full_message,
        ),
    )
    conn.commit()
    logger.info("Saved bonus quiz bonus_id=%s chat_id=%s",
                bonus_quiz.bonus_id, bonus_quiz.chat_id)


def get_bonus_quizzes_for_day(chat_id: int, date_str: str) -> list[BonusQuiz]:
    """Return all bonus quizzes for a user on a given date, ordered by sequence."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM bonus_quizzes
           WHERE chat_id=? AND date=?
           ORDER BY quiz_sequence_for_day ASC""",
        (chat_id, date_str),
    ).fetchall()
    return [_row_to_bonus_quiz(r) for r in rows]


def get_active_bonus_quiz(chat_id: int, date_str: str) -> Optional[BonusQuiz]:
    """Return the most recent unanswered bonus quiz for a user today, or None."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT * FROM bonus_quizzes
           WHERE chat_id=? AND date=? AND is_answered=0
           ORDER BY quiz_sequence_for_day ASC
           LIMIT 1""",
        (chat_id, date_str),
    ).fetchone()
    return _row_to_bonus_quiz(row) if row else None


def mark_bonus_answer(bonus_id: str, chat_id: int,
                      chosen: int, is_correct: bool) -> bool:
    """Record an answer for a bonus quiz. Returns True if updated, False if already answered."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT is_answered FROM bonus_quizzes WHERE bonus_id=? AND chat_id=?",
        (bonus_id, chat_id),
    ).fetchone()
    if not row or row["is_answered"]:
        return False
    conn.execute(
        """UPDATE bonus_quizzes
           SET is_answered=1, answered_at=?, chosen_option=?
           WHERE bonus_id=? AND chat_id=?""",
        (_now_iso(), chosen, bonus_id, chat_id),
    )
    conn.commit()
    logger.info("Bonus answer recorded: bonus_id=%s chat_id=%s chosen=%s correct=%s",
                bonus_id, chat_id, chosen, is_correct)
    return True


def count_quizzes_today(chat_id: int, date_str: str) -> int:
    """Return total number of quizzes answered today (main + bonus)."""
    conn = _get_conn()
    # Count main quiz answers
    main_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM answers WHERE chat_id=? AND quiz_date=?",
        (chat_id, date_str),
    ).fetchone()
    main_count = main_row["cnt"] if main_row else 0

    # Count answered bonus quizzes
    bonus_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM bonus_quizzes WHERE chat_id=? AND date=? AND is_answered=1",
        (chat_id, date_str),
    ).fetchone()
    bonus_count = bonus_row["cnt"] if bonus_row else 0

    return main_count + bonus_count


def delete_bonus_quizzes_for_date(date_str: str) -> None:
    """Delete all bonus quizzes for a given date (used by /reset_today for cleanup)."""
    conn = _get_conn()
    conn.execute("DELETE FROM bonus_quizzes WHERE date=?", (date_str,))
    conn.commit()
    logger.info("Deleted bonus quizzes for date=%s", date_str)


def get_unanswered_users(quiz_date: str) -> list[int]:
    """Return chat_ids of active, non-paused users who haven't answered today's quiz."""
    conn = _get_conn()
    cutoff = (datetime.now(TIMEZONE) - timedelta(days=ACTIVE_DAYS_THRESHOLD)).isoformat()
    rows = conn.execute(
        """SELECT u.chat_id FROM users u
           WHERE u.last_interaction >= ?
             AND u.paused = 0
             AND u.reminders_enabled = 1
             AND u.chat_id NOT IN (
                 SELECT a.chat_id FROM answers a WHERE a.quiz_date = ?
             )""",
        (cutoff, quiz_date),
    ).fetchall()
    return [r["chat_id"] for r in rows]
