"""
Nihongo.AI — Quiz Generator Module

Uses OpenAI API to generate daily Japanese reading comprehension passages
with questions, options, and explanations.
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime
from typing import Optional

from openai import OpenAI

from .config import (
    OPENAI_MODEL,
    GENERATION_TIMEOUT,
    PASSAGE_MIN_CHARS,
    PASSAGE_MAX_CHARS,
    FALLBACK_PASSAGE_MIN_CHARS,
    FALLBACK_PASSAGE_MAX_CHARS,
    BONUS_PASSAGE_MIN_CHARS,
    BONUS_PASSAGE_MAX_CHARS,
    TIMEZONE,
    MAX_TOPIC_REPEAT_IN_14_DAYS,
    logger,
)
from .models import Quiz, BonusQuiz
from . import database as db

# ---------------------------------------------------------------------------
# OpenAI client — lazily initialised
# ---------------------------------------------------------------------------
# B1 FIX: previously created at module load with client = OpenAI().
# If OPENAI_API_KEY is missing, some SDK versions raise at construction time
# before bot.py's startup validation can run and give a clear error.
# Lazy init ensures bot.py always gets to run its OPENAI_API_KEY check first.
_openai_client: Optional[OpenAI] = None

def _get_client() -> OpenAI:
    """Return the singleton OpenAI client, creating it on first use."""
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client

# ---------------------------------------------------------------------------
# Question type rotation
# ---------------------------------------------------------------------------
QUESTION_TYPES = [
    "main_idea",
    "detail_comprehension",
    "inference",
    "vocabulary_in_context",
    "pronoun_reference",
]


def _question_type_for_date(date_str: str) -> str:
    """
    S4 FIX: Previously used a process-global counter (_question_type_index)
    that reset to 0 on every Railway restart and was also consumed by retry
    attempts and bonus quiz generation, making the rotation completely uneven.

    Now derives question type deterministically from the date string so:
    - Same date always produces the same question type (idempotent across retries)
    - Restarts don't reset the rotation
    - Bonus quiz generation doesn't consume daily quiz rotation slots
    - The 5 types cycle evenly: one per day, repeating every 5 days
    """
    index = int(date_str.replace("-", "")) % len(QUESTION_TYPES)
    return QUESTION_TYPES[index]


def _random_question_type() -> str:
    """Pick a random question type for bonus quizzes (not date-tied)."""
    return random.choice(QUESTION_TYPES)


# ---------------------------------------------------------------------------
# Safe topics list
# ---------------------------------------------------------------------------
SAFE_TOPICS = [
    "daily life", "travel", "food", "weather", "hobbies",
    "parks", "school", "shopping", "cooking", "pets",
    "seasons", "festivals", "family", "friends", "sports",
    "music", "reading", "movies", "transportation", "neighborhood",
    "morning routine", "weekend plans", "café visit", "library",
    "birthday party", "new year", "cherry blossoms", "summer vacation",
    "autumn leaves", "winter clothes", "convenience store", "post office",
    "train station", "beach", "mountain hiking", "gardening",
    "studying Japanese", "part-time job", "moving to a new city",
    "visiting a shrine", "making friends", "phone call",
    "writing a letter", "rainy day", "picnic", "cycling",
]


def _pick_topic(recent_topics: list[str]) -> str:
    """Pick a topic that respects repetition rules."""
    topic_counts: dict[str, int] = {}
    for t in recent_topics:
        topic_counts[t] = topic_counts.get(t, 0) + 1

    last_topic = recent_topics[0] if recent_topics else ""

    candidates = []
    for topic in SAFE_TOPICS:
        if topic == last_topic:
            continue
        if topic_counts.get(topic, 0) >= MAX_TOPIC_REPEAT_IN_14_DAYS:
            continue
        candidates.append(topic)

    if not candidates:
        candidates = [t for t in SAFE_TOPICS if t != last_topic]
    if not candidates:
        candidates = SAFE_TOPICS

    return random.choice(candidates)


def _determine_jlpt_level(chat_id: Optional[int] = None) -> str:
    """Determine JLPT level based on user accuracy."""
    if chat_id is None:
        return "N5-N4 mixed"

    user = db.get_user(chat_id)
    if user and user.difficulty == "n4":
        return "primarily N4 with some N5"
    elif user and user.difficulty == "n5":
        return "primarily N5"
    return "N5-N4 mixed"


# ---------------------------------------------------------------------------
# Main generation prompt
# ---------------------------------------------------------------------------

def _build_generation_prompt(
    topic: str,
    question_type: str,
    jlpt_level: str,
    min_chars: int,
    max_chars: int,
) -> tuple[str, str]:
    """Build the system+user prompt for OpenAI. Returns (system_prompt, user_prompt)."""

    question_type_instruction = {
        "main_idea": "Ask about the main idea or theme of the passage.",
        "detail_comprehension": "Ask about a specific detail mentioned in the passage.",
        "inference": "Ask the reader to infer something not explicitly stated but implied.",
        "vocabulary_in_context": "Ask about the meaning of a word or phrase as used in context.",
        "pronoun_reference": "Ask what a pronoun (e.g., それ, この) refers to in the passage.",
    }.get(question_type, "Ask about the main idea of the passage.")

    system_prompt = """You are a Japanese language education expert creating JLPT reading comprehension materials.
