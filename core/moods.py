import asyncio
import logging
import time

import aiosqlite

from core.db import get_db, get_settings, set_setting, setting_value

logger = logging.getLogger(__name__)

CURRENT_MOOD_KEY = "current_mood"
ADMIN_ONLY_KEY = "mood_admin_only"
TTL_MINUTES_KEY = "mood_ttl_minutes"

SWITCH_COOLDOWN_SECONDS = 30
TTL_CHECK_INTERVAL_SECONDS = 60
SWITCH_LOG_KEEP = 500

SOURCE_CHAT = "chat"
SOURCE_ADMIN = "admin"
SOURCE_EXPIRY = "expiry"


async def _query(sql: str, params: tuple = ()) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]


async def list_moods() -> list[dict]:
    return await _query(
        "SELECT name, label, prompt_fragment, temperature, is_default, sort_order "
        "FROM moods ORDER BY COALESCE(sort_order, 9999), name"
    )


async def get_mood(name: str) -> dict | None:
    rows = await _query(
        "SELECT name, label, prompt_fragment, temperature, is_default, sort_order "
        "FROM moods WHERE name = ?",
        (name,),
    )
    return rows[0] if rows else None


async def get_default_mood() -> dict | None:
    rows = await _query(
        "SELECT name, label, prompt_fragment, temperature, is_default, sort_order "
        "FROM moods WHERE is_default = 1 ORDER BY COALESCE(sort_order, 9999) LIMIT 1"
    )
    return rows[0] if rows else None


async def resolve_current(values: dict[str, str]) -> dict | None:
    """Active mood: the one named in settings, else the default row."""
    name = values.get(CURRENT_MOOD_KEY)
    if name:
        mood = await get_mood(name)
        if mood is not None:
            return mood
        logger.warning("current_mood=%r is not a known mood, using the default", name)
    return await get_default_mood()


def compose_system_prompt(base_prompt: str, mood: dict | None) -> str:
    """Base prompt plus the mood modifier. The base is never rewritten."""
    fragment = (mood or {}).get("prompt_fragment") or ""
    fragment = fragment.strip()
    if not fragment:
        return base_prompt
    return f"{base_prompt}\n\n{fragment}"


def mood_temperature(mood: dict | None, fallback: float) -> float:
    value = (mood or {}).get("temperature")
    return float(value) if value is not None else fallback


async def set_current(name: str) -> None:
    await set_setting(CURRENT_MOOD_KEY, name)


async def log_switch(
    chat_id: int,
    user_id: int | None,
    display_name: str | None,
    from_mood: str | None,
    to_mood: str,
    source: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO mood_switches "
            "(chat_id, user_id, display_name, from_mood, to_mood, source, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, display_name, from_mood, to_mood, source, int(time.time())),
        )
        await db.commit()

    logger.info(
        "Mood switch (%s): %s -> %s by %s (%s)",
        source, from_mood or "—", to_mood, display_name or "—", user_id,
    )


async def recent_switches(limit: int = 10) -> list[dict]:
    return await _query(
        "SELECT user_id, display_name, from_mood, to_mood, source, ts "
        "FROM mood_switches ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def last_switch_ts() -> int | None:
    rows = await _query("SELECT MAX(ts) AS ts FROM mood_switches")
    return rows[0]["ts"] if rows and rows[0]["ts"] is not None else None


async def trim_switch_log(keep: int = SWITCH_LOG_KEEP) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM mood_switches WHERE id NOT IN "
            "(SELECT id FROM mood_switches ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        await db.commit()


async def upsert_mood(
    name: str,
    label: str | None,
    prompt_fragment: str | None,
    temperature: float | None,
    sort_order: int | None,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO moods (name, label, prompt_fragment, temperature, is_default, sort_order)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(name) DO UPDATE SET
                label = excluded.label,
                prompt_fragment = excluded.prompt_fragment,
                temperature = excluded.temperature,
                sort_order = excluded.sort_order
            """,
            (name, label, prompt_fragment, temperature, sort_order),
        )
        await db.commit()


class CannotDeleteDefault(Exception):
    pass


async def delete_mood(name: str) -> None:
    mood = await get_mood(name)
    if mood is None:
        return
    if mood["is_default"]:
        raise CannotDeleteDefault(name)

    async with get_db() as db:
        await db.execute("DELETE FROM moods WHERE name = ?", (name,))
        await db.commit()

    # Nothing should stay pointed at a mood that no longer exists.
    values = await get_settings()
    if values.get(CURRENT_MOOD_KEY) == name:
        fallback = await get_default_mood()
        if fallback is not None:
            await set_current(fallback["name"])
            logger.info("Active mood %s was deleted, reverted to %s", name, fallback["name"])


async def set_default(name: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE moods SET is_default = 0")
        await db.execute("UPDATE moods SET is_default = 1 WHERE name = ?", (name,))
        await db.commit()


async def revert_to_default(chat_id: int, from_mood: str | None, source: str = SOURCE_EXPIRY) -> str | None:
    default = await get_default_mood()
    if default is None:
        return None
    await set_current(default["name"])
    await log_switch(chat_id, None, None, from_mood, default["name"], source)
    return default["name"]


async def mood_ttl_task(chat_id: int) -> None:
    """Revert to the default mood after mood_ttl_minutes of no switching (0 = off)."""
    while True:
        await asyncio.sleep(TTL_CHECK_INTERVAL_SECONDS)
        try:
            values = await get_settings()
            ttl_minutes = setting_value(values, TTL_MINUTES_KEY, 0, int)
            if ttl_minutes <= 0:
                continue

            current = await resolve_current(values)
            default = await get_default_mood()
            if current is None or default is None or current["name"] == default["name"]:
                continue

            changed_at = await last_switch_ts()
            if changed_at is None:
                continue

            idle_seconds = int(time.time()) - changed_at
            if idle_seconds < ttl_minutes * 60:
                continue

            await revert_to_default(chat_id, current["name"])
            logger.info(
                "Mood %s expired after %d min idle, reverted to %s",
                current["name"], ttl_minutes, default["name"],
            )
        except Exception:
            logger.exception("Mood TTL check failed")
