"""
Nihongo.AI — Quiz Generator Module

Uses OpenAI API to generate daily Japanese reading comprehension passages
with questions, options, and explanations.
"""

from __future__ import annotations

import json
import random
import signal
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
# OpenAI client (pre-configured via environment)
# ---------------------------------------------------------------------------
client = OpenAI()

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

_question_type_index = 0


def _next_question_type() -> str:
    global _question_type_index
    qt = QUESTION_TYPES[_question_type_index % len(QUESTION_TYPES)]
    _question_type_index += 1
    return qt


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
) -> str:
    """Build the system+user prompt for OpenAI."""

    question_type_instruction = {
        "main_idea": "Ask about the main idea or theme of the passage.",
        "detail_comprehension": "Ask about a specific detail mentioned in the passage.",
        "inference": "Ask the reader to infer something not explicitly stated but implied.",
        "vocabulary_in_context": "Ask about the meaning of a word or phrase as used in context.",
        "pronoun_reference": "Ask what a pronoun (e.g., それ, この) refers to in the passage.",
    }.get(question_type, "Ask about the main idea of the passage.")

    system_prompt = """You are a Japanese language education expert creating JLPT reading comprehension materials.
You MUST respond with valid JSON only. No markdown, no code fences, no extra text."""

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
  "topic_label": "{topic} in Japanese (e.g., 日常生活)",
  "topic_label_en": "{topic}",
  "question_type": "{question_type}"
}}

IMPORTANT:
- correct_option must be an integer 1-4 indicating which option is correct.
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
            jlpt_level=data.get("jlpt_level", "N5-N4"),
            is_fallback=False,
            created_at=datetime.now(TIMEZONE).isoformat(),
        )
        return quiz
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Main quiz parsing failed: %s — raw: %s", e, raw[:300])
        return None


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

    if is_correct:
        header = "✅ 正解！"
    else:
        header = "❌ 残念！"

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
    question_type = _next_question_type()
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

        response = client.chat.completions.create(
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

        if quiz is None:
            logger.warning("Quiz parsing failed, returning None")
            return None

        quiz.is_fallback = is_fallback
        quiz.full_message = format_quiz_message(quiz, date_str)
        return quiz

    except Exception as e:
        logger.warning("OpenAI generation failed: %s", e)
        return None


def generate_quiz_with_fallback(date_str: Optional[str] = None) -> Quiz:
    """
    Try to generate a full quiz. If it fails or times out,
    generate a fallback mini quiz and schedule a retry.
    """
    if date_str is None:
        date_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    quiz = generate_quiz(date_str=date_str, is_fallback=False)
    if quiz is not None:
        logger.info("Main quiz generation succeeded for %s", date_str)
        return quiz

    logger.warning("Full quiz generation failed for %s, trying fallback...", date_str)

    quiz = generate_quiz(date_str=date_str, is_fallback=True)
    if quiz is not None:
        try:
            from . import scheduler as sched
            sched._schedule_fallback_retry(date_str)
        except Exception as e:
            logger.warning("Could not schedule fallback retry for %s: %s", date_str, e)

        return quiz

    logger.error("All generation attempts failed, using hardcoded fallback")
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
                        quiz_type: str, quiz_sequence_for_day: int) -> Optional[BonusQuiz]:
    """
    Generate a bonus quiz for a user.
    Bonus quizzes ignore topic rotation rules.
    Falls back to a hardcoded bonus quiz if generation fails.
    """
    topic = random.choice(SAFE_TOPICS)
    question_type = _next_question_type()
    jlpt_level = _determine_jlpt_level(chat_id)

    min_chars = BONUS_PASSAGE_MIN_CHARS
    max_chars = BONUS_PASSAGE_MAX_CHARS

    system_prompt, user_prompt = _build_generation_prompt(
        topic, question_type, jlpt_level, min_chars, max_chars
    )

    bonus_id = f"{date_str}_{quiz_type}"

    try:
        logger.info("Generating bonus quiz: bonus_id=%s topic=%s type=%s",
                     bonus_id, topic, question_type)

        response = client.chat.completions.create(
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

        if bonus is None:
            logger.warning("Bonus quiz parsing failed, using hardcoded fallback")
            return _hardcoded_bonus_fallback(
                date_str, chat_id, quiz_type, quiz_sequence_for_day
            )

        bonus.full_message = format_bonus_quiz_message(bonus)
        logger.info("bonus_quiz_generated: bonus_id=%s chat_id=%s", bonus_id, chat_id)
        return bonus

    except Exception as e:
        logger.warning("Bonus quiz generation failed: %s — using hardcoded fallback", e)
        return _hardcoded_bonus_fallback(
            date_str, chat_id, quiz_type, quiz_sequence_for_day
        )


def _hardcoded_fallback(date_str: str) -> Quiz:
    """Absolute last-resort fallback quiz."""
    quiz = Quiz(
        quiz_id=date_str,
        date=date_str,
        passage="今日(きょう)は天気(てんき)がいいです。公園(こうえん)に行(い)きました。花(はな)がきれいでした。友達(ともだち)と一緒(いっしょ)にお弁当(べんとう)を食(た)べました。とても楽(たの)しかったです。",
        question="今日(きょう)、どこに行(い)きましたか？",
        option1="学校(がっこう)",
        option2="公園(こうえん)",
        option3="お店(みせ)",
        option4="家(いえ)",
        correct_option=2,
        explanation_ja="本文(ほんぶん)に「公園(こうえん)に行(い)きました」とあります。だから、答(こた)えは公園(こうえん)です。",
        explanation_en="The passage says 'I went to the park', so the answer is the park.",
        topic_label="公園",
        topic_label_en="parks",
        jlpt_level="N5",
        is_fallback=True,
        created_at=datetime.now(TIMEZONE).isoformat(),
    )
    quiz.full_message = format_quiz_message(quiz, date_str)
    return quiz