You MUST respond with valid JSON only. No markdown, no code fences, no extra text."""

    # BUG FIX #4: topic_label prompt was ambiguous — the model was outputting
    # the English topic string instead of a Japanese translation.
    # Now explicitly instructs the model to translate the topic to Japanese.
    user_prompt = f"""Generate a Japanese reading comprehension quiz with these exact requirements:

PASSAGE REQUIREMENTS (CRITICAL — READ CAREFULLY):
- Topic: {topic}
- The passage MUST be {min_chars}–{max_chars} Japanese characters long.
- CHARACTER COUNT RULE: Count ONLY the base Japanese text characters (hiragana, katakana, kanji, punctuation). Do NOT count the furigana readings inside parentheses. For example, 学校(がっこう)に行(い)きました counts as 7 characters (学校に行きました), NOT 14.
- The passage MUST be long enough. Write at least 8-12 sentences to reach {min_chars} characters. A passage of only 3-4 sentences will be too short.
- Grammar level: {jlpt_level}
- Add furigana in parentheses immediately after EVERY kanji or kanji compound. Examples: 学校(がっこう), 今日(きょう), 行(い)きます, 食(た)べます, 天気(てんき)
- Use natural, engaging Japanese suitable for learners
- Do NOT include any content about: violence, sexual content, suicide, war, crime, medical emergencies
- The passage should read like a short story, diary entry, letter, or descriptive paragraph about everyday life
- Include specific details (names of places, times, descriptions) to make the passage rich and interesting

QUESTION REQUIREMENTS:
- Question type: {question_type_instruction}
- Write the question in Japanese (with furigana on kanji)
- Provide exactly 4 answer options in Japanese
- Exactly 1 option must be correct
- Other options must be plausible but clearly wrong
- No trick questions

EXPLANATION REQUIREMENTS:
- Japanese explanation: 2-4 sentences using simple N5 grammar, quoting a key phrase from the passage using 「」
- English explanation: 1 clear sentence

Respond with this exact JSON structure:
{{
  "passage": "the full passage with furigana — MUST be {min_chars}-{max_chars} base characters",
  "question": "the question in Japanese",
  "option1": "first option",
  "option2": "second option",
  "option3": "third option",
  "option4": "fourth option",
  "correct_option": 1,
  "explanation_ja": "Japanese explanation quoting from passage with 「」",
  "explanation_en": "One sentence English explanation",
  "topic_label": "Japanese translation of the topic (e.g. if topic is 'parks' write 公園, if 'daily life' write 日常生活, if 'cooking' write 料理)",
  "topic_label_en": "{topic}",
  "question_type": "{question_type}"
}}

