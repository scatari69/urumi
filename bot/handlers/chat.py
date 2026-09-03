import logging
import random
import time
from datetime import timezone

import httpx
from aiogram import Bot, F, Router
from aiogram.types import Message, User

from core.config import settings
from core.db import get_db, get_settings, insert_message, setting_bool, setting_value
from core.llm import LLMQuotaError, LLMUnavailableError, llm_client, parse_fallbacks, resolve_model
from core.moods import compose_system_prompt, mood_temperature, resolve_current
from core.prompts import base_system_prompt

logger = logging.getLogger(__name__)

router = Router(name="chat")

DEFAULT_CONTEXT_MESSAGES = 30
DEFAULT_RANDOM_REPLY_CHANCE = 0.0
DEFAULT_TEMPERATURE = 0.7

PROFILE_LOOKBACK_MESSAGES = 30
NOTES_HEADER = "Notes about participants:"
NOTES_CHAR_LIMIT = 1500
REPLY_COOLDOWN_SECONDS = 10

# Structural, not persona — kept separate from the editable system_prompt/mood text
# so it always reaches the model, telling it the transcript below is not one voice.
CHAT_FORMAT_NOTE = (
    "Ты общаешься в групповом чате с несколькими разными людьми, не с одним "
    "собеседником. История ниже — строки вида 'Имя: текст', и каждая строка написана "
    "своим отдельным человеком: не путай их между собой и не приписывай слова одного "
    "участника другому. В конце отдельно указано, кто написал сообщение, на которое "
    "нужно ответить именно сейчас, — отвечай ему, а не чату в целом."
)
HISTORY_LABEL = "История чата (для контекста):"
CURRENT_LABEL = "Ответь на это сообщение от {name}:"

QUOTA_MESSAGE = "Лимит запросов к модели исчерпан, попробую позже."
OVERLOADED_MESSAGE = "Модель сейчас перегружена, попробуй чуть позже."
TIMEOUT_MESSAGE = "Модель не ответила вовремя, попробуй ещё раз."

_last_reply_at: dict[int, float] = {}


def _should_reply(message: Message, me: User, random_reply_chance: float) -> bool:
    text = message.text or ""
    if me.username and f"@{me.username}".lower() in text.lower():
        return True

    replied_to = message.reply_to_message
    if replied_to is not None and replied_to.from_user is not None:
        if replied_to.from_user.id == me.id:
            return True

    return random.random() < random_reply_chance


def _acquire_reply_slot(user_id: int) -> bool:
    now = time.monotonic()
    last = _last_reply_at.get(user_id)
    if last is not None and now - last < REPLY_COOLDOWN_SECONDS:
        return False
    _last_reply_at[user_id] = now
    return True


async def _fetch_history(chat_id: int, limit: int) -> list[tuple[int, str | None, str | None]]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT user_id, display_name, text
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
    return list(reversed(rows))


async def _fetch_notes(chat_id: int) -> list[tuple[str | None, str]]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT display_name, notes
            FROM user_profiles
            WHERE chat_id = ?
              AND opted_out = 0
              AND notes IS NOT NULL
              AND notes != ''
              AND user_id IN (
                  SELECT user_id FROM (
                      SELECT user_id
                      FROM messages
                      WHERE chat_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
              )
            """,
            (chat_id, chat_id, PROFILE_LOOKBACK_MESSAGES),
        )
        return await cursor.fetchall()


def _build_notes_block(rows: list[tuple[str | None, str]]) -> str:
    lines: list[str] = []
    total = len(NOTES_HEADER)

    for display_name, notes in rows:
        line = f"{display_name or 'unknown'}: {notes}"
        if total + len(line) + 1 > NOTES_CHAR_LIMIT:
            break
        lines.append(line)
        total += len(line) + 1

    if not lines:
        return ""
    return "\n".join([NOTES_HEADER, *lines])


@router.message(F.chat.id == settings.GROUP_CHAT_ID, F.text)
async def reply_in_chat(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return

    values = await get_settings()
    if not setting_bool(values, "enabled", True):
        return

    random_reply_chance = setting_value(
        values, "random_reply_chance", DEFAULT_RANDOM_REPLY_CHANCE, float
    )

    me = await bot.me()
    if not _should_reply(message, me, random_reply_chance):
        return

    if not _acquire_reply_slot(message.from_user.id):
        logger.info("Skipping reply to user %s: cooldown active", message.from_user.id)
        return

    mood = await resolve_current(values)
    system_prompt = compose_system_prompt(base_system_prompt(values), mood)
    context_messages = setting_value(values, "context_messages", DEFAULT_CONTEXT_MESSAGES, int)
    temperature = mood_temperature(
        mood, setting_value(values, "temperature", DEFAULT_TEMPERATURE, float)
    )

    history = await _fetch_history(message.chat.id, context_messages + 1)
    if history and history[-1][0] == message.from_user.id and history[-1][2] == message.text:
        history = history[:-1]
    history = history[-context_messages:] if context_messages > 0 else []

    notes_block = _build_notes_block(await _fetch_notes(message.chat.id))
    system_content = f"{system_prompt}\n\n{CHAT_FORMAT_NOTE}"
    if notes_block:
        system_content = f"{system_content}\n\n{notes_block}"

    history_lines = [f"{display_name or 'unknown'}: {text}" for _, display_name, text in history]
    current_line = f"{message.from_user.full_name}: {message.text}"

    user_content_parts = []
    if history_lines:
        user_content_parts.append(f"{HISTORY_LABEL}\n" + "\n".join(history_lines))
    user_content_parts.append(
        f"{CURRENT_LABEL.format(name=message.from_user.full_name)}\n{current_line}"
    )
    user_content = "\n\n".join(user_content_parts)

    llm_messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    try:
        answer = await llm_client.chat(
            llm_messages,
            model=resolve_model(values, "chat_model"),
            temperature=temperature,
            fallbacks=parse_fallbacks(values),
        )
    except LLMQuotaError:
        logger.exception("LLM quota exhausted while answering in chat %s", message.chat.id)
        await message.reply(QUOTA_MESSAGE)
        return
    except LLMUnavailableError:
        logger.exception("LLM still failing after retries while answering in chat %s", message.chat.id)
        await message.reply(OVERLOADED_MESSAGE)
        return
    except httpx.TimeoutException:
        logger.exception("LLM request timed out while answering in chat %s", message.chat.id)
        await message.reply(TIMEOUT_MESSAGE)
        return

    answer = answer.strip()
    if not answer:
        logger.warning("LLM returned an empty answer for chat %s", message.chat.id)
        return

    sent = await message.reply(answer)

    await insert_message(
        chat_id=sent.chat.id,
        user_id=me.id,
        username=me.username,
        display_name=me.full_name,
        text=answer,
        reply_to_message_id=message.message_id,
        ts=int(sent.date.astimezone(timezone.utc).timestamp()),
    )
