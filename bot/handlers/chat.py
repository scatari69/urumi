import base64
import logging
import random
import time
from datetime import timezone

import httpx
from aiogram import Bot, F, Router
from aiogram.types import Message, User

from bot.filters import ActiveChat
from bot.handlers.logger import extract_text
from core.db import get_chat_settings, get_db, insert_message, setting_bool, setting_value
from core.llm import (
    LLMQuotaError,
    LLMUnavailableError,
    llm_client,
    model_supports_vision,
    parse_fallbacks,
    resolve_model,
)
from core.moods import compose_system_prompt, mood_temperature, resolve_current
from core.prompts import apply_chat_style, base_system_prompt

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
    "нужно ответить именно сейчас, — отвечай ему, а не чату в целом. Формат "
    "'Имя: текст' — это только разметка истории для тебя, а не образец для твоего "
    "ответа: не начинай свою реплику с имени, обращения по имени или '@Имя' — Telegram "
    "и так покажет, что это ответ именно этому человеку, лишний тег будет выдуманным "
    "и нерабочим. Просто пиши сам текст ответа."
)
HISTORY_LABEL = "История чата (для контекста):"
CURRENT_LABEL = "Ответь на это сообщение от {name}:"

# Telegram photos are always JPEG. Kept comfortably under most vision APIs' request
# body limits even after base64's ~33% inflation.
IMAGE_MIME_TYPE = "image/jpeg"
MAX_IMAGE_BYTES = 5 * 1024 * 1024

QUOTA_MESSAGE = "Лимит запросов к модели исчерпан, попробую позже."
OVERLOADED_MESSAGE = "Модель сейчас перегружена, попробуй чуть позже."
TIMEOUT_MESSAGE = "Модель не ответила вовремя, попробуй ещё раз."

_last_reply_at: dict[int, float] = {}


def _should_reply(message: Message, me: User, random_reply_chance: float) -> bool:
    text = message.text or message.caption or ""
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


async def _download_photo(bot: Bot, message: Message) -> bytes | None:
    """Largest photo size that fits MAX_IMAGE_BYTES, or None if none does / it fails."""
    for size in reversed(message.photo):
        if size.file_size and size.file_size > MAX_IMAGE_BYTES:
            continue
        try:
            buf = await bot.download(size.file_id)
        except Exception:
            logger.exception("Failed to download photo %s", size.file_id)
            return None
        data = buf.read()
        if len(data) <= MAX_IMAGE_BYTES:
            return data
    return None


@router.message(ActiveChat(), F.text | F.photo)
async def reply_in_chat(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return

    values = await get_chat_settings(message.chat.id)
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
    system_prompt = apply_chat_style(
        compose_system_prompt(base_system_prompt(values), mood), values
    )
    context_messages = setting_value(values, "context_messages", DEFAULT_CONTEXT_MESSAGES, int)
    temperature = mood_temperature(
        mood, setting_value(values, "temperature", DEFAULT_TEMPERATURE, float)
    )

    text_or_caption = extract_text(message)

    history = await _fetch_history(message.chat.id, context_messages + 1)
    if history and history[-1][0] == message.from_user.id and history[-1][2] == text_or_caption:
        history = history[:-1]
    history = history[-context_messages:] if context_messages > 0 else []

    notes_block = _build_notes_block(await _fetch_notes(message.chat.id))
    system_content = f"{system_prompt}\n\n{CHAT_FORMAT_NOTE}"
    if notes_block:
        system_content = f"{system_content}\n\n{notes_block}"

    history_lines = [f"{display_name or 'unknown'}: {text}" for _, display_name, text in history]
    current_line = f"{message.from_user.full_name}: {text_or_caption}"

    user_content_parts = []
    if history_lines:
        user_content_parts.append(f"{HISTORY_LABEL}\n" + "\n".join(history_lines))
    if message.reply_to_message is not None:
        replied_to = message.reply_to_message
        sender = replied_to.sender_chat or replied_to.from_user
        name = (getattr(sender, "title", None) or getattr(sender, "full_name", None)
                or "неизвестный отправитель")
        user_content_parts.append(
            f"Сообщение, на которое отвечает участник:\n{name}: {extract_text(replied_to)}"
        )
    user_content_parts.append(
        f"{CURRENT_LABEL.format(name=message.from_user.full_name)}\n{current_line}"
    )
    user_content = "\n\n".join(user_content_parts)

    model = resolve_model(values, "chat_model")
    content: str | list[dict] = user_content

    if message.photo:
        if await model_supports_vision(model):
            image_bytes = await _download_photo(bot, message)
            if image_bytes:
                b64 = base64.b64encode(image_bytes).decode("ascii")
                content = [
                    {"type": "text", "text": user_content},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{IMAGE_MIME_TYPE};base64,{b64}"},
                    },
                ]
            else:
                logger.warning(
                    "Could not download photo in chat %s, replying on caption only", message.chat.id
                )
        else:
            logger.info(
                "Model %s has no vision support, replying to photo on caption only", model
            )

    llm_messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": content},
    ]

    try:
        answer = await llm_client.chat(
            llm_messages,
            model=model,
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