IMPORTANT:
- correct_option must be an integer 1-4 indicating which option is correct.
- topic_label MUST be in Japanese kanji/hiragana, NOT English.
- The passage MUST be between {min_chars} and {max_chars} base characters (excluding furigana). This is the most critical requirement. Write a longer, more detailed passage to meet this count."""

    return system_prompt, user_prompt


def _clean_json_text(raw: str) -> str:
    """Make model output more tolerant for JSON parsing."""
    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()


def _parse_quiz_response(raw: str, date_str: str) -> Optional[Quiz]:
    """Parse the OpenAI JSON response into a Quiz object."""
    try:
        text = _clean_json_text(raw)
        data = json.loads(text)

        correct = int(data["correct_option"])
        if correct < 1 or correct > 4:
            correct = 1

        quiz = Quiz(
            quiz_id=date_str,
            date=date_str,
            passage=data["passage"],
            question=data["question"],
            option1=data["option1"],
            option2=data["option2"],
            option3=data["option3"],
            option4=data["option4"],
            correct_option=correct,
            explanation_ja=data.get("explanation_ja", ""),
            explanation_en=data.get("explanation_en", ""),
            topic_label=data.get("topic_label", ""),
            topic_label_en=data.get("topic_label_en", ""),
            question_type=data.get("question_type", ""),
            jlpt_level=data.get("jlpt_level", "N5-N4"),
            is_fallback=False,
            created_at=datetime.now(TIMEZONE).isoformat(),
        )
        return quiz
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Main quiz parsing failed: %s — raw: %s", e, raw[:300])
        return None


def _validate_quiz(quiz: Quiz, min_chars: int) -> bool:
    """
    Reject quizzes where the passage is too short, required fields are empty,
    or furigana is missing from kanji.

    The model sometimes ignores the character count or furigana instructions.
    This validation ensures both problems are caught and trigger a retry.
    """
    # Strip furigana readings e.g. 学校(がっこう) → 学校
    base_text = re.sub(r'\([^)]*\)', '', quiz.passage)
    base_text = re.sub(r'\s', '', base_text)
    base_len = len(base_text)

    # Allow 20% tolerance below the minimum to account for punctuation
    # differences in how the model counts vs how we count
    threshold = int(min_chars * 0.8)
    if base_len < threshold:
        logger.warning(
            "Passage too short: %d base chars (need %d, threshold %d) — rejecting",
            base_len, min_chars, threshold,
        )
        return False

    # Check all required fields are non-empty
    required = [quiz.question, quiz.option1, quiz.option2,
                quiz.option3, quiz.option4, quiz.explanation_ja]
    if not all(required):
        logger.warning("Quiz has one or more empty required fields — rejecting")
        return False

    # Q1 FIX: count kanji GROUPS not individual characters.
    # Old logic: counted each kanji char and checked if followed by '('.
    # Bug: 学校(がっこう) has 2 kanji chars but only 1 '(' → 50% coverage
    # → rejected every quiz with kanji compounds (i.e. virtually all of them).
    # Fix: treat each contiguous kanji sequence as one group.
    # 学校(がっこう) = 1 group WITH furigana ✓
    # 学校 (no reading) = 1 group WITHOUT furigana ✗
    kanji_group_pattern = re.compile(r'[\u4e00-\u9fff]+')
    kanji_group_with_furigana = re.compile(r'[\u4e00-\u9fff]+\(')
    kanji_total = len(kanji_group_pattern.findall(quiz.passage))
    kanji_covered = len(kanji_group_with_furigana.findall(quiz.passage))

    if kanji_total > 0:
        coverage = kanji_covered / kanji_total
        if coverage < 0.85:
            logger.warning(
                "Furigana coverage too low: %d/%d kanji groups have readings (%.0f%%) — rejecting",
                kanji_covered, kanji_total, coverage * 100,
            )
            return False

    return True


def _validate_bonus_quiz(bonus: BonusQuiz, min_chars: int) -> bool:
    """
    A6 FIX: Validate bonus quiz passages just like daily quizzes.
    Previously generate_bonus_quiz had no length or furigana check,
    meaning short or furigana-free passages were silently accepted.
    """
    # Strip furigana and count base characters
    base_text = re.sub(r'\([^)]*\)', '', bonus.passage)
    base_text = re.sub(r'\s', '', base_text)
    base_len = len(base_text)

    threshold = int(min_chars * 0.8)
    if base_len < threshold:
        logger.warning(
            "Bonus passage too short: %d base chars (need %d, threshold %d) — rejecting",
            base_len, min_chars, threshold,
        )
        return False

    required = [bonus.question, bonus.option1, bonus.option2,
                bonus.option3, bonus.option4, bonus.explanation_ja]
    if not all(required):
        logger.warning("Bonus quiz has one or more empty required fields — rejecting")
        return False

    # Q2 FIX: same group-based furigana check as daily quiz (see Q1 comment above)
    kanji_group_pattern = re.compile(r'[\u4e00-\u9fff]+')
    kanji_group_with_furigana = re.compile(r'[\u4e00-\u9fff]+\(')
    kanji_total = len(kanji_group_pattern.findall(bonus.passage))
    kanji_covered = len(kanji_group_with_furigana.findall(bonus.passage))

    if kanji_total > 0:
        coverage = kanji_covered / kanji_total
        if coverage < 0.85:
            logger.warning(
                "Bonus furigana coverage too low: %d/%d kanji groups (%.0f%%) — rejecting",
                kanji_covered, kanji_total, coverage * 100,
            )
            return False

    return True


# ---------------------------------------------------------------------------
# Format quiz message for Telegram
# ---------------------------------------------------------------------------

def format_quiz_message(quiz: Quiz, date_str: str) -> str:
    """Format the quiz into the exact Telegram message structure."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    day_names = ["月", "火", "水", "木", "金", "土", "日"]
    day_name = day_names[dt.weekday()]

    header = f"📅 {dt.year}年{dt.month}月{dt.day}日（{day_name}曜日）"
    topic_line = f"🏷️ テーマ：{quiz.topic_label}"
    level_line = f"📖 読解（N5〜N4）"

    msg = (
        f"{header}\n"
        f"{topic_line}\n"
        f"{level_line}\n\n"
        f"{quiz.passage}\n\n"
        f"❓ 質問\n"
        f"{quiz.question}\n\n"
        f"🅰️ 1. {quiz.option1}\n"
        f"🅱️ 2. {quiz.option2}\n"
        f"🅲️ 3. {quiz.option3}\n"
        f"🅳️ 4. {quiz.option4}\n\n"
        f"👉 答えを選んでね：（1 / 2 / 3 / 4 ボタン）"
    )

    if quiz.is_fallback:
        msg = (
            "🙇 Sorry — I couldn't generate today's full passage just now.\n"
            "Here's a quick mini one to keep your streak going:\n\n"
            + msg
            + "\n\nI'll try again in 10 minutes and send the full version."
        )

    return msg


