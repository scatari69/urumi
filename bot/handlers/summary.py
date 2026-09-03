import html
import logging

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from core.config import settings
from core.llm import LLMQuotaError, LLMUnavailableError
from core.summaries import NotEnoughMessages, generate_summary

logger = logging.getLogger(__name__)

router = Router(name="summary")

DEFAULT_SUMMARY_HOURS = 24
SPLIT_CHAR_LIMIT = 3500

USAGE_MESSAGE = "Использование: /summary [часов], например /summary 6"
QUOTA_MESSAGE = "Лимит запросов к модели исчерпан, попробую позже."
OVERLOADED_MESSAGE = "Модель сейчас перегружена, попробуй чуть позже."
TIMEOUT_MESSAGE = "Модель не ответила вовремя, попробуй ещё раз."
EMPTY_ANSWER_MESSAGE = "Модель вернула пустой пересказ."


def _parse_hours(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return min(DEFAULT_SUMMARY_HOURS, settings.HISTORY_TTL_HOURS)

    try:
        hours = int(raw.split()[0])
    except ValueError:
        return None

    if hours <= 0:
        return None
    return min(hours, settings.HISTORY_TTL_HOURS)


def _split_for_telegram(text: str, limit: int = SPLIT_CHAR_LIMIT) -> list[str]:
    parts: list[str] = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    if remaining:
        parts.append(remaining)
    return parts


@router.message(F.chat.id == settings.GROUP_CHAT_ID, Command("summary"))
async def summarize_chat(message: Message, command: CommandObject) -> None:
    hours = _parse_hours(command.args)
    if hours is None:
        await message.reply(USAGE_MESSAGE)
        return

    try:
        summary_text = await generate_summary(message.chat.id, hours)
    except NotEnoughMessages as exc:
        await message.reply(
            f"За последние {hours} ч. набралось всего {exc.count} сообщений — пересказывать нечего."
        )
        return
    except LLMQuotaError:
        logger.exception("LLM quota exhausted while summarizing chat %s", message.chat.id)
        await message.reply(QUOTA_MESSAGE)
        return
    except LLMUnavailableError:
        logger.exception("LLM still failing after retries while summarizing chat %s", message.chat.id)
        await message.reply(OVERLOADED_MESSAGE)
        return
    except httpx.TimeoutException:
        logger.exception("LLM request timed out while summarizing chat %s", message.chat.id)
        await message.reply(TIMEOUT_MESSAGE)
        return

    if not summary_text:
        await message.reply(EMPTY_ANSWER_MESSAGE)
        return

    parts = _split_for_telegram(summary_text)
    await message.reply(html.escape(parts[0], quote=False), parse_mode="HTML")
    for part in parts[1:]:
        await message.answer(html.escape(part, quote=False), parse_mode="HTML")
