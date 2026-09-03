import logging
import os
import sqlite3
import time
from typing import Any, Callable

import aiosqlite

from core.config import settings

logger = logging.getLogger(__name__)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        display_name TEXT,
        text TEXT,
        reply_to_message_id INTEGER,
        ts INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        display_name TEXT,
        notes TEXT,
        updated_at INTEGER NOT NULL,
        opted_out INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        period_start INTEGER NOT NULL,
        period_end INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS moods (
        name TEXT PRIMARY KEY,
        label TEXT,
        prompt_fragment TEXT,
        temperature REAL,
        is_default INTEGER DEFAULT 0,
        sort_order INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mood_switches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER,
        display_name TEXT,
        from_mood TEXT,
        to_mood TEXT NOT NULL,
        source TEXT NOT NULL,
        ts INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id)",
]

# Fragments are modifiers layered on top of the base prompt, not replacement personas.
SEED_MOODS = [
    ("normal", "Обычное", "", None, 1, 10),
    (
        "bored",
        "Скучающее",
        "Тебе скучно. Отвечай вяло и минимально, одна-две фразы, без энтузиазма и "
        "восклицаний. Происходящее кажется тебе мелким и не стоящим внимания, но "
        "грубить не нужно — просто не старайся.",
        None,
        0,
        20,
    ),
    (
        "sharp",
        "Едкое",
        "Включи максимальный сарказм: коротко, колко, одной-двумя фразами с подколом. "
        "Бей по сути сказанного, а не по людям — без оскорблений и перехода на личности.",
        0.9,
        0,
        30,
    ),
    (
        "helpful",
        "Дружелюбное",
        "Сними манерность. Отвечай прямо, полно и по делу, без иронии и подколов. "
        "Знаешь ответ — разверни его; не знаешь — так и скажи.",
        0.3,
        0,
        40,
    ),
    (
        "quiet",
        "Тихое",
        "Отвечай только когда обращаются напрямую. Максимум два предложения. "
        "Никаких отступлений, уточняющих вопросов и лишних деталей.",
        None,
        0,
        50,
    ),
]

# Applied after SCHEMA, for databases created before a column existed.
# SQLite has no ADD COLUMN IF NOT EXISTS, so "duplicate column name" means already applied.
MIGRATIONS = [
    "ALTER TABLE user_profiles ADD COLUMN opted_out INTEGER NOT NULL DEFAULT 0",
    # 'model' split into per-task settings; carry the old value over, then drop it.
    "INSERT OR IGNORE INTO settings (key, value) "
    "SELECT 'chat_model', value FROM settings WHERE key = 'model'",
    "INSERT OR IGNORE INTO settings (key, value) "
    "SELECT 'summary_model', value FROM settings WHERE key = 'model'",
    "INSERT OR IGNORE INTO settings (key, value) "
    "SELECT 'profile_model', value FROM settings WHERE key = 'model'",
    "DELETE FROM settings WHERE key = 'model'",
]


async def init_db() -> None:
    db_dir = os.path.dirname(settings.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(settings.DB_PATH) as db:
        for statement in SCHEMA:
            await db.execute(statement)

        for statement in MIGRATIONS:
            try:
                cursor = await db.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
                continue

            # DDL reports rowcount -1; DML reports 0 when it was a no-op on this run.
            if cursor.rowcount != 0:
                logger.info("Applied migration: %s", statement)

        cursor = await db.executemany(
            "INSERT OR IGNORE INTO moods "
            "(name, label, prompt_fragment, temperature, is_default, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            SEED_MOODS,
        )
        if cursor.rowcount > 0:
            logger.info("Seeded %d mood(s)", cursor.rowcount)

        await db.commit()

    logger.info("Database initialized at %s", settings.DB_PATH)


def get_db() -> aiosqlite.Connection:
    return aiosqlite.connect(settings.DB_PATH)


async def insert_message(
    *,
    chat_id: int,
    user_id: int,
    username: str | None,
    display_name: str | None,
    text: str | None,
    reply_to_message_id: int | None,
    ts: int,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO messages
                (chat_id, user_id, username, display_name, text, reply_to_message_id, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, username, display_name, text, reply_to_message_id, ts),
        )
        await db.commit()


async def insert_summary(
    *,
    chat_id: int,
    period_start: int,
    period_end: int,
    text: str,
    created_at: int,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO summaries (chat_id, period_start, period_end, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, period_start, period_end, text, created_at),
        )
        await db.commit()


# Large blobs that live in the settings table but must stay out of the settings dict,
# which is read on every message.
SETTINGS_BLOB_KEYS = {"models_cache"}


async def load_settings() -> dict[str, str]:
    async with get_db() as db:
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
    return {key: value for key, value in rows if key not in SETTINGS_BLOB_KEYS}


async def get_setting(key: str) -> str | None:
    async with get_db() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()

    if key not in SETTINGS_BLOB_KEYS:
        invalidate_settings_cache()


async def delete_setting(key: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await db.commit()

    if key not in SETTINGS_BLOB_KEYS:
        invalidate_settings_cache()


SETTINGS_CACHE_TTL_SECONDS = 30.0

_settings_cache: dict[str, str] | None = None
_settings_cached_at: float = 0.0


async def get_settings() -> dict[str, str]:
    """Settings with a short TTL cache, so SQLite is not hit on every message."""
    global _settings_cache, _settings_cached_at

    now = time.monotonic()
    if _settings_cache is None or now - _settings_cached_at >= SETTINGS_CACHE_TTL_SECONDS:
        _settings_cache = await load_settings()
        _settings_cached_at = now

    return _settings_cache


def invalidate_settings_cache() -> None:
    global _settings_cache, _settings_cached_at
    _settings_cache = None
    _settings_cached_at = 0.0


async def save_settings(values: dict[str, str]) -> None:
    async with get_db() as db:
        await db.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )
        await db.commit()

    invalidate_settings_cache()


def setting_bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "on", "yes"}


def setting_value(values: dict[str, str], key: str, default: Any, cast: Callable[[str], Any]) -> Any:
    raw = values.get(key)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid value for setting %r: %r, falling back to %r", key, raw, default)
        return default