def format_explanation(quiz: Quiz, chosen: int) -> str:
    """Format the explanation message after answering."""
    is_correct = chosen == quiz.correct_option

    options = [quiz.option1, quiz.option2, quiz.option3, quiz.option4]
    correct_text = options[quiz.correct_option - 1]

    header = "✅ 正解！" if is_correct else "❌ 残念！"

    msg = (
        f"{header}\n\n"
        f"正しい答え：{quiz.correct_option}. {correct_text}\n\n"
        f"理由（日本語）：{quiz.explanation_ja}\n\n"
        f"English Explanation: {quiz.explanation_en}"
    )
    return msg


# ---------------------------------------------------------------------------
# Public generation functions
# ---------------------------------------------------------------------------

def generate_quiz(date_str: Optional[str] = None,
                  chat_id: Optional[int] = None,
                  is_fallback: bool = False) -> Optional[Quiz]:
    """
    Generate a new quiz for the given date.
    Returns Quiz on success, None on failure.
    """
    if date_str is None:
        date_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    recent_topics = db.get_recent_topics(days=14)
    topic = _pick_topic(recent_topics)
    # S4 FIX: use date-based deterministic selection, not a mutable global counter
    question_type = _question_type_for_date(date_str)
    jlpt_level = _determine_jlpt_level(chat_id)

    if is_fallback:
        min_chars = FALLBACK_PASSAGE_MIN_CHARS
        max_chars = FALLBACK_PASSAGE_MAX_CHARS
    else:
        min_chars = PASSAGE_MIN_CHARS
        max_chars = PASSAGE_MAX_CHARS

    system_prompt, user_prompt = _build_generation_prompt(
        topic, question_type, jlpt_level, min_chars, max_chars
    )

    try:
        logger.info("Generating quiz: topic=%s type=%s level=%s fallback=%s",
                     topic, question_type, jlpt_level, is_fallback)

        response = _get_client().chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=2000,
            timeout=GENERATION_TIMEOUT,
        )

        raw = response.choices[0].message.content or ""

        quiz = _parse_quiz_response(raw, date_str)
        if quiz is None or not _validate_quiz(quiz, min_chars):
            logger.warning("Quiz failed validation, returning None")
            return None

        quiz.question_type = question_type
        quiz.is_fallback = is_fallback
        quiz.full_message = format_quiz_message(quiz, date_str)
        return quiz

    except Exception as e:
        logger.warning("OpenAI generation failed: %s", e)
        return None


