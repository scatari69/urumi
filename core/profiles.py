import asyncio
import logging
import time

import httpx

from core.config import settings
from core.db import get_db, get_settings, setting_value
from core.llm import LLMQuotaError, LLMUnavailableError, llm_client, parse_fallbacks, resolve_model
from core.prompts import base_system_prompt, profile_prompt

logger = logging.getLogger(__name__)

PROFILE_INTERVAL_SECONDS = 6 * 3600
MIN_NEW_MESSAGES = 20
NOTE_CHAR_LIMIT = 600
MESSAGES_CHAR_LIMIT = 8000
DEFAULT_TEMPERATURE = 0.7

EMPTY_NOTES_PLACEHOLDER = "(пусто)"


async def fetch_active_users(
    chat_id: int, since_ts: int, min_messages: int, exclude_user_id: int
) -> list[tuple[int, int]]:
    """Users with enough new messages in the window, skipping anyone opted out."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT m.user_id, COUNT(*) AS n
            FROM messages m
            LEFT JOIN user_profiles p
                ON p.user_id = m.user_id AND p.chat_id = m.chat_id
            WHERE m.chat_id = ?
              AND m.ts >= ?
              AND m.user_id != ?
              AND COALESCE(p.opted_out, 0) = 0
            GROUP BY m.user_id
            HAVING n >= ?
            """,
            (chat_id, since_ts, exclude_user_id, min_messages),
        )
        return await cursor.fetchall()


async def fetch_user_messages(chat_id: int, user_id: int, since_ts: int) -> list[tuple[str | None, str | None]]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT display_name, text
            FROM messages
            WHERE chat_id = ? AND user_id = ? AND ts >= ?
            ORDER BY id
            """,
            (chat_id, user_id, since_ts),
        )
        return await cursor.fetchall()


async def get_profile(chat_id: int, user_id: int) -> tuple[str | None, str | None, int, int] | None:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT display_name, notes, updated_at, opted_out
            FROM user_profiles
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        return await cursor.fetchone()


async def save_note(chat_id: int, user_id: int, display_name: str | None, notes: str) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO user_profiles (user_id, chat_id, display_name, notes, updated_at, opted_out)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                display_name = excluded.display_name,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (user_id, chat_id, display_name, notes, int(time.time())),
        )
        await db.commit()


async def opt_out(chat_id: int, user_id: int, display_name: str | None) -> None:
    """Wipe the note and stop profiling this user."""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO user_profiles (user_id, chat_id, display_name, notes, updated_at, opted_out)
            VALUES (?, ?, ?, NULL, ?, 1)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                notes = NULL,
                opted_out = 1,
                updated_at = excluded.updated_at
            """,
            (user_id, chat_id, display_name, int(time.time())),
        )
        await db.commit()


def _truncate_note(note: str, limit: int = NOTE_CHAR_LIMIT) -> str:
    note = note.strip()
    if len(note) <= limit:
        return note
    cut = note.rfind("\n", 0, limit)
    return (note[:cut] if cut > 0 else note[:limit]).rstrip()


def _build_messages_block(rows: list[tuple[str | None, str | None]], limit: int = MESSAGES_CHAR_LIMIT) -> str:
    """Most recent messages that fit the budget, kept in chronological order."""
    kept: list[str] = []
    total = 0

    for _, text in reversed(rows):
        if not text:
            continue
        if total + len(text) + 1 > limit:
            break
        kept.append(text)
        total += len(text) + 1

    return "\n".join(reversed(kept))


async def update_profile(
    chat_id: int,
    user_id: int,
    since_ts: int,
    system_content: str,
    prompt: str,
    temperature: float,
    model: str,
    fallbacks: list[str] | None = None,
) -> None:
    rows = await fetch_user_messages(chat_id, user_id, since_ts)
    messages_block = _build_messages_block(rows)
    if not messages_block:
        return

    display_name = rows[-1][0]
    profile = await get_profile(chat_id, user_id)

    if profile is not None and profile[3]:
        logger.info("Profile update: user %s opted out, skipping", user_id)
        return

    current_notes = (profile[1] if profile else None) or EMPTY_NOTES_PLACEHOLDER

    user_content = (
        f"{prompt}\n\n"
        f"Текущая заметка:\n{current_notes}\n\n"
        f"Сообщения участника ({display_name or 'unknown'}):\n{messages_block}"
    )

    answer = await llm_client.chat(
        [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        model=model,
        temperature=temperature,
        fallbacks=fallbacks,
    )

    note = _truncate_note(answer)
    if not note:
        logger.warning("Profile update: model returned an empty note for user %s", user_id)
        return

    await save_note(chat_id, user_id, display_name, note)
    logger.info("Profile update: stored %d-char note for user %s", len(note), user_id)


async def run_profile_updates(bot_id: int) -> None:
    chat_id = settings.GROUP_CHAT_ID
    since_ts = int(time.time()) - PROFILE_INTERVAL_SECONDS

    candidates = await fetch_active_users(chat_id, since_ts, MIN_NEW_MESSAGES, bot_id)
    if not candidates:
        logger.info("Profile update: nobody reached %d new messages", MIN_NEW_MESSAGES)
        return

    values = await get_settings()
    system_content = base_system_prompt(values)
    prompt = profile_prompt(values)
    temperature = setting_value(values, "temperature", DEFAULT_TEMPERATURE, float)
    model = resolve_model(values, "profile_model")
    fallbacks = parse_fallbacks(values)

    logger.info("Profile update: %d candidate(s)", len(candidates))

    for user_id, count in candidates:
        try:
            await update_profile(
                chat_id, user_id, since_ts, system_content, prompt, temperature, model, fallbacks
            )
        except LLMQuotaError:
            logger.exception("Profile update: quota exhausted, stopping this cycle")
            return
        except LLMUnavailableError:
            logger.exception("Profile update: model still failing after retries, stopping this cycle")
            return
        except httpx.TimeoutException:
            logger.exception("Profile update: timed out for user %s, skipping", user_id)
        except Exception:
            logger.exception("Profile update: failed for user %s", user_id)


async def list_profiles(chat_id: int) -> list[tuple[int, str | None, str | None, int, int]]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT user_id, display_name, notes, updated_at, opted_out
            FROM user_profiles
            WHERE chat_id = ?
            ORDER BY updated_at DESC
            """,
            (chat_id,),
        )
        return await cursor.fetchall()


async def delete_profile(chat_id: int, user_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM user_profiles WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        await db.commit()


async def rebuild_profile(chat_id: int, user_id: int, hours: int | None = None) -> None:
    """Regenerate one note from the retained history. Raises on LLM failure."""
    lookback = hours if hours is not None else settings.HISTORY_TTL_HOURS
    since_ts = int(time.time()) - lookback * 3600

    values = await get_settings()
    await update_profile(
        chat_id,
        user_id,
        since_ts,
        base_system_prompt(values),
        profile_prompt(values),
        setting_value(values, "temperature", DEFAULT_TEMPERATURE, float),
        resolve_model(values, "profile_model"),
        parse_fallbacks(values),
    )


async def profiles_task(bot_id: int) -> None:
    while True:
        await asyncio.sleep(PROFILE_INTERVAL_SECONDS)
        try:
            await run_profile_updates(bot_id)
        except Exception:
            logger.exception("Profile update cycle failed")