def generate_quiz_with_fallback(date_str: Optional[str] = None) -> Quiz:
    """
    Try to generate a full quiz with retries. If all attempts fail,
    generate a fallback mini quiz, then fall back to a hardcoded pool.

    BUG FIX #2 (part B): Added retry loop (3 attempts) before giving up on
    full quiz generation, and 2 attempts on fallback size. This prevents a
    single transient OpenAI timeout from immediately writing the hardcoded
    fallback to the DB.
    """
    if date_str is None:
        date_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    # Try full quiz up to 3 times
    for attempt in range(1, 4):
        quiz = generate_quiz(date_str=date_str, is_fallback=False)
        if quiz is not None:
            logger.info("Main quiz generation succeeded on attempt %d for %s",
                        attempt, date_str)
            return quiz
        logger.warning("Full quiz attempt %d/3 failed for %s", attempt, date_str)

    # Try fallback (shorter) quiz up to 2 times
    logger.warning("All full quiz attempts failed for %s, trying fallback size...", date_str)
    for attempt in range(1, 3):
        quiz = generate_quiz(date_str=date_str, is_fallback=True)
        if quiz is not None:
            logger.warning("Using fallback-size quiz for %s (attempt %d)", date_str, attempt)
            try:
                from . import scheduler as sched
                sched._schedule_fallback_retry(date_str)
            except Exception as e:
                logger.warning("Could not schedule fallback retry for %s: %s", date_str, e)
            return quiz

    # Last resort: hardcoded fallback pool (varies by date)
    logger.error("All generation attempts failed for %s, using hardcoded fallback pool", date_str)
    return _hardcoded_fallback(date_str)


# ---------------------------------------------------------------------------
# Bonus Quiz generation
# ---------------------------------------------------------------------------

def _parse_bonus_response(raw: str, bonus_id: str, date_str: str,
                          chat_id: int, quiz_type: str,
                          quiz_sequence_for_day: int) -> Optional[BonusQuiz]:
    """Parse the OpenAI JSON response into a BonusQuiz object."""
    try:
        text = _clean_json_text(raw)
        data = json.loads(text)

        correct = int(data["correct_option"])
        if correct < 1 or correct > 4:
            correct = 1

        bonus = BonusQuiz(
            bonus_id=bonus_id,
            date=date_str,
            quiz_type=quiz_type,
            quiz_sequence_for_day=quiz_sequence_for_day,
            chat_id=chat_id,
            passage=data["passage"],
            question=data["question"],
            option1=data["option1"],
            option2=data["option2"],
            option3=data["option3"],
            option4=data["option4"],
            correct_option=correct,
            explanation_ja=data.get("explanation_ja", ""),
            explanation_en=data.get("explanation_en", ""),
            topic_label=data.get("topic_label", ""),
            topic_label_en=data.get("topic_label_en", ""),
            is_answered=False,
            answered_at="",
            chosen_option=0,
            created_at=datetime.now(TIMEZONE).isoformat(),
        )
        return bonus
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Failed to parse bonus quiz response: %s — raw: %s", e, raw[:300])
        return None


def format_bonus_quiz_message(bonus: BonusQuiz) -> str:
    """Format a bonus quiz into the shorter Telegram message structure."""
    msg = (
        f"✨ Bonus Quiz\n"
        f"📖 読解（Practice）\n\n"
        f"{bonus.passage}\n\n"
        f"❓質問\n"
        f"{bonus.question}\n\n"
        f"1. {bonus.option1}\n"
        f"2. {bonus.option2}\n"
        f"3. {bonus.option3}\n"
        f"4. {bonus.option4}\n\n"
        f"👉 答えを選んでね（1 / 2 / 3 / 4）"
    )
    return msg


def format_bonus_explanation(bonus: BonusQuiz, chosen: int) -> str:
    """Format the explanation message after answering a bonus quiz."""
    is_correct = chosen == bonus.correct_option
    options = [bonus.option1, bonus.option2, bonus.option3, bonus.option4]
    correct_text = options[bonus.correct_option - 1]

    header = "✅ 正解！" if is_correct else "❌ 残念！"
    msg = (
        f"{header}\n\n"
        f"正しい答え：{bonus.correct_option}. {correct_text}\n\n"
        f"理由（日本語）：{bonus.explanation_ja}\n\n"
        f"English Explanation: {bonus.explanation_en}"
    )
    return msg


def _hardcoded_bonus_fallback(date_str: str, chat_id: int,
                              quiz_type: str,
                              quiz_sequence_for_day: int) -> BonusQuiz:
    """Absolute last-resort fallback bonus quiz."""
    bonus_id = f"{date_str}_{quiz_type}"

    bonus = BonusQuiz(
        bonus_id=bonus_id,
        date=date_str,
        quiz_type=quiz_type,
        quiz_sequence_for_day=quiz_sequence_for_day,
        chat_id=chat_id,
        passage="今日(きょう)、図書館(としょかん)へ行(い)きました。静(しず)かな部屋(へや)で本(ほん)を読(よ)みました。そのあと、近(ちか)くの店(みせ)でジュースを買(か)いました。",
        question="どこで本(ほん)を読(よ)みましたか？",
        option1="学校(がっこう)",
        option2="公園(こうえん)",
        option3="図書館(としょかん)",
        option4="駅(えき)",
        correct_option=3,
        explanation_ja="本文(ほんぶん)に「図書館(としょかん)へ行(い)きました。静(しず)かな部屋(へや)で本(ほん)を読(よ)みました」とあります。だから、答(こた)えは図書館(としょかん)です。",
        explanation_en="The passage says the person went to the library and read there, so the answer is the library.",
        topic_label="図書館",
        topic_label_en="library",
        is_answered=False,
        answered_at="",
        chosen_option=0,
        created_at=datetime.now(TIMEZONE).isoformat(),
    )
    bonus.full_message = format_bonus_quiz_message(bonus)
    return bonus


def generate_bonus_quiz(date_str: str, chat_id: int,
                        quiz_type: str, quiz_sequence_for_day: int,
                        used_topics: Optional[list[str]] = None) -> BonusQuiz:
    """
    Generate a bonus quiz for a user.
    Bonus quizzes are shorter (150-200 chars) by design — intentionally less
    taxing than the main daily quiz (250-300 chars).

    ISSUE FIX #8: accepts used_topics so the bonus quiz topic is never the
    same as the daily quiz topic or any earlier bonus quiz that day.
    Falls back to a hardcoded bonus quiz if generation fails.
    Always returns a BonusQuiz (never None).
    """
    # Pick a topic that hasn't been used yet today
    excluded = set(used_topics) if used_topics else set()
    candidates = [t for t in SAFE_TOPICS if t not in excluded]
    if not candidates:
        candidates = SAFE_TOPICS  # safety fallback if all topics somehow exhausted
    topic = random.choice(candidates)
    # Bonus quizzes use random question type — they're not part of the daily rotation
    question_type = _random_question_type()
    jlpt_level = _determine_jlpt_level(chat_id)

    min_chars = BONUS_PASSAGE_MIN_CHARS
    max_chars = BONUS_PASSAGE_MAX_CHARS

    system_prompt, user_prompt = _build_generation_prompt(
        topic, question_type, jlpt_level, min_chars, max_chars
    )

    bonus_id = f"{date_str}_{quiz_type}"

    # Q3 FIX: retry up to 2 times before falling back to hardcoded content.
    # Previously a single validation failure immediately served the hardcoded
    # fallback with no retry. Since validation failures are usually transient
    # (model produced a short passage this once), one retry recovers most cases.
    for attempt in range(1, 3):
        try:
            logger.info("Generating bonus quiz: bonus_id=%s topic=%s type=%s attempt=%d",
                         bonus_id, topic, question_type, attempt)

            response = _get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.8,
                max_tokens=1500,
                timeout=GENERATION_TIMEOUT,
            )

            raw = response.choices[0].message.content or ""
            bonus = _parse_bonus_response(
                raw, bonus_id, date_str, chat_id, quiz_type, quiz_sequence_for_day
            )

            if bonus is not None and _validate_bonus_quiz(bonus, min_chars):
                bonus.full_message = format_bonus_quiz_message(bonus)
                logger.info("bonus_quiz_generated: bonus_id=%s chat_id=%s attempt=%d",
                            bonus_id, chat_id, attempt)
                return bonus

            logger.warning("Bonus quiz attempt %d/2 failed validation — %s",
                           attempt, "parse error" if bonus is None else "validation failed")

        except Exception as e:
            logger.warning("Bonus quiz attempt %d/2 raised exception: %s", attempt, e)

    logger.warning("All bonus quiz attempts failed — using hardcoded fallback")
    return _hardcoded_bonus_fallback(
        date_str, chat_id, quiz_type, quiz_sequence_for_day
    )


# ---------------------------------------------------------------------------
# BUG FIX #2 (part A): Hardcoded fallback pool
# ---------------------------------------------------------------------------
# Previously a SINGLE hardcoded passage was always used, so every day the
# bot couldn't reach OpenAI, users got the exact same 公園 passage.
# Now we have a pool of varied passages. The pool is indexed by date so the
# same date always returns the same quiz (idempotent), but different days
# get different passages.

_HARDCODED_FALLBACKS = [
    {
        "passage": (
            "今日(きょう)は天気(てんき)がいいです。公園(こうえん)に行(い)きました。"
            "花(はな)がきれいでした。友達(ともだち)と一緒(いっしょ)に"
            "お弁当(べんとう)を食(た)べました。とても楽(たの)しかったです。"
        ),
        "question": "今日(きょう)、どこに行(い)きましたか？",
        "options": ["学校(がっこう)", "公園(こうえん)", "お店(みせ)", "家(いえ)"],
        "correct": 2,
        "explain_ja": "本文(ほんぶん)に「公園(こうえん)に行(い)きました」とあります。だから、答(こた)えは公園(こうえん)です。",
        "explain_en": "The passage says 'I went to the park', so the answer is the park.",
        "topic": "公園", "topic_en": "parks",
    },
    {
        "passage": (
            "わたしは毎朝(まいあさ)六時(ろくじ)に起(お)きます。"
            "シャワーを浴(あ)びて、朝(あさ)ごはんを食(た)べます。"
            "それから、電車(でんしゃ)で学校(がっこう)に行(い)きます。"
            "電車(でんしゃ)の中(なか)で本(ほん)を読(よ)みます。"
            "学校(がっこう)は九時(くじ)に始(はじ)まります。"
        ),
        "question": "この人(ひと)は学校(がっこう)へどうやって行(い)きますか？",
        "options": ["バスで", "電車(でんしゃ)で", "自転車(じてんしゃ)で", "歩(ある)いて"],
        "correct": 2,
        "explain_ja": "本文(ほんぶん)に「電車(でんしゃ)で学校(がっこう)に行(い)きます」とあります。だから、答(こた)えは電車(でんしゃ)です。",
        "explain_en": "The passage says the person goes to school by train.",
        "topic": "朝(あさ)のルーティン", "topic_en": "morning routine",
    },
    {
        "passage": (
            "きのう、スーパーへ買(か)い物(もの)に行(い)きました。"
            "野菜(やさい)と果物(くだもの)を買(か)いました。"
            "りんごがとても安(やす)かったです。"
            "家(いえ)に帰(かえ)って、カレーを作(つく)りました。"
            "家族(かぞく)みんなで食(た)べました。とてもおいしかったです。"
        ),
        "question": "この人(ひと)はきのう何(なに)を作(つく)りましたか？",
        "options": ["スープ", "サラダ", "カレー", "すし"],
        "correct": 3,
        "explain_ja": "本文(ほんぶん)に「カレーを作(つく)りました」とあります。だから、答(こた)えはカレーです。",
        "explain_en": "The passage says the person made curry after returning home.",
        "topic": "料理(りょうり)", "topic_en": "cooking",
    },
    {
        "passage": (
            "今日(きょう)は日曜日(にちようび)です。"
            "図書館(としょかん)へ行(い)きました。"
            "静(しず)かな部屋(へや)で日本語(にほんご)の本(ほん)を読(よ)みました。"
            "三時間(さんじかん)ぐらい勉強(べんきょう)しました。"
            "帰(かえ)りに友達(ともだち)とコーヒーを飲(の)みました。"
        ),
        "question": "この人(ひと)は図書館(としょかん)で何(なに)をしましたか？",
        "options": ["映画(えいが)を見(み)ました", "音楽(おんがく)を聞(き)きました", "日本語(にほんご)を勉強(べんきょう)しました", "友達(ともだち)と話(はな)しました"],
        "correct": 3,
        "explain_ja": "本文(ほんぶん)に「日本語(にほんご)の本(ほん)を読(よ)みました。三時間(さんじかん)ぐらい勉強(べんきょう)しました」とあります。だから、答(こた)えは日本語(にほんご)を勉強(べんきょう)することです。",
        "explain_en": "The passage says the person read a Japanese book and studied for about three hours.",
        "topic": "図書館(としょかん)", "topic_en": "library",
    },
    {
        "passage": (
            "先週(せんしゅう)の土曜日(どようび)、家族(かぞく)と海(うみ)に行(い)きました。"
            "砂浜(すなはま)を歩(ある)いて、貝(かい)を拾(ひろ)いました。"
            "お昼(ひる)ごはんはビーチで食(た)べました。"
            "子供(こども)たちは水(みず)の中(なか)で遊(あそ)びました。"
            "夕方(ゆうがた)に家(いえ)に帰(かえ)りました。"
        ),
        "question": "この人(ひと)たちはいつ海(うみ)に行(い)きましたか？",
        "options": ["先週(せんしゅう)の月曜日(げつようび)", "先週(せんしゅう)の土曜日(どようび)", "今週(こんしゅう)の日曜日(にちようび)", "先月(せんげつ)"],
        "correct": 2,
        "explain_ja": "本文(ほんぶん)に「先週(せんしゅう)の土曜日(どようび)、家族(かぞく)と海(うみ)に行(い)きました」とあります。だから、答(こた)えは先週(せんしゅう)の土曜日(どようび)です。",
        "explain_en": "The passage states they went to the sea last Saturday.",
        "topic": "海(うみ)", "topic_en": "beach",
    },
    {
        "passage": (
            "わたしの趣味(しゅみ)は料理(りょうり)です。"
            "毎週末(まいしゅうまつ)、新(あたら)しい料理(りょうり)を作(つく)ります。"
            "今週(こんしゅう)はイタリアのパスタを作(つく)りました。"
            "友達(ともだち)が家(いえ)に来(き)て、一緒(いっしょ)に食(た)べました。"
            "みんな「おいしい！」と言(い)いました。"
        ),
        "question": "今週(こんしゅう)、この人(ひと)は何(なに)を作(つく)りましたか？",
        "options": ["すし", "カレー", "パスタ", "ラーメン"],
        "correct": 3,
        "explain_ja": "本文(ほんぶん)に「今週(こんしゅう)はイタリアのパスタを作(つく)りました」とあります。だから、答(こた)えはパスタです。",
        "explain_en": "The passage says the person made Italian pasta this week.",
        "topic": "料理(りょうり)", "topic_en": "cooking",
    },
    {
        "passage": (
            "春(はる)になりました。桜(さくら)の花(はな)がきれいに咲(さ)いています。"
            "週末(しゅうまつ)に友達(ともだち)と花見(はなみ)をしました。"
            "公園(こうえん)でお弁当(べんとう)を食(た)べながら、桜(さくら)を見(み)ました。"
            "風(かぜ)が吹(ふ)いて、花びらが空(そら)に舞(ま)いました。"
            "とても美(うつく)しかったです。"
        ),
        "question": "この人(ひと)たちは週末(しゅうまつ)に何(なに)をしましたか？",
        "options": ["買(か)い物(もの)をしました", "映画(えいが)を見(み)ました", "花見(はなみ)をしました", "山(やま)に登(のぼ)りました"],
        "correct": 3,
        "explain_ja": "本文(ほんぶん)に「週末(しゅうまつ)に友達(ともだち)と花見(はなみ)をしました」とあります。だから、答(こた)えは花見(はなみ)です。",
        "explain_en": "The passage says they did hanami (flower viewing) with friends on the weekend.",
        "topic": "桜(さくら)", "topic_en": "cherry blossoms",
    },
]


def _hardcoded_fallback(date_str: str) -> Quiz:
    """
    BUG FIX #2 (part A): Use a pool of varied fallback quizzes instead of
    always returning the same 公園 passage.

    The date string is used as a deterministic index so:
    - The same date always gets the same quiz (idempotent / consistent).
    - Different days get different passages from the pool.
    """
    day_index = int(date_str.replace("-", "")) % len(_HARDCODED_FALLBACKS)
    fb = _HARDCODED_FALLBACKS[day_index]

    quiz = Quiz(
        quiz_id=date_str,
        date=date_str,
        passage=fb["passage"],
        question=fb["question"],
        option1=fb["options"][0],
        option2=fb["options"][1],
        option3=fb["options"][2],
        option4=fb["options"][3],
        correct_option=fb["correct"],
        explanation_ja=fb["explain_ja"],
        explanation_en=fb["explain_en"],
        topic_label=fb["topic"],
        topic_label_en=fb["topic_en"],
        question_type="detail_comprehension",
        jlpt_level="N5",
        is_fallback=True,
        created_at=datetime.now(TIMEZONE).isoformat(),
    )
    quiz.full_message = format_quiz_message(quiz, date_str)
    return quiz
